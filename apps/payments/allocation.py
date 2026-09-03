"""Atomic payment and credit allocation engine (PAY-003..PAY-007).

The engine owns every settlement invariant. Views only collect intent; this
module locks the source and targets, validates the request again against fresh
rows, writes allocations, updates all denormalised balances, and creates any
required advance-reclassification journal in one transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.core.models import DocumentStatus
from apps.ledger.services.posting import PostingEngine, PostingRequest
from apps.payments.models import Allocation, Payment, PaymentDirection
from apps.payments.posting import (
    allocation_required_mappings,
    make_allocation_journal_builder,
)
from apps.purchases.models import PurchaseBill, VendorDebitNote
from apps.sales.models import SalesCreditNote, SalesInvoice

if TYPE_CHECKING:
    from django.contrib.auth.models import AbstractBaseUser
    from django.db.models import Model, QuerySet

MONEY_QUANTUM = Decimal("0.0001")
ZERO = Decimal("0")
ALLOCATABLE_STATUSES = frozenset({DocumentStatus.POSTED, DocumentStatus.PARTIAL})
posting_engine = PostingEngine()


@dataclass(frozen=True, slots=True)
class AllocationLineInput:
    target_id: int
    amount_txn: Decimal


@dataclass(frozen=True, slots=True)
class AllocationBatchResult:
    source: Model
    allocations: tuple[Allocation, ...]
    amount_txn: Decimal
    remaining_txn: Decimal
    created: bool


def _money(value: Decimal, *, label: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValidationError(f"{label} must be a finite Decimal amount.")
    quantized = value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    if quantized != value:
        raise ValidationError(f"{label} cannot have more than four decimal places.")
    if quantized <= ZERO:
        raise ValidationError(f"{label} must be greater than zero.")
    return quantized


def _normalise_lines(lines) -> tuple[AllocationLineInput, ...]:
    normalised = []
    target_ids = set()
    for position, line in enumerate(lines, start=1):
        if not isinstance(line, AllocationLineInput):
            raise ValidationError(f"Allocation line {position} has an invalid shape.")
        if not isinstance(line.target_id, int) or line.target_id <= 0:
            raise ValidationError(f"Allocation line {position} has an invalid target.")
        if line.target_id in target_ids:
            raise ValidationError("Each open document may appear only once in a batch.")
        target_ids.add(line.target_id)
        normalised.append(
            AllocationLineInput(
                target_id=line.target_id,
                amount_txn=_money(line.amount_txn, label=f"Allocation line {position}"),
            )
        )
    if not normalised:
        raise ValidationError("Enter an amount for at least one open document.")
    return tuple(normalised)


def _validate_batch_key(batch_key: UUID) -> UUID:
    if not isinstance(batch_key, UUID):
        raise ValidationError("The allocation request key is invalid. Refresh and try again.")
    return batch_key


def _status_for_open_amount(*, open_txn: Decimal, settled_txn: Decimal) -> str:
    if open_txn == ZERO:
        return DocumentStatus.COMPLETED
    if settled_txn > ZERO:
        return DocumentStatus.PARTIAL
    return DocumentStatus.POSTED


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def _signed_fx(payment: Payment, difference: Decimal) -> Decimal:
    """Express a rate difference as a gain (positive) or loss (negative).

    ``difference`` is the money's carrying value minus the document's. Taking
    in more base currency than the receivable was booked at is a gain; paying
    out more than the payable was booked at is a loss — so the sign flips with
    the direction of the payment.
    """
    if payment.direction == PaymentDirection.RECEIPT:
        return difference
    return -difference


def _open_base(document) -> Decimal:
    return (document.open_txn * document.exchange_rate).quantize(
        MONEY_QUANTUM, rounding=ROUND_HALF_UP
    )


def _existing_batch(
    *,
    batch_key: UUID,
    source_field: str,
    source_id: int,
    target_field: str,
    lines: tuple[AllocationLineInput, ...],
    allocation_date: date,
    source,
    remaining_txn: Decimal,
) -> AllocationBatchResult | None:
    existing = tuple(
        Allocation.objects.filter(batch_key=batch_key)
        .select_related(
            "payment",
            "sales_credit_note",
            "vendor_debit_note",
            "sales_invoice",
            "purchase_bill",
        )
        .order_by("id")
    )
    if not existing:
        return None
    if any(item.is_reversed for item in existing):
        raise ValidationError("This allocation request key belongs to a reversed batch.")
    if any(getattr(item, f"{source_field}_id") != source_id for item in existing):
        raise ValidationError("This allocation request key has already been used.")
    expected = {line.target_id: line.amount_txn for line in lines}
    actual = {getattr(item, f"{target_field}_id"): item.target_amount_txn for item in existing}
    if (
        expected != actual
        or any(item.allocation_date != allocation_date for item in existing)
        or len(existing) != len(lines)
    ):
        raise ValidationError(
            "This allocation request was already submitted with different values. "
            "Refresh the workspace before trying again."
        )
    return AllocationBatchResult(
        source=source,
        allocations=existing,
        amount_txn=sum((item.source_amount_txn for item in existing), ZERO),
        remaining_txn=remaining_txn,
        created=False,
    )


def _lock_targets(queryset: QuerySet, target_ids: tuple[int, ...]) -> list:
    """Lock every target row in primary-key order to prevent allocation deadlocks.

    ``of=("self",)`` keeps the lock on the documents themselves. Without it the
    join would also lock the shared currency row, so two allocations touching
    unrelated invoices in the same currency would serialise on it — and Postgres
    refuses ``FOR UPDATE`` against the nullable side of an outer join outright.
    """
    targets = list(
        queryset.select_for_update(of=("self",))
        .select_related("currency")
        .filter(pk__in=target_ids)
        .order_by("pk")
    )
    if len(targets) != len(target_ids):
        raise ValidationError(
            "One or more selected open documents no longer exist. Refresh and try again."
        )
    return targets


def _validate_target(
    *, source, target, amount: Decimal, party_field: str, allocation_date: date
) -> None:
    if target.status not in ALLOCATABLE_STATUSES or target.is_reversed:
        raise ValidationError(
            f"{target.number} is no longer an open posted document. Refresh and try again."
        )
    if target.journal_entry_id is None:
        raise ValidationError(
            f"{target.number} has no posting journal and cannot be settled safely."
        )
    if getattr(target, f"{party_field}_id") != getattr(source, f"{party_field}_id"):
        raise ValidationError(f"{target.number} belongs to a different party.")
    if target.currency_id != source.currency_id:
        # Settling a USD invoice with EUR cash needs a rate between two
        # non-base currencies, which nothing in the system holds. A differing
        # *rate* on the same currency is the ordinary case and is handled.
        raise ValidationError(
            f"{target.number} is in a different currency from {source.number}. "
            "Settle it with money in its own currency."
        )
    if allocation_date < target.document_date:
        raise ValidationError(
            f"The allocation date cannot be earlier than {target.number}'s document date."
        )
    if amount > target.open_txn:
        raise ValidationError(
            f"{target.number} has only {target.open_txn:,.4f} open; "
            f"{amount:,.4f} was requested."
        )


def available_payment_targets(payment: Payment):
    """Return the open documents this payment may settle.

    Same currency, not same rate: a document booked at a different rate settles
    perfectly well and simply realises an exchange difference.
    """
    common = {
        "open_txn__gt": ZERO,
        "status__in": ALLOCATABLE_STATUSES,
        "currency_id": payment.currency_id,
        "is_reversed": False,
    }
    if payment.direction == PaymentDirection.RECEIPT:
        return (
            SalesInvoice.objects.filter(customer_id=payment.customer_id, **common)
            .select_related("currency")
            .order_by("due_date", "document_date", "id")
        )
    return (
        PurchaseBill.objects.filter(vendor_id=payment.vendor_id, **common)
        .select_related("currency")
        .order_by("due_date", "document_date", "id")
    )


@transaction.atomic
def allocate_payment(
    payment: Payment,
    *,
    lines,
    allocation_date: date,
    user: AbstractBaseUser,
    batch_key: UUID,
) -> AllocationBatchResult:
    """Allocate a posted advance across one or many invoices/bills atomically."""
    lines = _normalise_lines(lines)
    batch_key = _validate_batch_key(batch_key)
    locked = (
        Payment.objects.select_for_update(of=("self",))
        .select_related("currency", "customer", "vendor")
        .get(pk=payment.pk)
    )

    existing = _existing_batch(
        batch_key=batch_key,
        source_field="payment",
        source_id=locked.pk,
        target_field=(
            "sales_invoice"
            if locked.direction == PaymentDirection.RECEIPT
            else "purchase_bill"
        ),
        lines=lines,
        allocation_date=allocation_date,
        source=locked,
        remaining_txn=locked.unallocated_txn,
    )
    if existing is not None:
        return existing

    if locked.status not in ALLOCATABLE_STATUSES or locked.is_reversed:
        raise ValidationError(
            "Only an active posted payment with credit remaining can be allocated."
        )
    if locked.journal_entry_id is None:
        raise ValidationError("Post the payment before allocating it.")
    if allocation_date < locked.payment_date:
        raise ValidationError("The allocation date cannot be earlier than the payment date.")

    total = sum((line.amount_txn for line in lines), ZERO)
    if total > locked.unallocated_txn:
        raise ValidationError(
            f"This payment has only {locked.unallocated_txn:,.4f} available; "
            f"{total:,.4f} was requested."
        )

    line_by_target = {line.target_id: line for line in lines}
    target_ids = tuple(sorted(line_by_target))
    if locked.direction == PaymentDirection.RECEIPT:
        party_field = "customer"
        target_field = "sales_invoice"
        target_type = "SALES_INVOICE"
        targets = _lock_targets(SalesInvoice.objects, target_ids)
    else:
        party_field = "vendor"
        target_field = "purchase_bill"
        target_type = "PURCHASE_BILL"
        targets = _lock_targets(PurchaseBill.objects, target_ids)

    for target in targets:
        _validate_target(
            source=locked,
            target=target,
            amount=line_by_target[target.pk].amount_txn,
            party_field=party_field,
            allocation_date=allocation_date,
        )

    # Value the settled amount twice: once at the money's rate, once at each
    # document's own. Where those disagree the difference is realised FX.
    source_base = _quantize(total * locked.exchange_rate)
    fx_by_target = {}
    target_base = ZERO
    for target in targets:
        amount = line_by_target[target.pk].amount_txn
        at_source = _quantize(amount * locked.exchange_rate)
        at_target = _quantize(amount * target.exchange_rate)
        target_base += at_target
        fx_by_target[target.pk] = _signed_fx(locked, at_source - at_target)
    difference = source_base - target_base

    posting_result = posting_engine.post(
        PostingRequest(
            source=locked,
            user=user,
            idempotency_key=f"payment:{locked.pk}:allocation:{batch_key}",
            build_journal=make_allocation_journal_builder(
                allocation_date=allocation_date,
                amount_txn=total,
                source_base=source_base,
                target_base=target_base,
            ),
            required_mappings=allocation_required_mappings(
                locked, fx_difference_base=difference
            ),
            reason="Payment allocation",
        )
    )

    allocation_rows = []
    for target in targets:
        amount = line_by_target[target.pk].amount_txn
        row_data = {
            "allocation_date": allocation_date,
            "batch_key": batch_key,
            "party_side": (
                "CUSTOMER" if locked.direction == PaymentDirection.RECEIPT else "VENDOR"
            ),
            "source_type": "PAYMENT",
            "target_type": target_type,
            "payment": locked,
            "source_amount_txn": amount,
            "target_amount_txn": amount,
            "amount_base": _quantize(amount * locked.exchange_rate),
            "settlement_rate": locked.exchange_rate,
            "fx_gain_loss_base": fx_by_target[target.pk],
            # The exchange difference cannot be posted apart from the
            # reclassification it balances, so both point at the one journal.
            "fx_journal_entry": (
                posting_result.journal_entry if fx_by_target[target.pk] else None
            ),
            "journal_entry": posting_result.journal_entry,
            "created_by": user,
            "updated_by": user,
            party_field: getattr(locked, party_field),
            target_field: target,
        }
        allocation_rows.append(Allocation(**row_data))
    allocations = tuple(Allocation.objects.bulk_create(allocation_rows))

    now = timezone.now()
    for target in targets:
        amount = line_by_target[target.pk].amount_txn
        target.allocated_txn += amount
        target.open_txn = target.total_txn - target.allocated_txn - target.credited_txn
        target.open_base = _open_base(target)
        target.status = _status_for_open_amount(
            open_txn=target.open_txn,
            settled_txn=target.allocated_txn + target.credited_txn,
        )
        target.updated_by = user
        target.updated_at = now
    type(targets[0]).objects.bulk_update(
        targets,
        ["allocated_txn", "open_txn", "open_base", "status", "updated_by", "updated_at"],
    )

    locked.allocated_txn += total
    locked.unallocated_txn = locked.amount_txn - locked.allocated_txn
    locked.status = _status_for_open_amount(
        open_txn=locked.unallocated_txn, settled_txn=locked.allocated_txn
    )
    locked.updated_by = user
    locked.save(
        update_fields=[
            "allocated_txn",
            "unallocated_txn",
            "status",
            "updated_by",
            "updated_at",
        ]
    )
    return AllocationBatchResult(
        source=locked,
        allocations=allocations,
        amount_txn=total,
        remaining_txn=locked.unallocated_txn,
        created=True,
    )


def _allocate_credit(
    source,
    *,
    lines,
    allocation_date: date,
    user: AbstractBaseUser,
    batch_key: UUID,
    source_model,
    source_field: str,
    source_type: str,
    target_model,
    target_field: str,
    target_type: str,
    party_field: str,
) -> AllocationBatchResult:
    lines = _normalise_lines(lines)
    batch_key = _validate_batch_key(batch_key)
    locked = (
        source_model.objects.select_for_update(of=("self",))
        .select_related("currency", party_field)
        .get(pk=source.pk)
    )
    existing = _existing_batch(
        batch_key=batch_key,
        source_field=source_field,
        source_id=locked.pk,
        target_field=target_field,
        lines=lines,
        allocation_date=allocation_date,
        source=locked,
        remaining_txn=locked.open_txn,
    )
    if existing is not None:
        return existing
    if locked.status not in ALLOCATABLE_STATUSES or locked.is_reversed:
        raise ValidationError(
            "Only an active posted credit with value remaining can be allocated."
        )
    if locked.journal_entry_id is None:
        raise ValidationError("Post the credit before allocating it.")
    if allocation_date < locked.document_date:
        raise ValidationError("The allocation date cannot be earlier than the credit date.")

    total = sum((line.amount_txn for line in lines), ZERO)
    if total > locked.open_txn:
        raise ValidationError(
            f"This credit has only {locked.open_txn:,.4f} available; {total:,.4f} was requested."
        )

    line_by_target = {line.target_id: line for line in lines}
    targets = _lock_targets(target_model.objects, tuple(sorted(line_by_target)))
    for target in targets:
        _validate_target(
            source=locked,
            target=target,
            amount=line_by_target[target.pk].amount_txn,
            party_field=party_field,
            allocation_date=allocation_date,
        )

    allocation_rows = []
    for target in targets:
        amount = line_by_target[target.pk].amount_txn
        row_data = {
            "allocation_date": allocation_date,
            "batch_key": batch_key,
            "party_side": "CUSTOMER" if party_field == "customer" else "VENDOR",
            "source_type": source_type,
            "target_type": target_type,
            source_field: locked,
            target_field: target,
            party_field: getattr(locked, party_field),
            "source_amount_txn": amount,
            "target_amount_txn": amount,
            "amount_base": (amount * locked.exchange_rate).quantize(
                MONEY_QUANTUM, rounding=ROUND_HALF_UP
            ),
            "settlement_rate": locked.exchange_rate,
            "created_by": user,
            "updated_by": user,
        }
        allocation_rows.append(Allocation(**row_data))
    allocations = tuple(Allocation.objects.bulk_create(allocation_rows))

    now = timezone.now()
    for target in targets:
        target.credited_txn += line_by_target[target.pk].amount_txn
        target.open_txn = target.total_txn - target.allocated_txn - target.credited_txn
        target.open_base = _open_base(target)
        target.status = _status_for_open_amount(
            open_txn=target.open_txn,
            settled_txn=target.allocated_txn + target.credited_txn,
        )
        target.updated_by = user
        target.updated_at = now
    target_model.objects.bulk_update(
        targets,
        ["credited_txn", "open_txn", "open_base", "status", "updated_by", "updated_at"],
    )

    locked.allocated_txn += total
    locked.open_txn = locked.total_txn - locked.allocated_txn - locked.refunded_txn
    locked.open_base = _open_base(locked)
    locked.status = _status_for_open_amount(
        open_txn=locked.open_txn,
        settled_txn=locked.allocated_txn + locked.refunded_txn,
    )
    locked.updated_by = user
    locked.save(
        update_fields=[
            "allocated_txn",
            "open_txn",
            "open_base",
            "status",
            "updated_by",
            "updated_at",
        ]
    )
    return AllocationBatchResult(
        source=locked,
        allocations=allocations,
        amount_txn=total,
        remaining_txn=locked.open_txn,
        created=True,
    )


@transaction.atomic
def allocate_sales_credit(
    credit_note: SalesCreditNote,
    *,
    lines,
    allocation_date: date,
    user: AbstractBaseUser,
    batch_key: UUID,
) -> AllocationBatchResult:
    """Apply one customer credit to one or many sales invoices without cash."""
    return _allocate_credit(
        credit_note,
        lines=lines,
        allocation_date=allocation_date,
        user=user,
        batch_key=batch_key,
        source_model=SalesCreditNote,
        source_field="sales_credit_note",
        source_type="SALES_CREDIT_NOTE",
        target_model=SalesInvoice,
        target_field="sales_invoice",
        target_type="SALES_INVOICE",
        party_field="customer",
    )


@transaction.atomic
def allocate_vendor_credit(
    debit_note: VendorDebitNote,
    *,
    lines,
    allocation_date: date,
    user: AbstractBaseUser,
    batch_key: UUID,
) -> AllocationBatchResult:
    """Apply one vendor credit to one or many purchase bills without cash."""
    return _allocate_credit(
        debit_note,
        lines=lines,
        allocation_date=allocation_date,
        user=user,
        batch_key=batch_key,
        source_model=VendorDebitNote,
        source_field="vendor_debit_note",
        source_type="VENDOR_DEBIT_NOTE",
        target_model=PurchaseBill,
        target_field="purchase_bill",
        target_type="PURCHASE_BILL",
        party_field="vendor",
    )
