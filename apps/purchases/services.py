"""
Purchase cycle business logic: document numbering, line/header totals, the
PUR-002 approval workflow, the PUR-008/PUR-010 AP + tax posting, and the
RET-005..RET-008 return / vendor-credit reversal.

Kept out of views.py and forms.py on purpose (CONTRIBUTING.md §4): a view
should read as "check permission, validate the form, call a service, render".

BRD coverage: PUR-001, PUR-002, PUR-005..PUR-010, RET-005..RET-008, BR-003,
BR-005, BR-010, BR-011, BR-012, BR-015, CFG-007, CFG-008, CFG-010, GL-001,
GL-002, GL-010, GL-011, NFR-008.
"""

from decimal import ROUND_HALF_UP, Decimal

from django.contrib.contenttypes.models import ContentType
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
    FiscalPeriod,
)
from apps.inventory import services as inventory_services
from apps.inventory.models import MovementType
from apps.ledger.models import AccountMapping, JournalEntry, JournalLine, JournalType
from apps.sales.models import ReturnDisposition

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
def _allocate_number(document_type, on_date):
    """
    Reserve the next number for a document type.

    Takes SELECT ... FOR UPDATE on the sequence row so two clerks saving at the
    same moment cannot be handed the same number (NFR-008). Must be called
    from inside the caller's `transaction.atomic()` so the reservation and the
    document it is stamped onto commit or roll back together.
    """
    sequence = DocumentSequence.objects.select_for_update().get(
        document_type=document_type, series="DEFAULT"
    )
    key = _period_key(sequence.reset_policy, on_date)
    if sequence.reset_policy != "NEVER" and sequence.period_key != key:
        sequence.next_number = 1
        sequence.period_key = key
    allocated = sequence.next_number
    sequence.next_number += 1
    sequence.save(update_fields=["next_number", "period_key"])
    return f"{sequence.prefix}{allocated:0{sequence.padding}d}{sequence.suffix}"


def allocate_po_number(document_date):
    return _allocate_number(DocumentType.PURCHASE_ORDER, document_date)


def allocate_pb_number(document_date):
    return _allocate_number(DocumentType.PURCHASE_BILL, document_date)


def allocate_pr_number(document_date):
    return _allocate_number(DocumentType.PURCHASE_RETURN, document_date)


def allocate_dbn_number(document_date):
    return _allocate_number(DocumentType.DEBIT_NOTE, document_date)


def _allocate_journal_number(on_date):
    """Automatic postings share the same JV- sequence as a manual journal."""
    return _allocate_number(DocumentType.JOURNAL_ENTRY, on_date)


# ---------------------------------------------------------------------------
# Totals (BR-010, BR-011, BR-012 arithmetic contract)
#
# Shared by the purchase order and the purchase bill: both are a
# FinancialDocumentBase header over DocumentLineBase lines, and BR-010/BR-011
# apply identically to each, so the arithmetic is written once here.
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


