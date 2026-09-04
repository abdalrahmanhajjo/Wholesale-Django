"""The only module in this project that talks to Stripe over the network.

Everything Stripe-shaped is translated here into plain dataclasses, so the
service layer above never handles a raw API object and the tests never need a
network. Two conventions are worth stating because they cause real money bugs:

* Stripe counts in the currency's smallest unit - 1000 for $10.00, 1000 for
  ¥1000. This module is where that conversion happens and nowhere else.
* The import is lazy. ``stripe`` is a runtime dependency of a feature most
  deployments will not switch on, and a missing package should surface as
  "Stripe is not configured" on one screen rather than as an ImportError that
  stops the site from starting.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings

logger = logging.getLogger(__name__)

#: Stripe's own cap on how long a Checkout Session can stay payable.
MIN_SESSION_MINUTES = 30
MAX_SESSION_MINUTES = 1440


class StripeUnavailable(RuntimeError):
    """Stripe is switched off, not installed, or missing its webhook secret."""


class StripeCallFailed(RuntimeError):
    """Stripe was reachable but refused, or answered with something unusable."""


def is_enabled() -> bool:
    """Read the key itself rather than a cached flag, so there is one answer."""
    return bool(settings.STRIPE_SECRET_KEY)


def _stripe():
    """The configured SDK module, or a message explaining what is missing."""
    if not settings.STRIPE_SECRET_KEY:
        raise StripeUnavailable(
            "Stripe is not configured. Set STRIPE_SECRET_KEY in the environment "
            "(see .env.example) and restart."
        )
    try:
        import stripe
    except ModuleNotFoundError as exc:  # pragma: no cover - environment repair
        raise StripeUnavailable(
            "The stripe package is not installed in this environment. "
            "Run: pip install -r requirements.txt"
        ) from exc
    stripe.api_key = settings.STRIPE_SECRET_KEY
    return stripe


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------
def to_minor(amount: Decimal, *, decimal_places: int) -> int:
    """Decimal in major units -> Stripe's integer smallest unit.

    Rounds rather than truncates: a half-cent thrown away silently is a
    reconciliation difference nobody can trace back afterwards.
    """
    scaled = (amount * (Decimal(10) ** decimal_places)).to_integral_value(
        rounding=ROUND_HALF_UP
    )
    return int(scaled)


def from_minor(minor: int, *, decimal_places: int) -> Decimal:
    return (Decimal(minor) / (Decimal(10) ** decimal_places)).quantize(
        Decimal(1).scaleb(-decimal_places)
    )


def _timestamp(value) -> datetime | None:
    if not value:
        return None
    return datetime.fromtimestamp(int(value), tz=UTC)


# ---------------------------------------------------------------------------
# What the service layer sees
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class CheckoutSession:
    id: str
    url: str
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class Settlement:
    """The outcome of one Checkout Session, in the invoice's own currency."""

    session_id: str
    paid: bool
    payment_intent_id: str
    charge_id: str
    amount: Decimal
    currency_code: str
    fee: Decimal
    #: Set when the fee could not be established. The payment is still real and
    #: still posts; the fee is simply recorded as zero and said so out loud,
    #: because a guessed fee is worse than a missing one.
    fee_note: str = ""


# ---------------------------------------------------------------------------
# Calls
# ---------------------------------------------------------------------------
def create_checkout_session(
    *,
    amount: Decimal,
    decimal_places: int,
    currency_code: str,
    description: str,
    customer_email: str,
    client_reference_id: str,
    success_url: str,
    cancel_url: str,
    idempotency_key: str,
) -> CheckoutSession:
    """Create a hosted payment page for one invoice."""
    stripe = _stripe()
    minutes = min(
        max(int(settings.STRIPE_SESSION_MINUTES), MIN_SESSION_MINUTES),
        MAX_SESSION_MINUTES,
    )
    expires_at = int(datetime.now(tz=UTC).timestamp()) + minutes * 60

    payload = {
        "mode": "payment",
        "line_items": [
            {
                "quantity": 1,
                "price_data": {
                    "currency": currency_code.lower(),
                    "unit_amount": to_minor(amount, decimal_places=decimal_places),
                    "product_data": {"name": description},
                },
            }
        ],
        "client_reference_id": client_reference_id,
        "success_url": success_url,
        "cancel_url": cancel_url,
        "expires_at": expires_at,
    }
    if customer_email:
        payload["customer_email"] = customer_email

    try:
        # The idempotency key means a double-clicked button produces one link
        # rather than two payable ones for the same invoice.
        session = stripe.checkout.Session.create(**payload, idempotency_key=idempotency_key)
    except stripe.StripeError as exc:
        # Narrow on purpose. Network failures arrive as APIConnectionError,
        # which is a StripeError, so this still covers the timeout case - while
        # a TypeError from a mistake in the payload above stays a crash instead
        # of being reported to a user as "Stripe refused".
        logger.warning("Stripe session creation failed: %s", exc)
        raise StripeCallFailed(str(exc)) from exc

    if not session.get("id") or not session.get("url"):
        raise StripeCallFailed("Stripe returned a session with no id or no URL.")
    return CheckoutSession(
        id=session["id"],
        url=session["url"],
        expires_at=_timestamp(session.get("expires_at")),
    )


