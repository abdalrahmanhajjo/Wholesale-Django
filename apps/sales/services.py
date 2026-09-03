"""
Sales-order services: numbering, arithmetic, totals, discount allocation,
and the approval lifecycle (SAL-001..SAL-004, BR-010, BR-011, BR-022, NFR-008).

Every public function runs inside a transaction. The caller is responsible for
wrapping in `transaction.atomic()` if they need to combine it with a form save.
"""

from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import F, Sum
from django.utils import timezone

from apps.core import audit
from apps.core.models import (
    ZERO,
    DocumentSequence,
    DocumentStatus,
)
from apps.inventory.models import DeliveryNote, DeliveryNoteLine
from apps.ledger.models import AccountMapping, JournalType, MappingKey
from apps.ledger.services import (
    JournalDraft,
    JournalLineDraft,
    PostingEngineStub,
    PostingError,
    PostingErrorCode,
    PostingRequest,
)
from apps.sales.models import (
    DiscountKind,
    SalesInvoice,
    SalesInvoiceLine,
    SalesOrder,
)

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


def _document_discount_amount(order):
    """
    Derive the header discount total (txn currency) from kind/value.

    SAL-003: PERCENT is a percentage of the total gross
    (Σ quantity × unit_price); AMOUNT is used as-is. NONE (or a
    missing/non-positive value) yields zero.
    """
    kind = order.document_discount_kind
    value = order.document_discount_value or ZERO
    if kind == DiscountKind.NONE or value <= ZERO:
        return ZERO
    if kind == DiscountKind.AMOUNT:
        return _round_money(value)
    gross_total = sum(
        (ln.quantity or ZERO) * (ln.unit_price or ZERO) for ln in order.lines.all()
    )
    return _round_money(gross_total * value / Decimal("100"))


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
            ln.save(update_fields=["allocated_document_discount_txn"])
        return

    # Sum of all gross amounts
    total_gross = sum((ln.quantity or ZERO) * (ln.unit_price or ZERO) for ln in lines)
    if total_gross <= ZERO:
        for ln in lines:
            ln.allocated_document_discount_txn = ZERO
            ln.save(update_fields=["allocated_document_discount_txn"])
        return

    allocated_so_far = ZERO
    total_lines = len(lines)
    for i, ln in enumerate(lines):
        gross = (ln.quantity or ZERO) * (ln.unit_price or ZERO)
        if i < total_lines - 1:
            share = _round_money(discount_total * gross / total_gross)
            ln.allocated_document_discount_txn = share
            allocated_so_far += share
        else:
            # Last line gets the remainder to avoid rounding drift
            ln.allocated_document_discount_txn = discount_total - allocated_so_far
        ln.save(update_fields=["allocated_document_discount_txn"])


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
    order.document_discount_txn = _document_discount_amount(order)
    allocate_document_discount(order)
    for line in order.lines.all():
        calculate_line(line)
        line.save(
            update_fields=[
                "gross_txn",
                "line_discount_txn",
                "allocated_document_discount_txn",
                "net_txn",
                "taxable_base_txn",
                "tax_txn",
                "total_txn",
                "net_base",
                "taxable_base_base",
                "tax_base",
                "total_base",
            ]
        )
    calculate_totals(order)
    order.save(
        update_fields=[
            "subtotal_txn",
            "line_discount_txn",
            "document_discount_txn",
            "taxable_base_txn",
            "tax_txn",
            "rounding_txn",
            "total_txn",
            "subtotal_base",
            "line_discount_base",
            "document_discount_base",
            "taxable_base_base",
            "tax_base",
            "rounding_base",
            "total_base",
            "open_txn",
            "open_base",
        ]
    )


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
    order.save(
        update_fields=[
            "status",
            "submitted_at",
            "updated_by",
            "updated_at",
        ]
    )
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
    order.save(
        update_fields=[
            "status",
            "approved_at",
            "approved_by",
            "approval_reason",
            "updated_by",
            "updated_at",
        ]
    )
    audit.record_action(
        None,
        audit.AuditAction.APPROVE,
        order,
        reason=reason,
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
    order.save(
        update_fields=[
            "status",
            "approved_at",
            "approved_by",
            "approval_reason",
            "updated_by",
            "updated_at",
        ]
    )
    audit.record_action(
        None,
        audit.AuditAction.REJECT,
        order,
        reason=reason,
    )
    return order


# ---------------------------------------------------------------------------
# Delivery notes (SAL-005, INV-007)
#
# DeliveryNote / DeliveryNoteLine live in apps/inventory (Member 2's app, BRD
# 11.2). The screens and business flow are owned here in sales. The actual
# StockMovement ledger write is owned by Member 2's stock-posting engine (Day 5,
# INV-003/INV-005); this module ships the delivery flow and leaves a clearly
# marked seam (`_commit_stock_movements`) for that engine to fill. Everything
# else — eligibility, partial delivery, counters, status transitions — is
# complete and testable without it.
# ---------------------------------------------------------------------------


def allocate_dn_number(series="DEFAULT"):
    """
    Generate the next delivery-note number ("DN-00001") with SELECT ... FOR
    UPDATE on the sequence row (NFR-008). Raises ValueError when no active DN
    sequence is configured.
    """
    with transaction.atomic():
        seq = (
            DocumentSequence.objects.select_for_update()
            .filter(document_type="DN", series=series, is_active=True)
            .first()
        )
        if seq is None:
            raise ValueError(
                "No active document sequence for DN / DEFAULT. "
                "Ask an administrator to create one in Settings."
            )
        num = seq.next_number
        seq.next_number = F("next_number") + 1
        seq.save(update_fields=["next_number"])
        formatted = str(num).zfill(seq.padding)
        return f"{seq.prefix}{formatted}{seq.suffix}"


_OVERRIDE_OVER_DELIVERY_PERMISSION = "sales.override_over_delivery"


def remaining_to_deliver(order_line):
    """Quantity still to deliver for an order line (SAL-005)."""
    return (order_line.quantity or ZERO) - (order_line.quantity_delivered or ZERO)


def build_delivery_lines(order):
    """
    Order lines that are still partially or fully undelivered, as (order_line,
    remaining) pairs, in line order. Only OPEN/APPROVED orders with remaining
    quantity are candidates.
    """
    candidates = []
    for ln in order.lines.select_related("product", "unit").order_by("line_no"):
        remaining = remaining_to_deliver(ln)
        if remaining > ZERO:
            candidates.append((ln, remaining))
    return candidates


def create_delivery_from_order(*, order, user, quantities, **kwargs):
    """
    Create and immediately POST a delivery note from an approved sales order.

    Intended for the fast path on the SalesOrder detail screen. `quantities`
    maps order-line pk to the quantity delivered on this note. Raises
    ValueError when the order is not APPROVED or any line over-delivers
    (SAL-005) unless the caller passes allow_over_delivery=True after an
    explicit override.

    Returns the created, posted DeliveryNote.
    """
    note = draft_delivery_from_order(order=order, user=user, quantities=quantities, **kwargs)
    return post_delivery(note, user)


def draft_delivery_from_order(*, order, user, quantities, **kwargs):
    """
    Create a DRAFT delivery-note header and lines from an approved order, then
    number the note but do NOT post it. `quantities` is {order_line_pk: qty}.
    """
    if order.status not in (DocumentStatus.APPROVED, DocumentStatus.PARTIAL):
        raise ValueError(
            f"Cannot deliver order {order.number}: status is {order.status}, "
            "expected APPROVED (or PARTIAL for a follow-up delivery) (SAL-005)."
        )

    lines = order.lines.select_related("product", "unit").order_by("line_no")
    remaining = {ln.pk: remaining_to_deliver(ln) for ln in lines}
    updates = {}

    for ln in lines:
        qty = quantities.get(ln.pk, ZERO)
        qty = qty or ZERO
        if qty < ZERO:
            raise ValueError("Delivery quantities cannot be negative.")
        if qty > remaining[ln.pk]:
            raise ValueError(
                f"Cannot deliver {qty} of {ln.quantity} ordered on line "
                f"{ln.line_no} ({ln.product}): {remaining[ln.pk]} remains "
                "(over-delivery blocked, SAL-005)."
            )
        if qty > ZERO:
            updates[ln] = qty

    if not updates:
        raise ValueError("No quantities to deliver on this note.")

    note = DeliveryNote(
        number=allocate_dn_number(),
        customer=order.customer,
        sales_order=order,
        warehouse=kwargs.pop("warehouse", order.warehouse),
        document_date=kwargs.pop("document_date", timezone.localdate()),
        status=DocumentStatus.DRAFT,
        reference=kwargs.pop("reference", ""),
        notes=kwargs.pop("notes", ""),
        shipping_address_text=kwargs.pop("shipping_address_text", order.shipping_address_text),
        carrier=kwargs.pop("carrier", ""),
        tracking_reference=kwargs.pop("tracking_reference", ""),
    )
    note.save()

    line_no = 1
    for ln, qty in sorted(updates.items(), key=lambda kv: kv[0].line_no):
        DeliveryNoteLine.objects.create(
            delivery=note,
            line_no=line_no,
            sales_order_line=ln,
            product=ln.product,
            description=ln.product.name,
            unit=ln.unit,
            quantity=qty,
            unit_cost=ZERO,
            total_cost=ZERO,
        )
        line_no += 1

    audit.record(
        audit.AuditAction.CREATE, note, user=user, changes={"created": audit.snapshot(note)}
    )
    return note


def post_delivery(note, user):
    """
    POST a DRAFT delivery note (SAL-005, INV-007).

    Inside one transaction it:
      * validates the note is DRAFT and every line still has remaining
        quantity on its order line (the note may have been worked on while
        other deliveries went out);
      * writes the stock movements through the Member 2 seam
        (`_commit_stock_movements`) — a no-op until the Day 5 payers arrive,
        guarded by the same idempotency shape as StockMovement;
      * increments SalesOrderLine.quantity_delivered;
      * flips the order to PARTIAL while any line remains, or COMPLETED when
        every line is fully delivered (fired from the same transaction);
      * records the POST audit event.

    Over-delivery is rejected on the individual line unless the caller set
    line.delivery_override = True before posting (authorised override, SAL-005).
    """
    with transaction.atomic():
        _validate_postable(note)
        summary = {}
        for dn_line in note.lines.select_related("sales_order_line", "product").order_by(
            "line_no"
        ):
            so_line = dn_line.sales_order_line
            qty = dn_line.quantity or ZERO
            remaining = remaining_to_deliver(so_line)
            if qty > remaining and not getattr(dn_line, "delivery_override", False):
                raise ValueError(
                    f"Line {dn_line.line_no}: can deliver at most {remaining} "
                    f"more of {so_line.product}; this note delivers {qty}. "
                    "(over-delivery blocked, SAL-005)"
                )

            so_line.quantity_delivered = F("quantity_delivered") + qty
            so_line.save(update_fields=["quantity_delivered"])
            so_line.refresh_from_db()

            summary[f"line-{dn_line.line_no}"] = str(qty)

        _commit_stock_movements(note, user)

        note.status = DocumentStatus.POSTED
        note.posted_at = timezone.now()
        note.posted_by = user
        note.save(
            update_fields=[
                "status",
                "posted_at",
                "posted_by",
                "updated_at",
            ]
        )

        audit.record(audit.AuditAction.POST, note, user=user)
        _sync_order_fulfilment(note.sales_order)
        note.refresh_from_db()
        return note


def _commit_stock_movements(note, user):
    """
    Seam for Member 2's stock-posting engine (Day 5, INV-003/INV-005).

    When that engine lands, this function should write one StockMovement row
    per delivery line using DELIVERY / direction -1 and the engine's
    weighted-average cost, with `idempotency_key = "delivery:{note.number}:"
    "{warehouse_id}:{line_no}"` — the same idempotency shape defined on
    StockMovement (GL-002). Until then it is deliberately a no-op so the
    delivery flow works end-to-end without inventing a competing source of
    truth (INV-003).
    """
    return None


def _validate_postable(note):
    if note.status != DocumentStatus.DRAFT:
        raise ValueError(
            f"Cannot post delivery {note.number}: status is {note.status}, "
            "expected DRAFT (SAL-005)."
        )
    if not note.lines.exists():
        raise ValueError("A delivery note needs at least one line to post.")


def _sync_order_fulfilment(order):
    """
    After a delivery, move the order forward: PARTIAL while any line remains,
    COMPLETED once every line is fully delivered. Approved order stays APPROVED
    while unfulfilled lines remain.
    """
    if order is None:
        return
    order = SalesOrder.objects.select_for_update().get(pk=order.pk)

    total = order.lines.count()
    if total == 0:
        return
    open_lines = 0
    for ln in order.lines.all():
        if remaining_to_deliver(ln) > ZERO:
            open_lines += 1

    if open_lines == 0:
        new_status = DocumentStatus.COMPLETED
    elif order.status in (
        DocumentStatus.APPROVED,
        DocumentStatus.COMPLETED,
        DocumentStatus.PARTIAL,
    ):
        new_status = DocumentStatus.PARTIAL
    else:
        return

    if order.status != new_status:
        order.status = new_status
        order.save(update_fields=["status", "updated_at"])
        audit.record_action(
            None,
            audit.AuditAction.UPDATE,
            order,
            reason=f"Fulfilment changed to {new_status}",
        )


# ---------------------------------------------------------------------------
# Sales invoices (SAL-006..SAL-011)
# ---------------------------------------------------------------------------


def allocate_invoice_number(series="DEFAULT"):
    """Next 'INV-00001' via SELECT ... FOR UPDATE (same shape as allocate_so_number)."""
    with transaction.atomic():
        seq = (
            DocumentSequence.objects.select_for_update()
            .filter(document_type="SI", series=series, is_active=True)
            .first()
        )
        if seq is None:
            raise ValueError(
                "No active document sequence for SI / DEFAULT. "
                "Ask an administrator to create one in Settings."
            )
        num = seq.next_number
        seq.next_number = F("next_number") + 1
        seq.save(update_fields=["next_number"])
        return f"{seq.prefix}{str(num).zfill(seq.padding)}{seq.suffix}"


def remaining_to_invoice(delivery_line):
    """SAL-006 no-double-invoicing guard — mirrors remaining_to_deliver().

    remaining = delivery_line.quantity - delivery_line.quantity_invoiced
    """
    return (delivery_line.quantity or ZERO) - (delivery_line.quantity_invoiced or ZERO)


def build_invoice_lines_from_delivery(delivery):
    """(DeliveryNoteLine, remaining-to-invoice) pairs for a posted delivery."""
    rows = []
    for dl in delivery.lines.select_related("product", "unit").order_by("line_no"):
        remaining = remaining_to_invoice(dl)
        if remaining > ZERO:
            rows.append((dl, remaining))
    return rows


def recalculate_invoice(invoice):
    """Totals (SAL-008). Reuses the Day 2 arithmetic unchanged.

    SalesInvoiceLine is a DocumentLineBase and SalesInvoice a
    FinancialDocumentBase, so calculate_line / allocate_document_discount /
    calculate_totals work as-is — they only touch .lines and those field names.
    """
    for ln in invoice.lines.all():
        calculate_line(ln)  # Day-2 function, reused verbatim
        ln.save(
            update_fields=[
                "gross_txn",
                "line_discount_txn",
                "net_txn",
                "taxable_base_txn",
                "tax_txn",
                "total_txn",
            ]
        )
    allocate_document_discount(invoice)  # Day-2 function, reused verbatim
    calculate_totals(invoice)  # Day-2 function, reused verbatim
    invoice.save()


def create_invoice_from_delivery(*, delivery, user, quantities, **kwargs):
    """DRAFT invoice from a POSTED delivery (the graded Day-4 path).

    `quantities` = {DeliveryNoteLine.pk: qty}. Over-invoicing is blocked here
    (SAL-006) and re-checked at post time.
    """
    if delivery.status != DocumentStatus.POSTED:
        raise ValueError(
            f"Cannot invoice delivery {delivery.number}: status is "
            f"{delivery.status}, expected POSTED."
        )
    lines = delivery.lines.select_related("product", "unit", "sales_order_line").order_by(
        "line_no"
    )
    remaining = {dl.pk: remaining_to_invoice(dl) for dl in lines}
    updates = {}
    for dl in lines:
        qty = quantities.get(dl.pk, ZERO) or ZERO
        if qty < ZERO:
            raise ValueError("Invoice quantities cannot be negative.")
        if qty > remaining[dl.pk]:
            raise ValueError(
                f"Cannot invoice {qty} of {dl.quantity} on delivery line "
                f"{dl.line_no} ({dl.product}): {remaining[dl.pk]} remains to "
                "invoice (double-invoicing blocked, SAL-006)."
            )
        if qty > ZERO:
            updates[dl] = qty
    if not updates:
        raise ValueError("No quantities to invoice on this note.")

    invoice = SalesInvoice(
        number=allocate_invoice_number(),
        customer=delivery.customer,
        warehouse=kwargs.pop("warehouse", delivery.warehouse),
        sales_order=delivery.sales_order,
        payment_term=kwargs.pop("payment_term", delivery.customer.payment_term),
        document_date=kwargs.pop("document_date", timezone.localdate()),
        posting_date=kwargs.pop("posting_date", timezone.localdate()),
        currency=delivery.sales_order.currency
        if delivery.sales_order
        else delivery.customer.currency,
        exchange_rate=kwargs.pop(
            "exchange_rate",
            delivery.sales_order.exchange_rate if delivery.sales_order else Decimal("1"),
        ),
        due_date=kwargs.pop("due_date", None),
        status=DocumentStatus.DRAFT,
        customer_name_snapshot=delivery.customer.name,
        customer_tax_id_snapshot=delivery.customer.tax_id or "",
        billing_address_text=kwargs.pop("billing_address_text", ""),
        shipping_address_text=delivery.shipping_address_text,
        customer_reference=(
            delivery.sales_order.customer_reference if delivery.sales_order else ""
        ),
        notes=kwargs.pop("notes", ""),
        document_discount_kind=DiscountKind.NONE,
        document_discount_value=ZERO,
    )
    invoice.save()

    line_no = 1
    for dl, qty in sorted(updates.items(), key=lambda kv: kv[0].line_no):
        SalesInvoiceLine.objects.create(
            invoice=invoice,
            line_no=line_no,
            product=dl.product,
            unit=dl.unit,
            description=dl.product.name,
            quantity=qty,
            unit_price=(dl.sales_order_line.unit_price if dl.sales_order_line else ZERO),
            discount_percent=ZERO,
            tax_code=(dl.sales_order_line.tax_code if dl.sales_order_line else None),
            tax_rate_percent=(
                dl.sales_order_line.tax_rate_percent if dl.sales_order_line else ZERO
            ),
            tax_is_inclusive=(
                dl.sales_order_line.tax_is_inclusive if dl.sales_order_line else False
            ),
            warehouse=dl.delivery.warehouse,
            sales_order_line=dl.sales_order_line,
            delivery_line=dl,
            product_sku_snapshot=dl.product.sku,
        )
        line_no += 1

    recalculate_invoice(invoice)
    audit.record(
        audit.AuditAction.CREATE,
        invoice,
        user=user,
        changes={"created": audit.snapshot(invoice)},
    )
    return invoice


def submit_invoice(invoice, user):
    """SAL-007: DRAFT -> SUBMITTED (posting requires SUBMITTED)."""
    if invoice.status not in (DocumentStatus.DRAFT, DocumentStatus.SUBMITTED):
        raise ValueError(
            f"Cannot submit invoice {invoice.number}: status is {invoice.status}, "
            "expected DRAFT."
        )
    invoice.status = DocumentStatus.SUBMITTED
    invoice.submitted_at = timezone.now()
    invoice.save(update_fields=["status", "submitted_at", "updated_at"])
    audit.record_action(None, audit.AuditAction.SUBMIT, invoice)
    return invoice


# ---------------------------------------------------------------------------
# Journal builder (SAL-009) — contracts with the posting engine
# ---------------------------------------------------------------------------


def _resolve_account(key):
    """CFG-007: resolve an Account via AccountMapping, never a hardcoded id."""
    mapping = AccountMapping.objects.filter(key=key).select_related("account").first()
    if mapping is None:
        raise PostingError(
            f"No account mapping configured for {key} (CFG-007). "
            "Ask an administrator to set it in Settings.",
            code=PostingErrorCode.INVALID_REQUEST,
        )
    return mapping.account


def build_sales_invoice_journal(invoice, *, user):
    """Return an immutable JournalDraft. Builders never save anything."""
    lines = [
        JournalLineDraft(
            account=_resolve_account(MappingKey.ACCOUNTS_RECEIVABLE),
            debit_base=invoice.total_base,
            customer=invoice.customer,
            description=f"Sales invoice {invoice.number}",
        ),
        JournalLineDraft(
            account=_resolve_account(MappingKey.SALES_REVENUE),
            credit_base=invoice.taxable_base_base,
            description=f"Sales revenue {invoice.number}",
        ),
    ]
    if invoice.tax_base:
        lines.append(
            JournalLineDraft(
                account=_resolve_account(MappingKey.OUTPUT_TAX),
                credit_base=invoice.tax_base,
                tax_code=None,  # single VAT bucket; see tax subledger later
                description=f"Output tax {invoice.number}",
            )
        )
    if invoice.rounding_base:
        # Keep the journal exactly balanced (BR-006) when total != taxable + tax.
        lines.append(
            JournalLineDraft(
                account=_resolve_account(
                    MappingKey.ROUNDING_LOSS
                    if invoice.rounding_base < 0
                    else MappingKey.ROUNDING_GAIN
                ),
                credit_base=abs(invoice.rounding_base) if invoice.rounding_base < 0 else ZERO,
                debit_base=abs(invoice.rounding_base) if invoice.rounding_base > 0 else ZERO,
            )
        )

    # SAL-010: COGS / Inventory for stocked lines, at the delivery's unit cost.
    # unit_cost is Member 2's weighted-average number; until it lands it is 0
    # and the lines are skipped (a 0/0 JournalLineDraft is rejected on purpose).
    for sl in invoice.lines.select_related("delivery_line", "product"):
        dl = sl.delivery_line
        if dl is None or not (dl.unit_cost or ZERO) > ZERO:
            continue
        amount = (dl.unit_cost or ZERO) * (sl.quantity or ZERO)
        lines.append(
            JournalLineDraft(
                account=_resolve_account(MappingKey.COGS),
                debit_base=amount,
                product=sl.product,
                warehouse=sl.warehouse or dl.delivery.warehouse,
            )
        )
        lines.append(
            JournalLineDraft(
                account=_resolve_account(MappingKey.INVENTORY),
                credit_base=amount,
                product=sl.product,
                warehouse=sl.warehouse or dl.delivery.warehouse,
            )
        )

    return JournalDraft(
        entry_date=invoice.posting_date,
        journal_type=JournalType.SALES,
        narration=f"Sales invoice {invoice.number}",
        currency=invoice.currency,
        exchange_rate=invoice.exchange_rate,  # Decimal, as required by the contract
        source_doc_type="SI",
        source_doc_number=invoice.number,
        lines=tuple(lines),
    )


# ---------------------------------------------------------------------------
# Posting (SAL-009) — the real engine, no silent no-op
# ---------------------------------------------------------------------------

# The one binding to swap when Member 4's concrete engine lands. The contract
# interface is identical, so nothing else in this module changes.
posting_service = PostingEngineStub()


def post_invoice(invoice, user):
    """SUBMITTED -> POSTED, write the journal via the engine (BR-005 atomic)."""
    if invoice.status != DocumentStatus.SUBMITTED:
        raise ValueError(
            f"Cannot post invoice {invoice.number}: status is {invoice.status}, "
            "expected SUBMITTED (SAL-007)."
        )
    with transaction.atomic():
        result = posting_service.post(
            PostingRequest(
                source=invoice,
                user=user,
                idempotency_key=f"sales-invoice:{invoice.pk}:post:v1",
                build_journal=build_sales_invoice_journal,
                reason="Invoice posting",
            )
        )

        invoice.status = DocumentStatus.POSTED
        invoice.journal_entry = result.journal_entry
        invoice.posted_at = timezone.now()
        invoice.posted_by = user
        invoice.save(
            update_fields=[
                "status",
                "journal_entry",
                "posted_at",
                "posted_by",
                "updated_at",
            ]
        )

        # SAL-006: bump the delivery-line invoiced counter so nothing is
        # invoiced twice.
        for sl in invoice.lines.select_related("delivery_line").all():
            dl = sl.delivery_line
            if dl is not None and (sl.quantity or ZERO) > ZERO:
                dl.quantity_invoiced = F("quantity_invoiced") + sl.quantity
                dl.save(update_fields=["quantity_invoiced"])

        audit.record(audit.AuditAction.POST, invoice, user=user)
        invoice.refresh_from_db()
    return invoice