def _recalculate_document(document, lines):
    """
    Recompute every line, allocate the header discount across them (BR-011),
    and roll the results up into the header totals.

    Call this once, after the line formset has been saved, inside the same
    `transaction.atomic()` as the rest of the save (BR-005). `document` is a
    PurchaseOrder or a PurchaseBill; both carry the same discount and total
    fields (FinancialDocumentBase).
    """
    lines = list(lines)

    # First pass at gross-less-line-discount, to weight the header discount.
    pre_discount_net = []
    for line in lines:
        gross = (line.quantity or ZERO) * (line.unit_price or ZERO)
        line_discount = _money(gross * (line.discount_percent or ZERO) / HUNDRED)
        pre_discount_net.append(gross - line_discount)
    subtotal_before_header_discount = sum(pre_discount_net, ZERO)

    if document.document_discount_kind == "PERCENT":
        header_discount = _money(
            subtotal_before_header_discount
            * (document.document_discount_value or ZERO)
            / HUNDRED
        )
    elif document.document_discount_kind == "AMOUNT":
        header_discount = _money(document.document_discount_value or ZERO)
    else:
        header_discount = ZERO
    header_discount = min(header_discount, subtotal_before_header_discount)

    rate = document.exchange_rate or Decimal("1")
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
        # FTD-003: the ledger posts in base currency, so every line needs its
        # own converted figures, not just the header's.
        line.net_base = _money(line.net_txn * rate)
        line.taxable_base_base = _money(line.taxable_base_txn * rate)
        line.tax_base = _money(line.tax_txn * rate)
        line.total_base = _money(line.total_txn * rate)
        line.save()

        subtotal_txn += line.gross_txn
        line_discount_txn += line.line_discount_txn
        taxable_base_txn += line.taxable_base_txn
        tax_txn += line.tax_txn

    document.subtotal_txn = subtotal_txn
    document.line_discount_txn = line_discount_txn
    document.document_discount_txn = header_discount
    document.taxable_base_txn = taxable_base_txn
    document.tax_txn = tax_txn
    document.total_txn = taxable_base_txn + tax_txn

    document.subtotal_base = _money(document.subtotal_txn * rate)
    document.line_discount_base = _money(document.line_discount_txn * rate)
    document.document_discount_base = _money(document.document_discount_txn * rate)
    document.taxable_base_base = _money(document.taxable_base_txn * rate)
    document.tax_base = _money(document.tax_txn * rate)
    document.total_base = _money(document.total_txn * rate)

    # BR-007: open balance is derived from the total, not asked for — kept in
    # sync here so a still-DRAFT bill (nothing allocated or credited yet)
    # satisfies PurchaseBill's pb_open_is_derived constraint on every save,
    # not just when it's posted. Only the txn side is a DB-enforced identity
    # (FinancialDocumentBase has no allocated_base/credited_base to derive
    # open_base from); open_base tracks total_base for the same reason.
    document.open_txn = document.total_txn - document.allocated_txn - document.credited_txn
    document.open_base = document.total_base
    document.save()
    return document


def recalculate_order(order):
    return _recalculate_document(order, order.lines.all())


def recalculate_bill(bill):
    return _recalculate_document(bill, bill.lines.all())


def recalculate_debit_note(note):
    """
    Recompute every line's tax/discount arithmetic (BR-010, BR-012) and roll
    the results into the header total.

    This is deliberately not a call to `_recalculate_document()`: that
    function also derives `open_txn` from `credited_txn`, a field
    `VendorDebitNote` doesn't settle against — it settles against
    `refunded_txn` instead (RET-007). It also allocates a header-level
    discount, which a debit note has no field for and no need of: each line
    already states exactly the amount being credited back. Reuses
    `_recalculate_line()` for the per-line tax math, which only touches
    per-line fields and so applies unchanged.
    """
    lines = list(note.lines.all())
    rate = note.exchange_rate or Decimal("1")
    subtotal_txn = line_discount_txn = taxable_base_txn = tax_txn = ZERO

    for line in lines:
        line.allocated_document_discount_txn = ZERO
        _recalculate_line(line)
        line.net_base = _money(line.net_txn * rate)
        line.taxable_base_base = _money(line.taxable_base_txn * rate)
        line.tax_base = _money(line.tax_txn * rate)
        line.total_base = _money(line.total_txn * rate)
        line.save()

        subtotal_txn += line.gross_txn
        line_discount_txn += line.line_discount_txn
        taxable_base_txn += line.taxable_base_txn
        tax_txn += line.tax_txn

    note.subtotal_txn = subtotal_txn
    note.line_discount_txn = line_discount_txn
    note.document_discount_txn = ZERO
    note.taxable_base_txn = taxable_base_txn
    note.tax_txn = tax_txn
    note.total_txn = taxable_base_txn + tax_txn

    note.subtotal_base = _money(note.subtotal_txn * rate)
    note.line_discount_base = _money(note.line_discount_txn * rate)
    note.document_discount_base = ZERO
    note.taxable_base_base = _money(note.taxable_base_txn * rate)
    note.tax_base = _money(note.tax_txn * rate)
    note.total_base = _money(note.total_txn * rate)

    # RET-007: kept in sync on every save, same reason PurchaseBill derives
    # open_txn outside of posting — a still-DRAFT note satisfies
    # dbn_open_is_derived the moment its lines are saved.
    note.open_txn = note.total_txn - note.allocated_txn - note.refunded_txn
    note.open_base = note.total_base
    note.save()
    return note


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