def fetch_settlement(session_id: str, *, decimal_places: int) -> Settlement:
    """Re-read a session from Stripe, including what the fee turned out to be.

    The webhook body is not trusted for any of this. It arrives over the public
    internet and, even signed, it is a snapshot of the moment Stripe sent it;
    reading back gives the current truth and, more practically, the balance
    transaction that carries the fee - which the completion event does not.
    """
    stripe = _stripe()
    try:
        session = stripe.checkout.Session.retrieve(
            session_id,
            expand=["payment_intent.latest_charge.balance_transaction"],
        )
    except stripe.StripeError as exc:
        logger.warning("Stripe session retrieval failed for %s: %s", session_id, exc)
        raise StripeCallFailed(str(exc)) from exc

    currency_code = (session.get("currency") or "").upper()
    amount = from_minor(int(session.get("amount_total") or 0), decimal_places=decimal_places)

    intent = session.get("payment_intent") or {}
    if isinstance(intent, str):
        # Not expanded - an older API version, or a session with no intent yet.
        intent = {"id": intent}
    charge = intent.get("latest_charge") or {}
    if isinstance(charge, str):
        charge = {"id": charge}
    balance_transaction = charge.get("balance_transaction") or {}
    if isinstance(balance_transaction, str):
        balance_transaction = {}

    fee, fee_note = _fee_in_charge_currency(
        balance_transaction,
        currency_code=currency_code,
        decimal_places=decimal_places,
    )

    return Settlement(
        session_id=session_id,
        paid=session.get("payment_status") == "paid",
        payment_intent_id=intent.get("id") or "",
        charge_id=charge.get("id") or "",
        amount=amount,
        currency_code=currency_code,
        fee=fee,
        fee_note=fee_note,
    )


def _fee_in_charge_currency(
    balance_transaction: dict, *, currency_code: str, decimal_places: int
) -> tuple[Decimal, str]:
    """Stripe's fee, expressed in the currency the invoice is denominated in.

    A balance transaction is stated in the *settlement* currency - what Stripe
    pays out in - which is not always what the customer was charged in. When
    they differ Stripe supplies the rate it used, and the fee has to be divided
    back through it, because ``fee`` is already on the settlement side.

    Anything unresolvable returns zero and a reason. That leaves the receipt
    posting at its gross amount with the fee missing, which an accountant can
    see and correct; a plausible-looking wrong number is what they cannot.
    """
    if not balance_transaction:
        return Decimal("0"), (
            "Stripe had not published a balance transaction for this charge yet, "
            "so the processing fee is not recorded on this receipt."
        )

    fee_minor = balance_transaction.get("fee")
    if fee_minor is None:
        return Decimal("0"), "Stripe reported no fee figure for this charge."

    settlement_currency = (balance_transaction.get("currency") or "").upper()
    fee = from_minor(int(fee_minor), decimal_places=decimal_places)
    if not settlement_currency or settlement_currency == currency_code:
        return fee, ""

    rate = balance_transaction.get("exchange_rate")
    if not rate:
        return Decimal("0"), (
            f"Stripe settled this charge in {settlement_currency} rather than "
            f"{currency_code} and did not supply the rate it used, so the fee "
            "could not be converted and is not recorded on this receipt."
        )
    converted = (fee / Decimal(str(rate))).quantize(
        Decimal(1).scaleb(-decimal_places), rounding=ROUND_HALF_UP
    )
    return converted, ""


def verify_webhook(payload: bytes, signature: str):
    """Confirm a callback really came from Stripe, and return its event.

    The signing secret is the whole of the authentication on this endpoint - it
    is the only unauthenticated route in the application - so a missing secret
    is treated as a hard failure rather than as permission to skip the check.
    """
    if not settings.STRIPE_WEBHOOK_SECRET:
        raise StripeUnavailable(
            "STRIPE_WEBHOOK_SECRET is not set, so Stripe callbacks cannot be "
            "authenticated and are all rejected."
        )
    stripe = _stripe()
    # Raises on a bad signature, a replayed timestamp, or a mangled body.
    return stripe.Webhook.construct_event(payload, signature, settings.STRIPE_WEBHOOK_SECRET)
