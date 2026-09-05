"""Transactional entry and posting services for receipts and vendor payments."""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.core.models import (
    DocumentSequence,
    DocumentStatus,
    DocumentType,
    FiscalPeriod,
    PeriodStatus,
    SequenceReset,
)
from apps.ledger.services.posting import PostingEngine, PostingRequest
from apps.payments.models import Payment, PaymentDirection
from apps.payments.posting import build_payment_journal, payment_required_mappings

MONEY_QUANTUM = Decimal("0.0001")
posting_engine = PostingEngine()


@dataclass(frozen=True, slots=True)
class PaymentPostingResult:
    payment: Payment
    created: bool


def _period_key(sequence, document_date):
    if sequence.reset_policy == SequenceReset.MONTHLY:
        return document_date.strftime("%Y-%m")
    if sequence.reset_policy == SequenceReset.YEARLY:
        return document_date.strftime("%Y")
    return ""


def allocate_payment_number(direction, payment_date, series="DEFAULT"):
    document_type = (
        DocumentType.CUSTOMER_RECEIPT
        if direction == PaymentDirection.RECEIPT
        else DocumentType.VENDOR_PAYMENT
    )
    sequence = (
        DocumentSequence.objects.select_for_update()
        .filter(document_type=document_type, series=series, is_active=True)
        .first()
    )
    if sequence is None:
        raise ValidationError(
            f"No active {document_type.label.lower()} number sequence is configured."
        )
    key = _period_key(sequence, payment_date)
    if key and key != sequence.period_key:
        sequence.next_number = 1
        sequence.period_key = key
    number = f"{sequence.prefix}{sequence.next_number:0{sequence.padding}d}{sequence.suffix}"
    sequence.next_number += 1
    sequence.save(update_fields=["next_number", "period_key"])
    return number


def resolve_open_period(posting_date):
    period = (
        FiscalPeriod.objects.select_for_update()
        .filter(start_date__lte=posting_date, end_date__gte=posting_date)
        .first()
    )
    if period is None:
        raise ValidationError({"posting_date": "No fiscal period covers this posting date."})
    if period.status != PeriodStatus.OPEN:
        raise ValidationError({"posting_date": f"Fiscal period {period.name} is closed."})
    return period


def _apply_derived_values(payment):
    payment.amount_base = (payment.amount_txn * payment.exchange_rate).quantize(
        MONEY_QUANTUM, rounding=ROUND_HALF_UP
    )
    payment.fee_base = (payment.fee_txn * payment.exchange_rate).quantize(
        MONEY_QUANTUM, rounding=ROUND_HALF_UP
    )
    payment.unallocated_txn = payment.amount_txn - payment.allocated_txn


def _validate_business_rules(payment):
    """Run cross-table payment rules without repeating form and DB validation.

    ``Model.full_clean()`` would issue existence and constraint queries for the
    foreign keys that the bound ``ModelForm`` has already resolved. Against a
    remote database those redundant round trips are expensive. ``Payment.clean``
    owns the cross-table rules, while PostgreSQL remains authoritative for the
    model's CHECK, unique, and foreign-key constraints.
    """
    payment.clean()


@transaction.atomic
def create_payment(*, user, **data):
    payment_date = data["payment_date"]
    payment = Payment(
        **data,
        number=allocate_payment_number(data["direction"], payment_date),
        fiscal_period=resolve_open_period(data["posting_date"]),
        status=DocumentStatus.DRAFT,
        allocated_txn=Decimal("0"),
        created_by=user,
        updated_by=user,
    )
    _apply_derived_values(payment)
    _validate_business_rules(payment)
    payment.save()
    return payment


@transaction.atomic
def update_draft_payment(payment, *, user, **data):
    locked = Payment.objects.select_for_update().get(pk=payment.pk)
    if locked.status != DocumentStatus.DRAFT:
        raise ValidationError("Only draft payments can be edited.")
    for field, value in data.items():
        setattr(locked, field, value)
    locked.fiscal_period = resolve_open_period(locked.posting_date)
    locked.updated_by = user
    _apply_derived_values(locked)
    _validate_business_rules(locked)
    locked.save()
    return locked


@transaction.atomic
def post_payment(payment, *, user) -> PaymentPostingResult:
    """Post one payment exactly once and place its value in an advance account.

    The cash/bank movement happens here. Later allocation batches only reclassify
    the advance to AR/AP, which is how PAY-005 applies an existing advance without
    producing a second cash movement.
    """
    locked = (
        Payment.objects.select_for_update(of=("self",))
        .select_related(
            "currency",
            "customer",
            "vendor",
            "money_account__gl_account",
        )
        .get(pk=payment.pk)
    )
    if locked.is_reversed or locked.status == DocumentStatus.REVERSED:
        raise ValidationError("A reversed payment cannot be posted.")
    if locked.status in {
        DocumentStatus.POSTED,
        DocumentStatus.PARTIAL,
        DocumentStatus.COMPLETED,
    }:
        if locked.journal_entry_id is None:
            raise ValidationError(
                f"{locked.number} is marked posted but has no journal entry. "
                "An accountant must repair this inconsistency before continuing."
            )
        return PaymentPostingResult(payment=locked, created=False)
    if locked.status not in {
        DocumentStatus.DRAFT,
        DocumentStatus.SUBMITTED,
        DocumentStatus.APPROVED,
    }:
        raise ValidationError(
            f"{locked.number} cannot be posted from status {locked.get_status_display()}."
        )
    if locked.allocated_txn != 0 or locked.unallocated_txn != locked.amount_txn:
        raise ValidationError("A payment must be fully unallocated before its first posting.")

    result = posting_engine.post(
        PostingRequest(
            source=locked,
            user=user,
            idempotency_key=f"payment:{locked.pk}:post:v1",
            build_journal=build_payment_journal,
            required_mappings=payment_required_mappings(locked),
            reason="Payment posting",
        )
    )
    locked.status = DocumentStatus.POSTED
    locked.journal_entry = result.journal_entry
    locked.posted_at = timezone.now()
    locked.posted_by = user
    locked.updated_by = user
    locked.save(
        update_fields=[
            "status",
            "journal_entry",
            "posted_at",
            "posted_by",
            "updated_by",
            "updated_at",
        ]
    )
    return PaymentPostingResult(payment=locked, created=result.created)
