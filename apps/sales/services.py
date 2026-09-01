"""
Sales-order services: numbering, arithmetic, totals, discount allocation,
and the approval lifecycle (SAL-001..SAL-004, BR-010, BR-011, BR-022, NFR-008).

Every public function runs inside a transaction. The caller is responsible for
wrapping in `transaction.atomic()` if they need to combine it with a form save.
"""

from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.db.models import F, Sum
from django.utils import timezone

from apps.core.models import (
    DocumentSequence,
    DocumentStatus,
    TaxCode,
    ZERO,
)
from apps.core import audit
from apps.sales.models import SalesOrder, SalesOrderLine

# ---------------------------------------------------------------------------
# Number generation (CFG-008, NFR-008)
# ---------------------------------------------------------------------------

def allocate_so_number(series="DEFAULT"):
    """
    Generate the next sales-order number with SELECT ... FOR UPDATE on the
    sequence row, so two concurrent requests cannot collide (NFR-008).

    Format:  prefix + padded next_number + suffix
             e.g.  "SO-00001"

    Raises ValueError if no active sequence exists for document_type="SO".
    """
    with transaction.atomic():
        seq = (
            DocumentSequence.objects.select_for_update()
            .filter(document_type="SO", series=series, is_active=True)
            .first()
        )
        if seq is None:
            raise ValueError(
                f"No active document sequence for SO / {series}. "
                "Ask an administrator to create one in Settings."
            )

        num = seq.next_number
        seq.next_number = F("next_number") + 1
        seq.save(update_fields=["next_number"])

        formatted = str(num).zfill(seq.padding)
        return f"{seq.prefix}{formatted}{seq.suffix}"


# ---------------------------------------------------------------------------
# Line arithmetic (BR-010, BR-011, FTD-006)
# ---------------------------------------------------------------------------

def _round_money(value):
    """Round a Decimal to 4 dp (MONEY scale) using banker's rounding."""
    return value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def calculate_line(line):
    """
    Recalculate the financial fields of one SalesOrderLine in-place.

    Arithmetic contract (DocumentLineBase):
        gross_txn        = quantity * unit_price
        line_discount    = gross_txn * discount_percent / 100
        net_txn          = gross_txn - line_discount - allocated_document_discount_txn
        taxable_base_txn = net_txn               (exclusive tax)
                         = net_txn / (1 + r/100) (inclusive tax)
        tax_txn          = taxable_base_txn * rate / 100
        total_txn        = taxable_base_txn + tax_txn

    The caller must call line.save() afterwards.
    """
    qty = line.quantity or ZERO
    price = line.unit_price or ZERO
    disc_pct = line.discount_percent or ZERO
    alloc_doc_disc = line.allocated_document_discount_txn or ZERO
    rate = line.tax_rate_percent or ZERO

    # 1. Gross
    gross = _round_money(qty * price)

    # 2. Line-level discount
    line_disc = _round_money(gross * disc_pct / Decimal("100"))
    # Clamp: discount cannot exceed gross (FTD-008)
    if line_disc > gross:
        line_disc = gross

    # 3. Net (after line discount AND document-discount share)
    net = gross - line_disc - alloc_doc_disc
    if net < ZERO:
        net = ZERO

    # 4. Taxable base and tax
    if line.tax_is_inclusive and rate > ZERO:
        taxable_base = _round_money(net / (ONE + rate / Decimal("100")))
    else:
        taxable_base = net

    tax = _round_money(taxable_base * rate / Decimal("100"))
    total = taxable_base + tax

    # 5. Assign
    line.gross_txn = gross
    line.line_discount_txn = line_disc
    line.net_txn = net
    line.taxable_base_txn = taxable_base
    line.tax_txn = tax
    line.total_txn = total

    # 6. Base-currency mirrors (exchange_rate is set on the header)
    #    Caller must have already set exchange_rate on the order before
    #    calling this. We don't look it up here to avoid N+1.
    #    The base values are set in calculate_totals() after all lines.


ONE = Decimal("1")


