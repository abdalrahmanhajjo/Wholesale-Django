"""Turning a Stripe checkout into a posted, allocated customer receipt (PAY-013).

The shape of this module is set by one fact: **a payment is real whether or not
the ledger is ready to accept it.** Stripe tells us money moved; it does not ask
whether the period is open, whether an account mapping exists, or whether anyone
has entered today's exchange rate. So settlement is split in two:

1. Record that the customer paid. This must not fail for an accounting reason,
   and it happens under a row lock so a redelivered webhook is a no-op.
2. Try to post and allocate the receipt. If that cannot happen, the reason is
   written to ``settlement_error`` and the checkout shows up on a worklist for
   someone to finish by hand.

Collapsing the two - letting a closed period turn into a 500 - would make Stripe
retry for hours and then give up, leaving money received and no record of it
anywhere in this system. That is the failure worth designing against.
"""

import logging
import uuid
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from apps.core.models import Currency, DocumentStatus, ExchangeRate
from apps.ledger.services.exceptions import PostingError
from apps.payments import stripe_gateway
from apps.payments.allocation import AllocationLineInput, allocate_payment
from apps.payments.models import (
    Payment,
    PaymentDirection,
    PaymentMethod,
    StripeCheckout,
    StripeCheckoutStatus,
)
from apps.payments.services import create_payment, post_payment
from apps.payments.stripe_gateway import StripeCallFailed, StripeUnavailable

logger = logging.getLogger(__name__)

ZERO = Decimal("0")
STRIPE_METHOD_CODE = "STRIPE"

#: Fixed namespace so a batch key can be re-derived from a session id. Two
#: deliveries of the same event therefore present the same batch to
#: ``allocate_payment``, which recognises it and returns the first result
#: instead of allocating twice.
ALLOCATION_NAMESPACE = uuid.UUID("6f1d5c2e-3a47-4f8b-9c21-0d8e7b4a5f30")

#: Invoice states that can still take money.
CHARGEABLE_STATUSES = {
    DocumentStatus.POSTED,
    DocumentStatus.PARTIAL,
}


class StripeSettlementError(Exception):
    """The customer paid, but the receipt could not be posted."""


class UnknownStripeSession(StripeSettlementError):
    """A session this installation has no record of creating.

    Kept separate because it is the one settlement failure a retry can never
    fix, so the webhook answers 200 to it and lets Stripe stop asking.
    """


# ---------------------------------------------------------------------------
# Creating a payment link
# ---------------------------------------------------------------------------
def _absolute(path: str) -> str:
    from django.conf import settings

    return f"{settings.STRIPE_RETURN_ORIGIN}{path}"


def live_checkout(invoice) -> StripeCheckout | None:
    """An existing link for this invoice that a customer could still pay.

    Handing out a second payable link for the same invoice is how a customer
    ends up paying twice, so an unexpired pending session is reused rather than
    replaced.
    """
    return (
        StripeCheckout.objects.filter(invoice=invoice, status=StripeCheckoutStatus.PENDING)
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()))
        .order_by("-created_at")
        .first()
    )


@transaction.atomic
def start_checkout(invoice, *, user) -> StripeCheckout:
    """Create (or reuse) a Stripe payment link for one posted sales invoice."""
    if not stripe_gateway.is_enabled():
        raise ValidationError(
            "Stripe is not configured on this installation, so payment links "
            "cannot be created. A Stripe receipt can still be entered by hand."
        )
    if invoice.is_reversed or invoice.status not in CHARGEABLE_STATUSES:
        raise ValidationError(
            f"{invoice.number} is {invoice.get_status_display().lower()} and "
            "cannot be charged."
        )
    if invoice.open_txn <= ZERO:
        raise ValidationError(f"{invoice.number} has nothing left to pay.")

    existing = live_checkout(invoice)
    if existing is not None:
        if existing.amount_txn == invoice.open_txn:
            return existing
        # The open amount moved under it - a credit note, or a part payment. The
        # old link is for the wrong figure, so it stops being offered here. If a
        # customer pays it anyway the webhook still settles it correctly; the
        # allocation is capped at what is open and any excess becomes an advance.
        StripeCheckout.objects.filter(pk=existing.pk).update(
            status=StripeCheckoutStatus.EXPIRED, updated_at=timezone.now()
        )

    method = _stripe_method()
    if method.default_money_account_id is None:
        raise ValidationError(
            "The STRIPE payment method has no money account. Set one in "
            "Settings before creating payment links."
        )

    invoice_url = reverse("sales:invoice_detail", args=[invoice.pk])
    try:
        session = stripe_gateway.create_checkout_session(
            amount=invoice.open_txn,
            decimal_places=invoice.currency.decimal_places,
            currency_code=invoice.currency.code,
            description=f"Invoice {invoice.number}",
            customer_email=invoice.customer.email,
            client_reference_id=invoice.number,
            success_url=_absolute(f"{invoice_url}?stripe=paid"),
            cancel_url=_absolute(f"{invoice_url}?stripe=cancelled"),
            # Keyed on what is being charged, not on when the button was
            # pressed, so a double click cannot mint two payable links.
            idempotency_key=f"invoice:{invoice.pk}:open:{invoice.open_txn}",
        )
    except (StripeUnavailable, StripeCallFailed) as exc:
        raise ValidationError(str(exc)) from exc

    return StripeCheckout.objects.create(
        invoice=invoice,
        session_id=session.id,
        url=session.url,
        expires_at=session.expires_at,
        currency=invoice.currency,
        amount_txn=invoice.open_txn,
        status=StripeCheckoutStatus.PENDING,
        created_by=user,
        updated_by=user,
    )


