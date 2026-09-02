"""Transactional creation and update services for payment entry."""

from decimal import ROUND_HALF_UP, Decimal

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.core.models import (
    DocumentSequence,
    DocumentStatus,
    DocumentType,
    FiscalPeriod,
    PeriodStatus,
    SequenceReset,
)
from apps.payments.models import Payment, PaymentDirection

MONEY_QUANTUM = Decimal("0.0001")


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