# ---------------------------------------------------------------------------
# Posting (PUR-008, PUR-010, GL-001, GL-002, GL-010, GL-011)
# ---------------------------------------------------------------------------
def _fiscal_period_for(on_date):
    period = FiscalPeriod.objects.filter(
        start_date__lte=on_date, end_date__gte=on_date, status="OPEN"
    ).first()
    if period is None:
        raise ValidationError(
            f"No open fiscal period covers {on_date}. Ask an accountant to open one (CFG-009)."
        )
    return period


def _mapped_account(key):
    """CFG-007: posting stops with a clear message rather than a bad guess."""
    mapping = AccountMapping.objects.filter(key=key).select_related("account").first()
    if mapping is None:
        raise ValidationError(
            f"No account is mapped for {key} yet. Ask an administrator to configure it (CFG-007)."
        )
    return mapping.account


def post_purchase_bill(bill, user, request):
    """
    Posts the bill's AP and tax effect (PUR-008): a stock line clears the
    goods-received-not-invoiced accrual if it came from a receipt, or debits
    Inventory directly for a bill entered without one; a non-stock line debits
    its expense account; recoverable tax debits Input Tax; everything is
    credited to Accounts Payable (BR-006 balanced, GL-010/GL-011 control
    accounts only touched through this service).
    """
    if bill.status != DocumentStatus.DRAFT:
        raise ValidationError("Only a draft bill can be posted.")
    lines = list(bill.lines.select_related("purchase_order_line", "receipt_line"))
    if not lines:
        raise ValidationError("Add at least one line before posting.")
    if bill.total_txn == ZERO:
        raise ValidationError("This bill has no value to post — check the line prices.")

    with transaction.atomic():
        fiscal_period = _fiscal_period_for(bill.posting_date)
        ap_account = _mapped_account("ACCOUNTS_PAYABLE")
        inventory_account = _mapped_account("INVENTORY")
        grni_account = _mapped_account("GOODS_IN_TRANSIT")
        purchase_expense_account = _mapped_account("PURCHASE_EXPENSE")
        input_tax_account = _mapped_account("INPUT_TAX")
        non_recoverable_account = _mapped_account("TAX_NON_RECOVERABLE")

        journal_entry = JournalEntry.objects.create(
            number=_allocate_journal_number(bill.posting_date),
            entry_date=bill.posting_date,
            fiscal_period=fiscal_period,
            journal_type=JournalType.PURCHASE,
            narration=f"Purchase bill {bill.number} — {bill.vendor}",
            currency=bill.currency,
            exchange_rate=bill.exchange_rate,
            total_debit_base=bill.total_base,
            total_credit_base=bill.total_base,
            source_content_type=ContentType.objects.get_for_model(bill),
            source_object_id=bill.pk,
            source_doc_type=DocumentType.PURCHASE_BILL,
            source_doc_number=bill.number,
            idempotency_key=f"PB:{bill.pk}",
            posted_at=timezone.now(),
            posted_by=user,
        )

        line_no = 0

        def _write_line(account, debit_txn, debit_base, **dims):
            nonlocal line_no
            line_no += 1
            JournalLine.objects.create(
                entry=journal_entry,
                line_no=line_no,
                account=account,
                debit_txn=debit_txn,
                debit_base=debit_base,
                credit_txn=ZERO,
                credit_base=ZERO,
                currency=bill.currency,
                exchange_rate=bill.exchange_rate,
                **dims,
            )

        for line in lines:
            # The tax-exclusive base is what lands on inventory/expense; tax is
            # posted separately below so an inclusive rate is never counted twice.
            # A zero-priced line (a free/bonus item) has nothing to book here —
            # journal_line_debit_xor_credit rejects a line with both sides at
            # zero, and there is nothing wrong with skipping it: it contributes
            # nothing to the bill's total either, so BR-006 balance is unaffected.
            if line.taxable_base_txn:
                if line.is_stock_line:
                    account = grni_account if line.receipt_line_id else inventory_account
                    _write_line(
                        account,
                        line.taxable_base_txn,
                        line.taxable_base_base,
                        description=line.description
                        or (line.product and str(line.product))
                        or "",
                        product=line.product,
                        warehouse=line.warehouse,
                    )
                else:
                    _write_line(
                        line.expense_account or purchase_expense_account,
                        line.taxable_base_txn,
                        line.taxable_base_base,
                        description=line.description,
                    )
            if line.tax_txn:
                tax_account = (
                    input_tax_account if line.tax_is_recoverable else non_recoverable_account
                )
                _write_line(
                    tax_account,
                    line.tax_txn,
                    line.tax_base,
                    description=f"Tax on {bill.number} line {line.line_no}",
                    tax_code=line.tax_code,
                )
            if line.receipt_line_id:
                line.receipt_line.quantity_billed = (
                    line.receipt_line.quantity_billed + line.quantity
                )
                line.receipt_line.save(update_fields=["quantity_billed"])
            if line.purchase_order_line_id:
                line.purchase_order_line.quantity_billed = (
                    line.purchase_order_line.quantity_billed + line.quantity
                )
                line.purchase_order_line.save(update_fields=["quantity_billed"])

        line_no += 1
        JournalLine.objects.create(
            entry=journal_entry,
            line_no=line_no,
            account=ap_account,
            debit_txn=ZERO,
            debit_base=ZERO,
            credit_txn=bill.total_txn,
            credit_base=bill.total_base,
            currency=bill.currency,
            exchange_rate=bill.exchange_rate,
            vendor=bill.vendor,
            description=f"{bill.number} — {bill.vendor}",
        )

        bill.journal_entry = journal_entry
        bill.status = DocumentStatus.POSTED
        bill.posted_at = timezone.now()
        bill.posted_by = user
        bill.open_txn = bill.total_txn
        bill.open_base = bill.total_base
        bill.updated_by = user
        bill.save()
        audit.record_action(request, AuditAction.POST, bill)
    return bill


