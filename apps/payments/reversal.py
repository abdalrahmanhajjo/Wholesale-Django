"""Controlled reversal of allocations and payments (PAY-010, PAY-011).

Nothing here edits or deletes a posted journal — the ledger is append-only
(BR-004), so undoing a posting always means a second journal that cancels the
first and points back at it.

Reversal is ordered. An allocation may be reversed on its own, but a payment
may not be reversed while its money is still applied to something: the
allocations come off first, then the payment. That dependency is checked
against freshly locked rows, so it cannot be raced.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.core.models import DocumentStatus
from apps.ledger.services.posting import PostingEngine, PostingRequest
from apps.payments.allocation import ZERO, _open_base, _status_for_open_amount
from apps.payments.models import Allocation, Payment
from apps.payments.posting import make_reversing_journal_builder, mappings_used_by
from apps.purchases.models import PurchaseBill, VendorDebitNote
from apps.sales.models import SalesCreditNote, SalesInvoice

if TYPE_CHECKING:
    from django.contrib.auth.models import AbstractBaseUser

posting_engine = PostingEngine()

MAX_REASON = 255


@dataclass(frozen=True, slots=True)
class ReversalResult:
    source: object
    allocations: tuple[Allocation, ...]
    amount_txn: Decimal
    journal_entry: object | None


def _clean_reason(reason: str) -> str:
    reason = (reason or "").strip()
    if not reason:
        # PAY-010: the model constraint demands one, and an audit trail that
        # says only "reversed" is not an audit trail.
        raise ValidationError("Give a reason for the reversal.")
    if len(reason) > MAX_REASON:
        raise ValidationError(f"Keep the reason under {MAX_REASON} characters.")
    return reason


def _restore_target(target, *, field: str, amount: Decimal, user) -> None:
    """Give an invoice or bill its open balance back."""
    setattr(target, field, getattr(target, field) - amount)
    target.open_txn = target.total_txn - target.allocated_txn - target.credited_txn
    target.open_base = _open_base(target)
    target.status = _status_for_open_amount(
        open_txn=target.open_txn,
        settled_txn=target.allocated_txn + target.credited_txn,
    )
    target.updated_by = user
    target.updated_at = timezone.now()


@transaction.atomic
def reverse_allocation_batch(
    batch_key: UUID,
    *,
    user: AbstractBaseUser,
    reason: str,
    reversal_date: date | None = None,
) -> ReversalResult:
    """Un-apply every line of one allocation batch, as one accounting event."""
    reason = _clean_reason(reason)
    rows = list(
        Allocation.objects.select_for_update(of=("self",))
        .filter(batch_key=batch_key)
        .order_by("id")
    )
    if not rows:
        raise ValidationError("That allocation batch does not exist.")
    if any(row.is_reversed for row in rows):
        raise ValidationError("This allocation batch has already been reversed.")

    first = rows[0]
    total = sum((row.source_amount_txn for row in rows), ZERO)

    if first.payment_id is not None:
        source_model, source_id = Payment, first.payment_id
    elif first.sales_credit_note_id is not None:
        source_model, source_id = SalesCreditNote, first.sales_credit_note_id
    else:
        source_model, source_id = VendorDebitNote, first.vendor_debit_note_id
    source = source_model.objects.select_for_update(of=("self",)).get(pk=source_id)

    if first.sales_invoice_id is not None:
        target_model = SalesInvoice
        target_field = "sales_invoice_id"
    else:
        target_model = PurchaseBill
        target_field = "purchase_bill_id"
    target_ids = sorted(getattr(row, target_field) for row in rows)
    targets = {
        target.pk: target
        for target in target_model.objects.select_for_update(of=("self",))
        .filter(pk__in=target_ids)
        .order_by("pk")
    }
    if len(targets) != len(target_ids):
        raise ValidationError("A settled document no longer exists; cannot reverse.")

    # A payment allocation reduced allocated_txn; a credit application reduced
    # credited_txn. Put back whichever this batch consumed.
    column = "allocated_txn" if first.payment_id is not None else "credited_txn"
    for row in rows:
        target = targets[getattr(row, target_field)]
        _restore_target(target, field=column, amount=row.target_amount_txn, user=user)
    target_model.objects.bulk_update(
        targets.values(),
        ["allocated_txn", "credited_txn", "open_txn", "open_base", "status"],
    )

    journal = None
    if first.journal_entry_id is not None:
        journal = posting_engine.post(
            PostingRequest(
                source=source,
                user=user,
                idempotency_key=f"alloc-reversal:{batch_key}",
                build_journal=make_reversing_journal_builder(
                    first.journal_entry,
                    entry_date=reversal_date or timezone.localdate(),
                    reason=reason,
                ),
                required_mappings=mappings_used_by(first.journal_entry),
                reason="Allocation reversal",
            )
        ).journal_entry

    Allocation.objects.filter(pk__in=[row.pk for row in rows]).update(
        is_reversed=True, reversal_reason=reason, updated_by=user
    )

    source.allocated_txn -= total
    if isinstance(source, Payment):
        source.unallocated_txn = source.amount_txn - source.allocated_txn
        source.status = _status_for_open_amount(
            open_txn=source.unallocated_txn, settled_txn=source.allocated_txn
        )
        fields = ["allocated_txn", "unallocated_txn", "status", "updated_by"]
    else:
        source.open_txn = source.total_txn - source.allocated_txn - source.refunded_txn
        source.open_base = _open_base(source)
        source.status = _status_for_open_amount(
            open_txn=source.open_txn,
            settled_txn=source.allocated_txn + source.refunded_txn,
        )
        fields = ["allocated_txn", "open_txn", "open_base", "status", "updated_by"]
    source.updated_by = user
    source.save(update_fields=fields)

    for row in rows:
        row.is_reversed = True
        row.reversal_reason = reason
    return ReversalResult(
        source=source,
        allocations=tuple(rows),
        amount_txn=total,
        journal_entry=journal,
    )


@transaction.atomic
def reverse_payment(
    payment: Payment,
    *,
    user: AbstractBaseUser,
    reason: str,
    reversal_date: date | None = None,
) -> ReversalResult:
    """Reverse a posted payment, once nothing depends on it any more."""
    reason = _clean_reason(reason)
    locked = (
        Payment.objects.select_for_update(of=("self",))
        .select_related("currency", "customer", "vendor", "journal_entry")
        .get(pk=payment.pk)
    )

    if locked.is_reversed or locked.status == DocumentStatus.REVERSED:
        raise ValidationError(f"{locked.number} has already been reversed.")
    if locked.status == DocumentStatus.DRAFT or locked.journal_entry_id is None:
        raise ValidationError(
            f"{locked.number} is not posted, so there is nothing to reverse. "
            "Delete the draft instead."
        )

    # The dependency check. Reversing the cash while it is still applied would
    # leave invoices settled by money that no longer exists.
    live = list(
        Allocation.objects.filter(payment_id=locked.pk, is_reversed=False)
        .select_related("sales_invoice", "purchase_bill")
        .order_by("id")
    )
    if live:
        settled = ", ".join(
            sorted({row.target.number for row in live if row.target is not None})
        )
        raise ValidationError(
            f"{locked.number} is still applied to {settled}. Reverse the "
            "allocation first, then reverse the payment."
        )

    journal = posting_engine.post(
        PostingRequest(
            source=locked,
            user=user,
            idempotency_key=f"payment-reversal:{locked.pk}",
            build_journal=make_reversing_journal_builder(
                locked.journal_entry,
                entry_date=reversal_date or timezone.localdate(),
                reason=reason,
            ),
            required_mappings=mappings_used_by(locked.journal_entry),
            reason="Payment reversal",
        )
    ).journal_entry

    locked.is_reversed = True
    locked.reversed_at = timezone.now()
    locked.reversed_by = user
    locked.reversal_reason = reason
    locked.reversal_journal = journal
    locked.status = DocumentStatus.REVERSED
    # allocated_txn / unallocated_txn are left as they are. Nothing was applied
    # — the dependency check above guarantees it — so they already read zero and
    # the full amount, and payment_unallocated_is_derived requires they agree.
    locked.updated_by = user
    locked.save(
        update_fields=[
            "is_reversed",
            "reversed_at",
            "reversed_by",
            "reversal_reason",
            "reversal_journal",
            "status",
            "updated_by",
        ]
    )
    return ReversalResult(
        source=locked,
        allocations=(),
        amount_txn=locked.amount_txn,
        journal_entry=journal,
    )


def live_allocation_batches(payment: Payment):
    """Batches still applied, newest first — what the UI offers to reverse."""
    seen: dict[UUID, dict] = {}
    rows = (
        Allocation.objects.filter(payment_id=payment.pk, is_reversed=False)
        .select_related("sales_invoice", "purchase_bill")
        .order_by("-allocation_date", "-id")
    )
    for row in rows:
        entry = seen.setdefault(
            row.batch_key,
            {
                "batch_key": row.batch_key,
                "allocation_date": row.allocation_date,
                "amount_txn": ZERO,
                "fx_gain_loss_base": ZERO,
                "documents": [],
            },
        )
        entry["amount_txn"] += row.source_amount_txn
        entry["fx_gain_loss_base"] += row.fx_gain_loss_base
        if row.target is not None:
            entry["documents"].append(row.target.number)
    return list(seen.values())


__all__ = [
    "ReversalResult",
    "live_allocation_batches",
    "reverse_allocation_batch",
    "reverse_payment",
]
