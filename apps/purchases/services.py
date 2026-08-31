"""
Purchase order business logic: document numbering, line/header totals, and the
PUR-002 approval workflow.

Kept out of views.py and forms.py on purpose (CONTRIBUTING.md §4): a view
should read as "check permission, validate the form, call a service, render".

BRD coverage: PUR-001, PUR-002, BR-003, BR-005, BR-010, BR-011, BR-012,
CFG-008, CFG-010, NFR-008.
"""

from decimal import ROUND_HALF_UP, Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.core import audit
from apps.core.models import (
    AuditAction,
    Company,
    DocumentSequence,
    DocumentStatus,
    DocumentType,
)

ZERO = Decimal("0")
HUNDRED = Decimal("100")
#: MONEY columns are 18,4 (apps/core/models.py) — every derived amount is
#: rounded to that scale so what is displayed is what is stored and summed.
MONEY_QUANT = Decimal("0.0001")


def _money(value):
    return (value or ZERO).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def _period_key(reset_policy, on_date):
    if reset_policy == "YEARLY":
        return str(on_date.year)
    if reset_policy == "MONTHLY":
        return f"{on_date.year}{on_date.month:02d}"
    return ""


# ---------------------------------------------------------------------------
# Numbering (CFG-008, BR-003, NFR-008)
# ---------------------------------------------------------------------------
def allocate_po_number(document_date):
    """
    Reserve the next purchase-order number.

    Takes SELECT ... FOR UPDATE on the sequence row so two purchasing clerks
    saving at the same moment cannot be handed the same number (NFR-008).
    Must be called from inside the caller's `transaction.atomic()` so the
    reservation and the order it is stamped onto commit or roll back together.
    """
    sequence = DocumentSequence.objects.select_for_update().get(
        document_type=DocumentType.PURCHASE_ORDER, series="DEFAULT"
    )
    key = _period_key(sequence.reset_policy, document_date)
    if sequence.reset_policy != "NEVER" and sequence.period_key != key:
        sequence.next_number = 1
        sequence.period_key = key
    allocated = sequence.next_number
    sequence.next_number += 1
    sequence.save(update_fields=["next_number", "period_key"])
    return f"{sequence.prefix}{allocated:0{sequence.padding}d}{sequence.suffix}"


# ---------------------------------------------------------------------------
# Totals (BR-010, BR-011, BR-012 arithmetic contract)
# ---------------------------------------------------------------------------
def _recalculate_line(line):
    """Derive one line's amounts from quantity, price, discount and tax code."""
    tax_code = line.tax_code
    line.tax_rate_percent = tax_code.rate_percent if tax_code else ZERO
    line.tax_is_inclusive = tax_code.is_inclusive if tax_code else False
    line.tax_is_recoverable = tax_code.is_recoverable if tax_code else True

    gross = (line.quantity or ZERO) * (line.unit_price or ZERO)
    discount = _money(gross * (line.discount_percent or ZERO) / HUNDRED)
    net = gross - discount - (line.allocated_document_discount_txn or ZERO)

    rate = line.tax_rate_percent or ZERO
    if line.tax_is_inclusive and rate:
        taxable_base = _money(net / (Decimal("1") + rate / HUNDRED))
    else:
        taxable_base = _money(net)
    tax = _money(taxable_base * rate / HUNDRED)

    line.gross_txn = _money(gross)
    line.line_discount_txn = discount
    line.net_txn = _money(net)
    line.taxable_base_txn = taxable_base
    line.tax_txn = tax
    line.total_txn = taxable_base + tax
    return line