# ---------------------------------------------------------------------------
# Purchase return (RET-005, RET-008, BR-015, BR-017)
#
# Physical/authorisation side of a vendor return, mirroring SalesReturn:
# money follows on a vendor debit note. Posting only ever moves stock and
# consumes return eligibility — PurchaseReturn has no "posted must have a
# journal" constraint (unlike GoodsReceipt, PurchaseBill, StockAdjustment and
# VendorDebitNote, which all do), and that absence is deliberate, not an
# oversight: the AP/inventory-value/tax reversal is booked once, on the
# vendor debit note, not twice.
# ---------------------------------------------------------------------------
def _purchase_return_line_cost_preview(line):
    """
    Estimate a return line's cost before posting: the receipt's own
    weighted-average cost if the line traces back to one, else the bill
    line's tax-exclusive unit price, else the product's standing purchase
    price. A stock-affecting line's real cost is fixed at posting time from
    the shared costing engine instead (INV-005) — this is only ever shown as
    a preview.
    """
    if line.receipt_line_id and line.receipt_line.unit_cost:
        return line.receipt_line.unit_cost
    if line.bill_line_id and line.bill_line.quantity:
        return _money(line.bill_line.taxable_base_txn / line.bill_line.quantity)
    return _money(line.product.purchase_price)


def recalculate_purchase_return(purchase_return):
    total_cost = ZERO
    for line in purchase_return.lines.select_related("product", "bill_line", "receipt_line"):
        line.unit_cost = _purchase_return_line_cost_preview(line)
        line.total_cost = _money(line.quantity * line.unit_cost)
        line.save(update_fields=["unit_cost", "total_cost"])
        total_cost += line.total_cost
    purchase_return.total_cost_base = total_cost
    purchase_return.save(update_fields=["total_cost_base"])
    return purchase_return


