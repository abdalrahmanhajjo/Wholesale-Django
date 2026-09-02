"""
Goods receipt business logic: document numbering, the accept/reject split,
and the INV-006/PUR-003/PUR-004 stock + GRNI posting.

Kept out of views.py and forms.py on purpose, mirroring apps/purchases/services.py
(CONTRIBUTING.md §4): a view should read as "check permission, validate the
form, call a service, render".

BRD coverage: PUR-003, PUR-004, INV-003..INV-006, BR-017..BR-019, CFG-007,
CFG-008, GL-001, GL-002, GL-010, NFR-008.
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
from apps.inventory.models import MovementType, StockBalance, StockMovement
from apps.ledger.models import AccountMapping, JournalEntry, JournalLine, JournalType

ZERO = Decimal("0")
MONEY_QUANT = Decimal("0.0001")
COST_QUANT = Decimal("0.000001")


def _money(value):
    return (value or ZERO).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def _cost(value):
    return (value or ZERO).quantize(COST_QUANT, rounding=ROUND_HALF_UP)


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


def allocate_gr_number(document_date):
    return _allocate_number(DocumentType.GOODS_RECEIPT, document_date)


def _allocate_journal_number(on_date):
    return _allocate_number(DocumentType.JOURNAL_ENTRY, on_date)


# ---------------------------------------------------------------------------
# Accept/reject split and cost (PUR-004)
# ---------------------------------------------------------------------------
def _line_unit_cost(line):
    """
    Cost basis for a receipt line: the ordered price if it came off a PO
    (tax-exclusive, net of discount — the same basis the bill will post), or
    the product's standing purchase price for an authorised direct receipt.
    """
    po_line = line.purchase_order_line
    if po_line is not None and po_line.quantity:
        return _cost(po_line.taxable_base_txn / po_line.quantity)
    return _cost(line.product.purchase_price)


def recalculate_receipt(receipt):
    """
    Derive each line's accepted quantity and cost, and the header total.

    Call this once, after the line formset has been saved, inside the same
    `transaction.atomic()` as the rest of the save — mirrors
    apps.purchases.services.recalculate_order.
    """
    total_cost = ZERO
    for line in receipt.lines.select_related("purchase_order_line", "product"):
        line.quantity_accepted = line.quantity_received - line.quantity_rejected
        line.unit_cost = _line_unit_cost(line)
        line.total_cost = _money(line.quantity_accepted * line.unit_cost)
        line.save(update_fields=["quantity_accepted", "unit_cost", "total_cost"])
        total_cost += line.total_cost
    receipt.total_cost_base = total_cost
    receipt.save(update_fields=["total_cost_base"])
    return receipt


# ---------------------------------------------------------------------------
# Posting (INV-006, PUR-003, GL-001, GL-002, GL-010)
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
    mapping = AccountMapping.objects.filter(key=key).select_related("account").first()
    if mapping is None:
        raise ValidationError(
            f"No account is mapped for {key} yet. Ask an administrator to configure it (CFG-007)."
        )
    return mapping.account


def post_goods_receipt(receipt, user, request):
    """
    Moves stock and clears the accrual (INV-006): every accepted line becomes
    a weighted-average StockMovement, StockBalance is updated under a row
    lock so concurrent receipts of the same item serialise (NFR-003, NFR-008),
    and — unless every line was fully rejected — Inventory is debited against
    Goods Received Not Invoiced, cleared later when the vendor's bill lands
    (PUR-005..PUR-008).
    """
    if receipt.status != DocumentStatus.DRAFT:
        raise ValidationError("Only a draft receipt can be posted.")
    lines = list(receipt.lines.select_related("product", "purchase_order_line"))
    if not lines:
        raise ValidationError("Add at least one line before posting.")

    with transaction.atomic():
        fiscal_period = _fiscal_period_for(receipt.document_date)
        source_type = ContentType.objects.get_for_model(receipt)
        movements = []
        total_cost = ZERO

        for line in lines:
            if line.quantity_accepted <= 0:
                continue
            balance, _ = StockBalance.objects.select_for_update().get_or_create(
                product=line.product,
                warehouse=receipt.warehouse,
                defaults={"quantity_on_hand": ZERO, "average_cost": ZERO, "total_value": ZERO},
            )
            line_total = _money(line.quantity_accepted * line.unit_cost)
            new_qty = balance.quantity_on_hand + line.quantity_accepted
            new_value = balance.total_value + line_total
            new_avg = _cost(new_value / new_qty) if new_qty else ZERO

            movement = StockMovement.objects.create(
                movement_date=receipt.document_date,
                movement_type=MovementType.GOODS_RECEIPT,
                direction=1,
                product=line.product,
                warehouse=receipt.warehouse,
                quantity=line.quantity_accepted,
                unit_cost=line.unit_cost,
                total_cost=line_total,
                balance_quantity_after=new_qty,
                balance_value_after=new_value,
                average_cost_after=new_avg,
                source_content_type=source_type,
                source_object_id=receipt.pk,
                source_doc_type=DocumentType.GOODS_RECEIPT,
                source_doc_number=receipt.number,
                idempotency_key=f"GR:{receipt.pk}:{line.pk}",
                created_by=user,
            )
            balance.quantity_on_hand = new_qty
            balance.total_value = new_value
            balance.average_cost = new_avg
            balance.last_movement_at = timezone.now()
            balance.save()

            total_cost += line_total
            movements.append(movement)

            if line.purchase_order_line_id:
                po_line = line.purchase_order_line
                po_line.quantity_received = po_line.quantity_received + line.quantity_accepted
                po_line.save(update_fields=["quantity_received"])

        receipt.total_cost_base = total_cost

        journal_entry = None
        if total_cost > ZERO:
            inventory_account = _mapped_account("INVENTORY")
            grni_account = _mapped_account("GOODS_IN_TRANSIT")
            company = Company.objects.first()
            if company is None:
                raise ValidationError(
                    "Company configuration is missing. Ask an administrator to set it up."
                )
            base_currency = company.base_currency

            journal_entry = JournalEntry.objects.create(
                number=_allocate_journal_number(receipt.document_date),
                entry_date=receipt.document_date,
                fiscal_period=fiscal_period,
                journal_type=JournalType.INVENTORY,
                narration=f"Goods receipt {receipt.number} — {receipt.vendor}",
                currency=base_currency,
                exchange_rate=Decimal("1"),
                total_debit_base=total_cost,
                total_credit_base=total_cost,
                source_content_type=source_type,
                source_object_id=receipt.pk,
                source_doc_type=DocumentType.GOODS_RECEIPT,
                source_doc_number=receipt.number,
                idempotency_key=f"GR:{receipt.pk}",
                posted_at=timezone.now(),
                posted_by=user,
            )
            JournalLine.objects.create(
                entry=journal_entry,
                line_no=1,
                account=inventory_account,
                debit_txn=total_cost,
                debit_base=total_cost,
                credit_txn=ZERO,
                credit_base=ZERO,
                currency=base_currency,
                warehouse=receipt.warehouse,
                description=f"Received on {receipt.number}",
            )
            JournalLine.objects.create(
                entry=journal_entry,
                line_no=2,
                account=grni_account,
                debit_txn=ZERO,
                debit_base=ZERO,
                credit_txn=total_cost,
                credit_base=total_cost,
                currency=base_currency,
                warehouse=receipt.warehouse,
                description=f"Accrued for {receipt.number}, pending vendor bill",
            )
            for movement in movements:
                movement.journal_entry = journal_entry
                movement.save(update_fields=["journal_entry"])
            receipt.journal_entry = journal_entry

        receipt.status = DocumentStatus.POSTED
        receipt.posted_at = timezone.now()
        receipt.posted_by = user
        receipt.save()
        audit.record_action(request, AuditAction.POST, receipt)

    return receipt