def recalculate_order(order):
    """
    Recompute every line, allocate the header discount across them (BR-011),
    and roll the results up into the header totals.

    Call this once, after the line formset has been saved, inside the same
    `transaction.atomic()` as the rest of the save (BR-005).
    """
    lines = list(order.lines.all())

    # First pass at gross-less-line-discount, to weight the header discount.
    pre_discount_net = []
    for line in lines:
        gross = (line.quantity or ZERO) * (line.unit_price or ZERO)
        line_discount = _money(gross * (line.discount_percent or ZERO) / HUNDRED)
        pre_discount_net.append(gross - line_discount)
    subtotal_before_header_discount = sum(pre_discount_net, ZERO)

    if order.document_discount_kind == "PERCENT":
        header_discount = _money(
            subtotal_before_header_discount * (order.document_discount_value or ZERO) / HUNDRED
        )
    elif order.document_discount_kind == "AMOUNT":
        header_discount = _money(order.document_discount_value or ZERO)
    else:
        header_discount = ZERO
    header_discount = min(header_discount, subtotal_before_header_discount)

    allocated_so_far = ZERO
    subtotal_txn = line_discount_txn = taxable_base_txn = tax_txn = ZERO
    last_index = len(lines) - 1
    for index, (line, net_before_header) in enumerate(zip(lines, pre_discount_net)):
        if subtotal_before_header_discount > ZERO:
            share = _money(
                header_discount * net_before_header / subtotal_before_header_discount
            )
        else:
            share = ZERO
        if index == last_index:
            # The last line absorbs the rounding remainder so shares foot exactly.
            share = header_discount - allocated_so_far
        allocated_so_far += share
        line.allocated_document_discount_txn = share

        _recalculate_line(line)
        line.save()

        subtotal_txn += line.gross_txn
        line_discount_txn += line.line_discount_txn
        taxable_base_txn += line.taxable_base_txn
        tax_txn += line.tax_txn

    order.subtotal_txn = subtotal_txn
    order.line_discount_txn = line_discount_txn
    order.document_discount_txn = header_discount
    order.taxable_base_txn = taxable_base_txn
    order.tax_txn = tax_txn
    order.total_txn = taxable_base_txn + tax_txn

    rate = order.exchange_rate or Decimal("1")
    order.subtotal_base = _money(order.subtotal_txn * rate)
    order.line_discount_base = _money(order.line_discount_txn * rate)
    order.document_discount_base = _money(order.document_discount_txn * rate)
    order.taxable_base_base = _money(order.taxable_base_txn * rate)
    order.tax_base = _money(order.tax_txn * rate)
    order.total_base = _money(order.total_txn * rate)
    order.save()
    return order


# ---------------------------------------------------------------------------
# Approval workflow (PUR-002)
# ---------------------------------------------------------------------------
_HEADER_FIELDS = [
    "status",
    "submitted_at",
    "approved_at",
    "approved_by",
    "approval_reason",
    "updated_by",
    "updated_at",
]


def submit_purchase_order(order, user, request):
    """
    DRAFT/REJECTED -> SUBMITTED for sign-off, or straight to APPROVED when the
    company does not require one (CFG-010 `require_po_approval`).
    """
    if order.status not in (DocumentStatus.DRAFT, DocumentStatus.REJECTED):
        raise ValidationError("Only a draft or rejected order can be submitted.")
    if not order.lines.exists():
        raise ValidationError("Add at least one line before submitting.")

    company = Company.objects.first()
    requires_approval = company is None or company.require_po_approval

    with transaction.atomic():
        order.status = DocumentStatus.SUBMITTED
        order.submitted_at = timezone.now()
        order.approval_reason = ""
        order.updated_by = user
        order.save(update_fields=_HEADER_FIELDS)
        audit.record_action(request, AuditAction.SUBMIT, order)

        if not requires_approval:
            return approve_purchase_order(
                order,
                user,
                request,
                reason="Auto-approved — company policy does not require sign-off.",
            )
    return order


def approve_purchase_order(order, user, request, reason=""):
    if order.status != DocumentStatus.SUBMITTED:
        raise ValidationError("Only a submitted order can be approved.")
    with transaction.atomic():
        order.status = DocumentStatus.APPROVED
        order.approved_at = timezone.now()
        order.approved_by = user
        order.approval_reason = reason
        order.updated_by = user
        order.save(update_fields=_HEADER_FIELDS)
        audit.record_action(request, AuditAction.APPROVE, order, reason=reason)
    return order


def reject_purchase_order(order, user, reason, request):
    if order.status != DocumentStatus.SUBMITTED:
        raise ValidationError("Only a submitted order can be rejected.")
    if not (reason or "").strip():
        raise ValidationError("Give a reason for rejecting this order.")
    with transaction.atomic():
        order.status = DocumentStatus.REJECTED
        order.approved_at = timezone.now()
        order.approved_by = user
        order.approval_reason = reason
        order.updated_by = user
        order.save(update_fields=_HEADER_FIELDS)
        audit.record_action(request, AuditAction.REJECT, order, reason=reason)
    return order