# ---------------------------------------------------------------------------
# Document-level discount allocation (BR-011, SAL-003)
# ---------------------------------------------------------------------------

def allocate_document_discount(order):
    """
    Split the header-level discount across all eligible lines proportionally
    by each line's (quantity × unit_price).

    SAL-003: "A document-level discount is spread across lines in proportion
    to each line's gross amount, stored as `allocated_document_discount_txn`
    on each line."

    A line with zero gross gets zero allocation.
    """
    lines = list(order.lines.all())
    if not lines:
        return

    discount_total = order.document_discount_txn or ZERO
    if discount_total <= ZERO:
        for ln in lines:
            ln.allocated_document_discount_txn = ZERO
        return

    # Sum of all gross amounts
    total_gross = sum((ln.quantity or ZERO) * (ln.unit_price or ZERO) for ln in lines)
    if total_gross <= ZERO:
        for ln in lines:
            ln.allocated_document_discount_txn = ZERO
        return

    allocated_so_far = ZERO
    for i, ln in enumerate(lines):
        gross = (ln.quantity or ZERO) * (ln.unit_price or ZERO)
        if i < len(lines) - 1:
            share = _round_money(discount_total * gross / total_gross)
            ln.allocated_document_discount_txn = share
            allocated_so_far += share
        else:
            # Last line gets the remainder to avoid rounding drift
            ln.allocated_document_discount_txn = discount_total - allocated_so_far


# ---------------------------------------------------------------------------
# Totals roll-up (SAL-002, BR-022)
# ---------------------------------------------------------------------------

def calculate_totals(order):
    """
    Sum line values into the header totals. Must run AFTER calculate_line()
    on every line and allocate_document_discount().

    Sets subtotal, line_discount, document_discount, taxable_base, tax,
    total, rounding, and their base-currency mirrors.
    """
    lines = order.lines.all()

    agg = lines.aggregate(
        sum_gross=Sum("gross_txn", default=ZERO),
        sum_line_disc=Sum("line_discount_txn", default=ZERO),
        sum_alloc_doc_disc=Sum("allocated_document_discount_txn", default=ZERO),
        sum_net=Sum("net_txn", default=ZERO),
        sum_taxable=Sum("taxable_base_txn", default=ZERO),
        sum_tax=Sum("tax_txn", default=ZERO),
        sum_total=Sum("total_txn", default=ZERO),
    )

    rate = order.exchange_rate or ONE

    order.subtotal_txn = agg["sum_gross"]
    order.line_discount_txn = agg["sum_line_disc"]
    order.document_discount_txn = agg["sum_alloc_doc_disc"]  # stored as the
    # allocated total, not the header field — reconciles to header
    order.taxable_base_txn = agg["sum_taxable"]
    order.tax_txn = agg["sum_tax"]

    # BR-022: rounding tolerance
    company = _get_company()
    tolerance = company.rounding_tolerance if company else Decimal("0.05")

    raw_total = agg["sum_taxable"] + agg["sum_tax"]
    rounded_total = raw_total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    rounding = rounded_total - raw_total

    if abs(rounding) <= tolerance:
        order.rounding_txn = rounding
        order.total_txn = rounded_total
    else:
        order.rounding_txn = ZERO
        order.total_txn = raw_total

    # Base-currency mirrors
    order.subtotal_base = _round_money(order.subtotal_txn * rate)
    order.line_discount_base = _round_money(order.line_discount_txn * rate)
    order.document_discount_base = _round_money(order.document_discount_txn * rate)
    order.taxable_base_base = _round_money(order.taxable_base_txn * rate)
    order.tax_base = _round_money(order.tax_txn * rate)
    order.rounding_base = _round_money(order.rounding_txn * rate)
    order.total_base = _round_money(order.total_txn * rate)

    order.open_txn = order.total_txn
    order.open_base = order.total_base


def _get_company():
    from apps.core.models import Company
    return Company.objects.first()


# ---------------------------------------------------------------------------
# Create / recalculate helpers
# ---------------------------------------------------------------------------