def post_purchase_return(purchase_return, user, request):
    """
    Ships each line's quantity back out (RET-005): a RESTOCK or WRITE_OFF
    disposition posts a PURCHASE_RETURN_OUT movement through the shared
    costing engine — costed at the warehouse's *current* average, same as
    any other outbound movement, since that is what the units leaving are
    actually worth today, whatever they were bought at (INV-005). A
    NO_STOCK_EFFECT line (a paperwork-only correction — nothing is physically
    shipped) moves no stock at all.

    Every line, regardless of disposition, consumes return eligibility: BR-015
    checks it against whichever original line it traces back to (a bill line
    or a receipt line) before touching anything, so the vendor can't be
    credited twice for the same units. `OVERRIDE_RETURN_QUANTITY` isn't wired
    to bypass this — like BR-017's negative-stock check, the database enforces
    the same limit unconditionally via `pb_line_returned_within_billed` /
    `gr_line_returned_within_accepted`, so a permission-gated override here
    would be misleading.
    """
    if purchase_return.status != DocumentStatus.DRAFT:
        raise ValidationError("Only a draft return can be posted.")
    lines = list(purchase_return.lines.select_related("product", "bill_line", "receipt_line"))
    if not lines:
        raise ValidationError("Add at least one line before posting.")

    for line in lines:
        if line.bill_line_id:
            remaining = line.bill_line.quantity - line.bill_line.quantity_returned
            if line.quantity > remaining:
                raise ValidationError(
                    f"Line {line.line_no}: only {remaining} of "
                    f"{line.bill_line.quantity} billed is still eligible to return."
                )
        elif line.receipt_line_id:
            remaining = (
                line.receipt_line.quantity_accepted - line.receipt_line.quantity_returned
            )
            if line.quantity > remaining:
                raise ValidationError(
                    f"Line {line.line_no}: only {remaining} of "
                    f"{line.receipt_line.quantity_accepted} accepted is still "
                    "eligible to return."
                )

    with transaction.atomic():
        total_cost = ZERO
        for line in lines:
            if line.disposition != ReturnDisposition.NO_STOCK_EFFECT:
                movement = inventory_services.post_stock_movement(
                    product=line.product,
                    warehouse=purchase_return.warehouse,
                    movement_date=purchase_return.document_date,
                    movement_type=MovementType.PURCHASE_RETURN_OUT,
                    quantity=line.quantity,
                    source=purchase_return,
                    source_doc_type=DocumentType.PURCHASE_RETURN,
                    source_doc_number=purchase_return.number,
                    idempotency_key=f"PR:{purchase_return.pk}:{line.pk}",
                    user=user,
                )
                line.unit_cost = movement.unit_cost
                line.total_cost = movement.total_cost
                line.save(update_fields=["unit_cost", "total_cost"])

            total_cost += line.total_cost
            if line.bill_line_id:
                line.bill_line.quantity_returned = (
                    line.bill_line.quantity_returned + line.quantity
                )
                line.bill_line.save(update_fields=["quantity_returned"])
            elif line.receipt_line_id:
                line.receipt_line.quantity_returned = (
                    line.receipt_line.quantity_returned + line.quantity
                )
                line.receipt_line.save(update_fields=["quantity_returned"])

        purchase_return.total_cost_base = total_cost
        purchase_return.status = DocumentStatus.POSTED
        purchase_return.posted_at = timezone.now()
        purchase_return.posted_by = user
        purchase_return.save()
        audit.record_action(request, AuditAction.POST, purchase_return)

    return purchase_return