def _stripe_method() -> PaymentMethod:
    method = (
        PaymentMethod.objects.filter(code=STRIPE_METHOD_CODE, is_active=True)
        .select_related("default_money_account")
        .first()
    )
    if method is None:
        raise ValidationError(
            "No active STRIPE payment method is configured. It is seeded by "
            "core.0011_seed_stripe_settlement - run: python manage.py migrate"
        )
    return method


# ---------------------------------------------------------------------------
# Settling one
# ---------------------------------------------------------------------------
def settle_session(session_id: str) -> StripeCheckout:
    """Record that a session was paid, and post its receipt if the ledger lets us.

    Safe to call repeatedly: the first call that finds the row unpaid does the
    work, and every later one returns the same checkout untouched.

    Ordered so that the call to Stripe happens with **no transaction open and no
    row locked**. A network call inside a lock holds a database connection for
    however long the other end feels like taking, which on a slow day is how one
    webhook becomes a pile of blocked ones.
    """
    checkout = _checkout_for(session_id)
    if checkout.payment_id is not None:
        return checkout

    settlement = _read_settlement(checkout)
    if not settlement.paid:
        return checkout

    with transaction.atomic():
        # of=("self",) because created_by is nullable: select_related turns it
        # into an outer join, and PostgreSQL refuses to lock the nullable side
        # of one. Only this row needs locking anyway.
        locked = (
            StripeCheckout.objects.select_for_update(of=("self",))
            .select_related("invoice", "currency", "created_by")
            .get(pk=checkout.pk)
        )
        # Re-checked under the lock: two deliveries of the same event can both
        # have got this far, and only one of them may write.
        if locked.payment_id is not None:
            return locked

        locked.status = StripeCheckoutStatus.PAID
        locked.payment_intent_id = settlement.payment_intent_id
        locked.charge_id = settlement.charge_id
        locked.fee_txn = settlement.fee
        locked.paid_at = locked.paid_at or timezone.now()
        locked.save(
            update_fields=[
                "status",
                "payment_intent_id",
                "charge_id",
                "fee_txn",
                "paid_at",
                "updated_at",
            ]
        )

    return post_settled_checkout(locked, fee_note=settlement.fee_note)


def _checkout_for(session_id: str) -> StripeCheckout:
    checkout = (
        StripeCheckout.objects.select_related("invoice", "currency", "created_by")
        .filter(session_id=session_id)
        .first()
    )
    if checkout is None:
        # A session this installation never created. Somebody else's test
        # webhook pointed here, or the database was restored underneath us.
        logger.warning("Stripe settlement for unknown session %s", session_id)
        raise UnknownStripeSession(f"Unknown Stripe session {session_id}.")
    return checkout


def _read_settlement(checkout: StripeCheckout):
    try:
        return stripe_gateway.fetch_settlement(
            checkout.session_id,
            decimal_places=checkout.currency.decimal_places,
        )
    except (StripeUnavailable, StripeCallFailed) as exc:
        raise StripeSettlementError(str(exc)) from exc


def post_settled_checkout(checkout: StripeCheckout, *, fee_note: str = "") -> StripeCheckout:
    """Create, post and allocate the receipt for a checkout already marked paid.

    Every foreseeable accounting objection is caught and written to the row
    rather than raised: a closed period, an unmapped account, no rate for the
    day. None of them make the money less received, and all of them are fixed by
    a person rather than by a retry. ``PostingError`` is in that list because the
    posting engine - not Django - is what rejects a missing account mapping, and
    that is precisely one of the cases this is here to absorb.
    """
    if checkout.payment_id is not None or checkout.status != StripeCheckoutStatus.PAID:
        return checkout

    try:
        with transaction.atomic():
            # Locked and re-checked here as well: this is the step that creates
            # money in the ledger, so it is the one that must happen once.
            locked = (
                StripeCheckout.objects.select_for_update(of=("self",))
                .select_related("invoice", "currency", "created_by")
                .get(pk=checkout.pk)
            )
            if locked.payment_id is not None or locked.status != StripeCheckoutStatus.PAID:
                return locked

            payment = _create_receipt(locked)
            # post_payment works on its own row-locked reload and returns it.
            # Keeping the instance from before the post would leave this
            # holding a copy that still says DRAFT with no journal, which is
            # exactly what the caller reads back off checkout.payment.
            posted = post_payment(payment, user=locked.created_by).payment
            _allocate_to_invoice(locked, posted)
            locked.payment = posted
            locked.settlement_error = fee_note
            locked.save(update_fields=["payment", "settlement_error", "updated_at"])
            checkout = locked
    except (ValidationError, PostingError, StripeSettlementError) as exc:
        message = _readable(exc)
        logger.warning(
            "Stripe receipt could not be posted for session %s: %s",
            checkout.session_id,
            message,
        )
        StripeCheckout.objects.filter(pk=checkout.pk).update(
            settlement_error=message, updated_at=timezone.now()
        )
        checkout.settlement_error = message
    return checkout