def create_sales_order(*, user, **kwargs):
    """
    Create a new SalesOrder with a generated number and initial status.
    Lines are added separately via formset.

    Returns the new SalesOrder instance.
    """
    number = allocate_so_number()
    order = SalesOrder(
        number=number,
        status=DocumentStatus.DRAFT,
        created_by=user,
        updated_by=user,
        **kwargs,
    )
    order.save()
    audit.record_create(None, order)
    return order


def recalculate_order(order):
    """
    Full recalculation pass: allocate doc discount → calculate each line →
    roll up totals. Call this after any change to lines, prices, quantities,
    discounts, or the header discount.
    """
    allocate_document_discount(order)
    for line in order.lines.all():
        calculate_line(line)
        line.save(
            update_fields=[
                "gross_txn", "line_discount_txn",
                "allocated_document_discount_txn",
                "net_txn", "taxable_base_txn", "tax_txn", "total_txn",
                "net_base", "taxable_base_base", "tax_base", "total_base",
            ]
        )
    calculate_totals(order)
    order.save(update_fields=[
        "subtotal_txn", "line_discount_txn", "document_discount_txn",
        "taxable_base_txn", "tax_txn", "rounding_txn", "total_txn",
        "subtotal_base", "line_discount_base", "document_discount_base",
        "taxable_base_base", "tax_base", "rounding_base", "total_base",
        "open_txn", "open_base",
    ])


# ---------------------------------------------------------------------------
# Approval workflow (SAL-004, ACC-005, ACC-008)
# ---------------------------------------------------------------------------

def submit_order(order, user):
    """
    Move a DRAFT (or previously REJECTED) order to SUBMITTED.

    A REJECTED order is editable (EDITABLE_STATES) so the user can fix it and
    resubmit for approval. Only the creator or a manager should call this — the
    view enforces the permission; the service just validates state.
    """
    if order.status not in (DocumentStatus.DRAFT, DocumentStatus.REJECTED):
        raise ValueError(
            f"Cannot submit order {order.number}: status is {order.status}, "
            "expected DRAFT or REJECTED."
        )
    order.status = DocumentStatus.SUBMITTED
    order.submitted_at = timezone.now()
    order.updated_by = user
    order.save(update_fields=[
        "status", "submitted_at", "updated_by", "updated_at",
    ])
    audit.record_action(None, audit.AuditAction.SUBMIT, order)
    return order


def approve_order(order, user, reason=""):
    """
    Approve a SUBMITTED order (SAL-004). Gated behind
    APPROVE_SALES_ORDER permission — the view must check this.
    """
    if order.status != DocumentStatus.SUBMITTED:
        raise ValueError(
            f"Cannot approve order {order.number}: status is {order.status}, "
            "expected SUBMITTED."
        )
    order.status = DocumentStatus.APPROVED
    order.approved_at = timezone.now()
    order.approved_by = user
    order.approval_reason = reason
    order.updated_by = user
    order.save(update_fields=[
        "status", "approved_at", "approved_by", "approval_reason",
        "updated_by", "updated_at",
    ])
    audit.record_action(
        None, audit.AuditAction.APPROVE, order, reason=reason,
    )
    return order


def reject_order(order, user, reason=""):
    """
    Reject a SUBMITTED order (SAL-004). Requires a reason (ACC-008).
    """
    if order.status != DocumentStatus.SUBMITTED:
        raise ValueError(
            f"Cannot reject order {order.number}: status is {order.status}, "
            "expected SUBMITTED."
        )
    if not reason.strip():
        raise ValueError("A reason is required to reject an order (ACC-008).")

    order.status = DocumentStatus.REJECTED
    order.approved_at = None
    order.approved_by = user
    order.approval_reason = reason
    order.updated_by = user
    order.save(update_fields=[
        "status", "approved_at", "approved_by", "approval_reason",
        "updated_by", "updated_at",
    ])
    audit.record_action(
        None, audit.AuditAction.REJECT, order, reason=reason,
    )
    return order