# ---------------------------------------------------------------------------
# Vendor debit note (RET-006, RET-007, RET-008, GL-010, GL-011)
# ---------------------------------------------------------------------------
def post_vendor_debit_note(note, user, request):
    """
    Books the RET-006 reversal — the exact mirror of `post_purchase_bill`,
    credited instead of debited: a stock line credits Inventory directly
    (by the time a bill posted, its value had already left GRNI one way or
    another, so there is no accrual left to re-touch here); a non-stock line
    credits its own expense account if it named one, else the dedicated
    Purchase Returns contra account rather than Purchase Expense, so returns
    show as their own line in the accounts instead of silently netting
    against gross purchases; recoverable tax credits Input Tax (or Non-
    recoverable Tax); everything debits Accounts Payable (BR-006 balanced).

    Never touches physical stock or `StockBalance` itself — a purchase return
    already did that, and a debit note with no return behind it (a pure
    pricing correction) has nothing physical to reverse. If this line
    references a bill line directly rather than a return line, it is the
    thing consuming that bill line's return eligibility, so
    `quantity_returned` is incremented here instead — never both, so the
    same units are never credited twice.
    """
    if note.status != DocumentStatus.DRAFT:
        raise ValidationError("Only a draft debit note can be posted.")
    lines = list(note.lines.select_related("bill_line", "return_line", "tax_code"))
    if not lines:
        raise ValidationError("Add at least one line before posting.")
    if note.total_txn == ZERO:
        raise ValidationError("This debit note has no value to post — check the line prices.")

    with transaction.atomic():
        fiscal_period = _fiscal_period_for(note.posting_date)
        ap_account = _mapped_account("ACCOUNTS_PAYABLE")
        inventory_account = _mapped_account("INVENTORY")
        purchase_returns_account = _mapped_account("PURCHASE_RETURNS")
        input_tax_account = _mapped_account("INPUT_TAX")
        non_recoverable_account = _mapped_account("TAX_NON_RECOVERABLE")

        journal_entry = JournalEntry.objects.create(
            number=_allocate_journal_number(note.posting_date),
            entry_date=note.posting_date,
            fiscal_period=fiscal_period,
            journal_type=JournalType.PURCHASE,
            narration=f"Vendor debit note {note.number} — {note.vendor}",
            currency=note.currency,
            exchange_rate=note.exchange_rate,
            total_debit_base=note.total_base,
            total_credit_base=note.total_base,
            source_content_type=ContentType.objects.get_for_model(note),
            source_object_id=note.pk,
            source_doc_type=DocumentType.DEBIT_NOTE,
            source_doc_number=note.number,
            idempotency_key=f"DBN:{note.pk}",
            posted_at=timezone.now(),
            posted_by=user,
        )

        line_no = 0

        def _write_line(account, credit_txn, credit_base, **dims):
            nonlocal line_no
            line_no += 1
            JournalLine.objects.create(
                entry=journal_entry,
                line_no=line_no,
                account=account,
                debit_txn=ZERO,
                debit_base=ZERO,
                credit_txn=credit_txn,
                credit_base=credit_base,
                currency=note.currency,
                exchange_rate=note.exchange_rate,
                **dims,
            )

        for line in lines:
            # See post_purchase_bill: a zero-value line has nothing to book
            # here and would otherwise violate journal_line_debit_xor_credit.
            if line.taxable_base_txn:
                if line.is_stock_line:
                    _write_line(
                        inventory_account,
                        line.taxable_base_txn,
                        line.taxable_base_base,
                        description=line.description
                        or (line.product and str(line.product))
                        or "",
                        product=line.product,
                    )
                else:
                    _write_line(
                        line.expense_account or purchase_returns_account,
                        line.taxable_base_txn,
                        line.taxable_base_base,
                        description=line.description,
                    )
            if line.tax_txn:
                tax_account = (
                    input_tax_account if line.tax_is_recoverable else non_recoverable_account
                )
                _write_line(
                    tax_account,
                    line.tax_txn,
                    line.tax_base,
                    description=f"Tax on {note.number} line {line.line_no}",
                    tax_code=line.tax_code,
                )
            if line.bill_line_id and not line.return_line_id:
                line.bill_line.quantity_returned = (
                    line.bill_line.quantity_returned + line.quantity
                )
                line.bill_line.save(update_fields=["quantity_returned"])

        line_no += 1
        JournalLine.objects.create(
            entry=journal_entry,
            line_no=line_no,
            account=ap_account,
            debit_txn=note.total_txn,
            debit_base=note.total_base,
            credit_txn=ZERO,
            credit_base=ZERO,
            currency=note.currency,
            exchange_rate=note.exchange_rate,
            vendor=note.vendor,
            description=f"{note.number} — {note.vendor}",
        )

        note.journal_entry = journal_entry
        note.status = DocumentStatus.POSTED
        note.posted_at = timezone.now()
        note.posted_by = user
        note.open_txn = note.total_txn
        note.open_base = note.total_base
        note.updated_by = user
        note.save()
        audit.record_action(request, AuditAction.POST, note)
    return note