def _create_receipt(checkout: StripeCheckout) -> Payment:
    if checkout.created_by_id is None:
        raise StripeSettlementError(
            "This checkout has no originating user, so its receipt cannot be "
            "attributed to anyone. Enter the receipt by hand."
        )
    method = _stripe_method()
    money_account = method.default_money_account
    if money_account is None:
        raise ValidationError("The STRIPE payment method has no money account.")

    today = timezone.localdate()
    invoice = checkout.invoice
    return create_payment(
        user=checkout.created_by,
        direction=PaymentDirection.RECEIPT,
        payment_date=today,
        posting_date=today,
        customer=invoice.customer,
        vendor=None,
        currency=checkout.currency,
        exchange_rate=_rate_for(checkout.currency, today),
        amount_txn=checkout.amount_txn,
        fee_txn=checkout.fee_txn,
        method=method,
        money_account=money_account,
        # PaymentMethod.requires_reference is true for Stripe: this is what
        # ties the receipt to a row in the Stripe dashboard.
        reference=checkout.payment_intent_id or checkout.session_id,
        narration=f"Stripe payment for invoice {invoice.number}",
    )


def _rate_for(currency: Currency, on_date) -> Decimal:
    """The exchange rate to record against the receipt (BR-013).

    A missing rate stops the posting instead of defaulting to 1. Inventing a
    rate would silently misstate the receipt in base currency, and it is the one
    error here that would not announce itself later.
    """
    if currency.is_base:
        return Decimal("1")
    rate = (
        ExchangeRate.objects.filter(currency=currency, rate_date__lte=on_date)
        .order_by("-rate_date")
        .values_list("rate", flat=True)
        .first()
    )
    if rate is None:
        raise StripeSettlementError(
            f"No exchange rate is on file for {currency.code} on or before "
            f"{on_date}. Enter one in Settings, then post this receipt."
        )
    return rate


def _allocate_to_invoice(checkout: StripeCheckout, payment: Payment) -> None:
    """Apply the receipt to the invoice it was raised for.

    The link was created for the invoice's open amount, but time passes: a
    credit note or another receipt may have closed some of it in between. So the
    allocation is capped at whatever is genuinely still open, and any remainder
    stays on the customer's account as unapplied credit rather than forcing the
    invoice past its total.
    """
    invoice = type(checkout.invoice).objects.get(pk=checkout.invoice_id)
    amount = min(payment.unallocated_txn, invoice.open_txn)
    if amount <= ZERO:
        return
    allocate_payment(
        payment,
        lines=[AllocationLineInput(target_id=invoice.pk, amount_txn=amount)],
        allocation_date=payment.payment_date,
        user=payment.created_by,
        batch_key=uuid.uuid5(ALLOCATION_NAMESPACE, checkout.session_id),
    )


def _readable(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return (
            " ".join(str(message) for message in exc.messages)
            or "The receipt could not be posted."
        )
    return str(exc)


# ---------------------------------------------------------------------------
# Webhook dispatch
# ---------------------------------------------------------------------------
def expire_session(session_id: str) -> None:
    """A link nobody paid. Ordinary, and not worth an error anywhere."""
    StripeCheckout.objects.filter(
        session_id=session_id, status=StripeCheckoutStatus.PENDING
    ).update(status=StripeCheckoutStatus.EXPIRED, updated_at=timezone.now())


def handle_event(event) -> str:
    """Act on one verified Stripe event. Returns a short line for the log.

    Unknown event types are accepted and ignored on purpose. Stripe sends what
    the endpoint is subscribed to plus whatever gets added to the account later,
    and answering anything but 200 to those would put the endpoint into Stripe's
    retry and disable machinery over events this integration never wanted.
    """
    event_type = event.get("type")
    session = (event.get("data") or {}).get("object") or {}
    session_id = session.get("id")

    if event_type == "checkout.session.completed":
        if not session_id:
            return "completion event carried no session id"
        checkout = settle_session(session_id)
        if checkout.needs_attention:
            return f"{session_id} paid, receipt outstanding: {checkout.settlement_error}"
        return f"{session_id} paid and posted"

    if event_type in {"checkout.session.expired", "checkout.session.async_payment_failed"}:
        if session_id:
            expire_session(session_id)
        return f"{session_id} expired"

    return f"ignored {event_type}"
