"""
Inventory business logic: document numbering, the goods-receipt accept/reject
split, the shared weighted-average costing engine, and posting for goods
receipts, transfers and adjustments.

Kept out of views.py and forms.py on purpose, mirroring apps/purchases/services.py
(CONTRIBUTING.md §4): a view should read as "check permission, validate the
form, call a service, render".

BRD coverage: PUR-003, PUR-004, INV-003..INV-011, BR-017..BR-019, CFG-007,
CFG-008, GL-001, GL-002, GL-010, NFR-008.
"""

from decimal import ROUND_HALF_UP, Decimal

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
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
from apps.inventory.models import INBOUND_TYPES, MovementType, StockBalance, StockMovement
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


def allocate_dn_number(document_date):
    return _allocate_number(DocumentType.DELIVERY_NOTE, document_date)


def allocate_st_number(document_date):
    return _allocate_number(DocumentType.STOCK_TRANSFER, document_date)


def allocate_sa_number(document_date):
    return _allocate_number(DocumentType.STOCK_ADJUSTMENT, document_date)


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
# Weighted-average costing engine (INV-003..INV-005, BR-018, BR-019, NFR-008)
#
# The single place every stock-moving document posts through: goods receipts
# today, deliveries/transfers/adjustments as they land. One function owns the
# balance arithmetic so "what is a unit of this product worth right now" is
# never computed two different ways in two different services.
# ---------------------------------------------------------------------------
def post_stock_movement(
    *,
    product,
    warehouse,
    movement_date,
    movement_type,
    quantity,
    unit_cost=None,
    source,
    source_doc_type,
    source_doc_number,
    idempotency_key,
    user,
    notes="",
):
    """
    Apply one movement to the product/warehouse balance and write the
    immutable ledger line for it (INV-004, RPT-017).

    Call this from inside the caller's `transaction.atomic()` — it takes
    `SELECT ... FOR UPDATE` on the StockBalance row so concurrent movements of
    the same item serialise (NFR-003, NFR-008) rather than racing each other's
    read-modify-write of the running average.

    An inbound movement (`movement_type` in INBOUND_TYPES) blends `unit_cost`
    into the running weighted average — the caller must supply it. An outbound
    movement is costed at the *current* average; `unit_cost` is ignored if
    given, because the balance itself is the only source of truth for what
    stock leaving the warehouse is worth (INV-005).

    BR-017's negative-stock policy is a database trigger
    (`wams_stock_negative_check`), not application logic, so the same rule
    applies everywhere a movement is posted, however it gets there. This
    only turns that trigger's error into a `ValidationError` a view can show.
    """
    direction = 1 if movement_type in INBOUND_TYPES else -1
    balance, _ = StockBalance.objects.select_for_update().get_or_create(
        product=product,
        warehouse=warehouse,
        defaults={"quantity_on_hand": ZERO, "average_cost": ZERO, "total_value": ZERO},
    )

    if direction == 1:
        cost = _cost(unit_cost)
        line_total = _money(quantity * cost)
        new_qty = balance.quantity_on_hand + quantity
        new_value = balance.total_value + line_total
        new_avg = _cost(new_value / new_qty) if new_qty else ZERO
    else:
        cost = balance.average_cost
        line_total = _money(quantity * cost)
        new_qty = balance.quantity_on_hand - quantity
        new_value = balance.total_value - line_total if new_qty > 0 else ZERO
        new_avg = balance.average_cost if new_qty > 0 else ZERO

    try:
        movement = StockMovement.objects.create(
            movement_date=movement_date,
            movement_type=movement_type,
            direction=direction,
            product=product,
            warehouse=warehouse,
            quantity=quantity,
            unit_cost=cost,
            total_cost=line_total,
            balance_quantity_after=new_qty,
            balance_value_after=new_value,
            average_cost_after=new_avg,
            source_content_type=ContentType.objects.get_for_model(source),
            source_object_id=source.pk,
            source_doc_type=source_doc_type,
            source_doc_number=source_doc_number,
            idempotency_key=idempotency_key,
            notes=notes,
            created_by=user,
        )
    except IntegrityError as exc:
        # wams_stock_negative_check (BR-017) fires on the StockBalance write
        # below in a real posting, but StockMovement's own CHECK constraints
        # (e.g. direction must match movement_type) can also land here.
        raise ValidationError(str(exc).strip()) from exc

    balance.quantity_on_hand = new_qty
    balance.total_value = new_value
    balance.average_cost = new_avg
    balance.last_movement_at = timezone.now()
    try:
        balance.save()
    except IntegrityError as exc:
        raise ValidationError(str(exc).strip()) from exc

    return movement


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
        movements = []
        total_cost = ZERO

        for line in lines:
            if line.quantity_accepted <= 0:
                continue
            movement = post_stock_movement(
                product=line.product,
                warehouse=receipt.warehouse,
                movement_date=receipt.document_date,
                movement_type=MovementType.GOODS_RECEIPT,
                quantity=line.quantity_accepted,
                unit_cost=line.unit_cost,
                source=receipt,
                source_doc_type=DocumentType.GOODS_RECEIPT,
                source_doc_number=receipt.number,
                idempotency_key=f"GR:{receipt.pk}:{line.pk}",
                user=user,
            )

            total_cost += movement.total_cost
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
                source_content_type=ContentType.objects.get_for_model(receipt),
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


# ---------------------------------------------------------------------------
# Stock transfers (INV-008, BR-017)
# ---------------------------------------------------------------------------
def recalculate_transfer(transfer):
    """
    Estimate each line's cost from the source warehouse's *current* average
    (a preview only — the authoritative figure is fixed at posting time, since
    the average can move between saving a draft and posting it).
    """
    total_cost = ZERO
    for line in transfer.lines.select_related("product"):
        balance = StockBalance.objects.filter(
            product=line.product, warehouse=transfer.from_warehouse
        ).first()
        line.unit_cost = balance.average_cost if balance else ZERO
        line.total_cost = _money(line.quantity * line.unit_cost)
        line.save(update_fields=["unit_cost", "total_cost"])
        total_cost += line.total_cost
    transfer.total_cost_base = total_cost
    transfer.save(update_fields=["total_cost_base"])
    return transfer


def post_stock_transfer(transfer, user, request):
    """
    Moves each line's quantity out of `from_warehouse` and into `to_warehouse`
    at the cost it actually left at (INV-008) — the TRANSFER_IN leg is costed
    at the TRANSFER_OUT leg's `unit_cost`, so a transfer carries the source's
    weighted average forward rather than inventing a new one.

    A transfer only touches the general ledger when the two warehouses
    resolve to different inventory accounts (CFG-007's optional per-warehouse
    override) — the common case, one shared Inventory account, is a pure
    stock relocation with no financial effect. When it does post, both legs
    clear through Stock Transfer Clearing rather than crediting one
    warehouse's account and debiting the other directly, so the clearing
    account's activity is a reviewable record of transfers in progress.
    """
    if transfer.status != DocumentStatus.DRAFT:
        raise ValidationError("Only a draft transfer can be posted.")
    lines = list(transfer.lines.select_related("product"))
    if not lines:
        raise ValidationError("Add at least one line before posting.")

    with transaction.atomic():
        fiscal_period = _fiscal_period_for(transfer.document_date)
        movements = []
        total_cost = ZERO

        for line in lines:
            out_movement = post_stock_movement(
                product=line.product,
                warehouse=transfer.from_warehouse,
                movement_date=transfer.document_date,
                movement_type=MovementType.TRANSFER_OUT,
                quantity=line.quantity,
                source=transfer,
                source_doc_type=DocumentType.STOCK_TRANSFER,
                source_doc_number=transfer.number,
                idempotency_key=f"ST:{transfer.pk}:{line.pk}:OUT",
                user=user,
            )
            in_movement = post_stock_movement(
                product=line.product,
                warehouse=transfer.to_warehouse,
                movement_date=transfer.document_date,
                movement_type=MovementType.TRANSFER_IN,
                quantity=line.quantity,
                unit_cost=out_movement.unit_cost,
                source=transfer,
                source_doc_type=DocumentType.STOCK_TRANSFER,
                source_doc_number=transfer.number,
                idempotency_key=f"ST:{transfer.pk}:{line.pk}:IN",
                user=user,
            )
            line.unit_cost = out_movement.unit_cost
            line.total_cost = out_movement.total_cost
            line.save(update_fields=["unit_cost", "total_cost"])

            total_cost += out_movement.total_cost
            movements.append(out_movement)
            movements.append(in_movement)

        transfer.total_cost_base = total_cost

        source_account = transfer.from_warehouse.inventory_account or _mapped_account(
            "INVENTORY"
        )
        dest_account = transfer.to_warehouse.inventory_account or _mapped_account("INVENTORY")

        journal_entry = None
        if total_cost > ZERO and source_account.pk != dest_account.pk:
            clearing_account = _mapped_account("STOCK_TRANSFER_CLEARING")
            company = Company.objects.first()
            if company is None:
                raise ValidationError(
                    "Company configuration is missing. Ask an administrator to set it up."
                )
            base_currency = company.base_currency

            journal_entry = JournalEntry.objects.create(
                number=_allocate_journal_number(transfer.document_date),
                entry_date=transfer.document_date,
                fiscal_period=fiscal_period,
                journal_type=JournalType.INVENTORY,
                narration=(
                    f"Stock transfer {transfer.number} — "
                    f"{transfer.from_warehouse} to {transfer.to_warehouse}"
                ),
                currency=base_currency,
                exchange_rate=Decimal("1"),
                total_debit_base=total_cost * 2,
                total_credit_base=total_cost * 2,
                source_content_type=ContentType.objects.get_for_model(transfer),
                source_object_id=transfer.pk,
                source_doc_type=DocumentType.STOCK_TRANSFER,
                source_doc_number=transfer.number,
                idempotency_key=f"ST:{transfer.pk}",
                posted_at=timezone.now(),
                posted_by=user,
            )
            JournalLine.objects.create(
                entry=journal_entry,
                line_no=1,
                account=dest_account,
                debit_txn=total_cost,
                debit_base=total_cost,
                credit_txn=ZERO,
                credit_base=ZERO,
                currency=base_currency,
                warehouse=transfer.to_warehouse,
                description=f"Received via {transfer.number}",
            )
            JournalLine.objects.create(
                entry=journal_entry,
                line_no=2,
                account=clearing_account,
                debit_txn=ZERO,
                debit_base=ZERO,
                credit_txn=total_cost,
                credit_base=total_cost,
                currency=base_currency,
                warehouse=transfer.to_warehouse,
                description=f"{transfer.number} clearing",
            )
            JournalLine.objects.create(
                entry=journal_entry,
                line_no=3,
                account=clearing_account,
                debit_txn=total_cost,
                debit_base=total_cost,
                credit_txn=ZERO,
                credit_base=ZERO,
                currency=base_currency,
                warehouse=transfer.from_warehouse,
                description=f"{transfer.number} clearing",
            )
            JournalLine.objects.create(
                entry=journal_entry,
                line_no=4,
                account=source_account,
                debit_txn=ZERO,
                debit_base=ZERO,
                credit_txn=total_cost,
                credit_base=total_cost,
                currency=base_currency,
                warehouse=transfer.from_warehouse,
                description=f"Shipped via {transfer.number}",
            )
            for movement in movements:
                movement.journal_entry = journal_entry
                movement.save(update_fields=["journal_entry"])
            transfer.journal_entry = journal_entry

        transfer.status = DocumentStatus.POSTED
        transfer.posted_at = timezone.now()
        transfer.posted_by = user
        transfer.save()
        audit.record_action(request, AuditAction.POST, transfer)

    return transfer


# ---------------------------------------------------------------------------
# Stock adjustments (INV-009, BR-017)
# ---------------------------------------------------------------------------
def _adjustment_line_cost_preview(adjustment, line):
    """
    A best-effort draft-time estimate only: an increase is costed at the
    line's own `unit_cost` (falling back to the product's standing purchase
    price), a decrease is costed at the warehouse's *current* average — both
    of which post_stock_adjustment recomputes for real at posting time.
    """
    if line.quantity_delta > 0:
        return _cost(line.unit_cost or line.product.purchase_price)
    balance = StockBalance.objects.filter(
        product=line.product, warehouse=adjustment.warehouse
    ).first()
    return balance.average_cost if balance else ZERO


def recalculate_adjustment(adjustment):
    total_value = ZERO
    for line in adjustment.lines.select_related("product"):
        line.unit_cost = _adjustment_line_cost_preview(adjustment, line)
        line.value_delta = _money(line.quantity_delta * line.unit_cost)
        line.save(update_fields=["unit_cost", "value_delta"])
        total_value += line.value_delta
    adjustment.total_value_base = total_value
    adjustment.save(update_fields=["total_value_base"])
    return adjustment


def validate_adjustment_directions(reason, cleaned_lines):
    """
    A reason is either an increase reason or a decrease reason, never mixed —
    `AdjustmentReason.increases_stock` says which. Returns the 1-based line
    numbers that disagree with it, so the view can show one clear message
    instead of a confusing posting-time result (nothing in the schema stops a
    "Damaged goods" reason from being used to raise stock, since only this
    check does).
    """
    bad_lines = []
    for index, line in enumerate(cleaned_lines, start=1):
        if not line or line.get("DELETE"):
            continue
        delta = line.get("quantity_delta") or ZERO
        if reason.increases_stock and delta <= 0:
            bad_lines.append(index)
        elif not reason.increases_stock and delta >= 0:
            bad_lines.append(index)
    return bad_lines


def submit_stock_adjustment(adjustment, user, request):
    """
    DRAFT/REJECTED -> SUBMITTED for sign-off, or straight to APPROVED when the
    reason doesn't require one (`AdjustmentReason.requires_approval`).
    """
    if adjustment.status not in (DocumentStatus.DRAFT, DocumentStatus.REJECTED):
        raise ValidationError("Only a draft or rejected adjustment can be submitted.")
    if not adjustment.lines.exists():
        raise ValidationError("Add at least one line before submitting.")

    with transaction.atomic():
        if not adjustment.reason.requires_approval:
            return approve_stock_adjustment(adjustment, user, request)
        adjustment.status = DocumentStatus.SUBMITTED
        adjustment.updated_by = user
        adjustment.save()
        audit.record_action(request, AuditAction.SUBMIT, adjustment)
    return adjustment


def approve_stock_adjustment(adjustment, user, request):
    if adjustment.status not in (
        DocumentStatus.DRAFT,
        DocumentStatus.SUBMITTED,
        DocumentStatus.REJECTED,
    ):
        raise ValidationError(
            "Only a draft, submitted or rejected adjustment can be approved."
        )
    with transaction.atomic():
        adjustment.status = DocumentStatus.APPROVED
        adjustment.approved_at = timezone.now()
        adjustment.approved_by = user
        adjustment.updated_by = user
        adjustment.save()
        audit.record_action(request, AuditAction.APPROVE, adjustment)
    return adjustment


def reject_stock_adjustment(adjustment, user, reason, request):
    if adjustment.status != DocumentStatus.SUBMITTED:
        raise ValidationError("Only a submitted adjustment can be rejected.")
    if not (reason or "").strip():
        raise ValidationError("Give a reason for rejecting this adjustment.")
    with transaction.atomic():
        adjustment.status = DocumentStatus.REJECTED
        adjustment.updated_by = user
        adjustment.save()
        audit.record_action(request, AuditAction.REJECT, adjustment, reason=reason)
    return adjustment


def post_stock_adjustment(adjustment, user, request):
    """
    Posts each line as an ADJUSTMENT_IN or ADJUSTMENT_OUT movement (sign of
    `quantity_delta` decides which) through the shared costing engine, then
    books the net value against the reason's gain/loss account (INV-009):
    Dr Inventory / Cr the account for a net increase, the reverse for a net
    decrease.

    Always requires APPROVED: `submit_stock_adjustment` already collapses
    "this reason needs sign-off" and "it doesn't" into the same end state —
    either straight to APPROVED, or via SUBMITTED once someone signs off —
    so posting never needs to branch on the reason itself.
    """
    if adjustment.status != DocumentStatus.APPROVED:
        raise ValidationError("Only an approved adjustment can be posted.")
    lines = list(adjustment.lines.select_related("product"))
    if not lines:
        raise ValidationError("Add at least one line before posting.")

    with transaction.atomic():
        fiscal_period = _fiscal_period_for(adjustment.document_date)
        movements = []
        total_value = ZERO

        for line in lines:
            is_increase = line.quantity_delta > 0
            movement = post_stock_movement(
                product=line.product,
                warehouse=adjustment.warehouse,
                movement_date=adjustment.document_date,
                movement_type=(
                    MovementType.ADJUSTMENT_IN if is_increase else MovementType.ADJUSTMENT_OUT
                ),
                quantity=abs(line.quantity_delta),
                unit_cost=line.unit_cost if is_increase else None,
                source=adjustment,
                source_doc_type=DocumentType.STOCK_ADJUSTMENT,
                source_doc_number=adjustment.number,
                idempotency_key=f"SA:{adjustment.pk}:{line.pk}",
                user=user,
            )
            signed_value = movement.total_cost if is_increase else -movement.total_cost
            line.unit_cost = movement.unit_cost
            line.value_delta = signed_value
            line.save(update_fields=["unit_cost", "value_delta"])

            total_value += signed_value
            movements.append(movement)

        if total_value == ZERO:
            raise ValidationError(
                "This adjustment has no cost effect (every line values at zero) "
                "and cannot be posted."
            )

        adjustment.total_value_base = total_value

        inventory_account = adjustment.warehouse.inventory_account or _mapped_account(
            "INVENTORY"
        )
        if adjustment.reason.increases_stock:
            gain_loss_account = adjustment.reason.gain_loss_account or _mapped_account(
                "INVENTORY_GAIN"
            )
            debit_account, credit_account = inventory_account, gain_loss_account
        else:
            gain_loss_account = adjustment.reason.gain_loss_account or _mapped_account(
                "INVENTORY_LOSS"
            )
            debit_account, credit_account = gain_loss_account, inventory_account
        amount = abs(total_value)

        company = Company.objects.first()
        if company is None:
            raise ValidationError(
                "Company configuration is missing. Ask an administrator to set it up."
            )
        base_currency = company.base_currency

        journal_entry = JournalEntry.objects.create(
            number=_allocate_journal_number(adjustment.document_date),
            entry_date=adjustment.document_date,
            fiscal_period=fiscal_period,
            journal_type=JournalType.INVENTORY,
            narration=f"Stock adjustment {adjustment.number} — {adjustment.reason}",
            currency=base_currency,
            exchange_rate=Decimal("1"),
            total_debit_base=amount,
            total_credit_base=amount,
            source_content_type=ContentType.objects.get_for_model(adjustment),
            source_object_id=adjustment.pk,
            source_doc_type=DocumentType.STOCK_ADJUSTMENT,
            source_doc_number=adjustment.number,
            idempotency_key=f"SA:{adjustment.pk}",
            posted_at=timezone.now(),
            posted_by=user,
        )
        JournalLine.objects.create(
            entry=journal_entry,
            line_no=1,
            account=debit_account,
            debit_txn=amount,
            debit_base=amount,
            credit_txn=ZERO,
            credit_base=ZERO,
            currency=base_currency,
            warehouse=adjustment.warehouse,
            description=f"{adjustment.number} — {adjustment.reason}",
        )
        JournalLine.objects.create(
            entry=journal_entry,
            line_no=2,
            account=credit_account,
            debit_txn=ZERO,
            debit_base=ZERO,
            credit_txn=amount,
            credit_base=amount,
            currency=base_currency,
            warehouse=adjustment.warehouse,
            description=f"{adjustment.number} — {adjustment.reason}",
        )
        for movement in movements:
            movement.journal_entry = journal_entry
            movement.save(update_fields=["journal_entry"])
        adjustment.journal_entry = journal_entry

        adjustment.status = DocumentStatus.POSTED
        adjustment.posted_at = timezone.now()
        adjustment.posted_by = user
        adjustment.save()
        audit.record_action(request, AuditAction.POST, adjustment)

    return adjustment


# ---------------------------------------------------------------------------
# Delivery note (INV-007, SAL-005, SAL-010, GL-001, GL-002)
# ---------------------------------------------------------------------------
def recalculate_delivery(delivery):
    """
    Estimate each line's cost from the warehouse's *current* average (a
    preview only, same as `recalculate_transfer` — the authoritative figure
    is fixed at posting time, since the average can move between saving a
    draft and posting it).
    """
    total_cost = ZERO
    for line in delivery.lines.select_related("product"):
        balance = StockBalance.objects.filter(
            product=line.product, warehouse=delivery.warehouse
        ).first()
        line.unit_cost = balance.average_cost if balance else ZERO
        line.total_cost = _money(line.quantity * line.unit_cost)
        line.save(update_fields=["unit_cost", "total_cost"])
        total_cost += line.total_cost
    delivery.total_cost_base = total_cost
    delivery.save(update_fields=["total_cost_base"])
    return delivery


def post_delivery(delivery, user, request):
    """
    Moves stock out and books the cost of goods sold (INV-007, SAL-010):
    every line becomes a DELIVERY movement through the shared costing
    engine — costed at the warehouse's *current* weighted average, which
    is exactly what COGS means (INV-005) — and Cost of Goods Sold is
    debited against Inventory for the total, unless every line happened to
    cost zero (a product that has never carried a value), in which case
    there is nothing to post. Mirrors `post_goods_receipt` with the
    direction and accounts swapped: outbound instead of inbound, COGS
    instead of GRNI.
    """
    if delivery.status != DocumentStatus.DRAFT:
        raise ValidationError("Only a draft delivery can be posted.")
    lines = list(delivery.lines.select_related("product", "sales_order_line"))
    if not lines:
        raise ValidationError("Add at least one line before posting.")

    with transaction.atomic():
        fiscal_period = _fiscal_period_for(delivery.document_date)
        movements = []
        total_cost = ZERO

        for line in lines:
            movement = post_stock_movement(
                product=line.product,
                warehouse=delivery.warehouse,
                movement_date=delivery.document_date,
                movement_type=MovementType.DELIVERY,
                quantity=line.quantity,
                source=delivery,
                source_doc_type=DocumentType.DELIVERY_NOTE,
                source_doc_number=delivery.number,
                idempotency_key=f"DN:{delivery.pk}:{line.pk}",
                user=user,
            )
            line.unit_cost = movement.unit_cost
            line.total_cost = movement.total_cost
            line.save(update_fields=["unit_cost", "total_cost"])

            total_cost += movement.total_cost
            movements.append(movement)

            if line.sales_order_line_id:
                so_line = line.sales_order_line
                so_line.quantity_delivered = so_line.quantity_delivered + line.quantity
                so_line.save(update_fields=["quantity_delivered"])

        delivery.total_cost_base = total_cost

        journal_entry = None
        if total_cost > ZERO:
            cogs_account = _mapped_account("COGS")
            inventory_account = _mapped_account("INVENTORY")
            company = Company.objects.first()
            if company is None:
                raise ValidationError(
                    "Company configuration is missing. Ask an administrator to set it up."
                )
            base_currency = company.base_currency

            journal_entry = JournalEntry.objects.create(
                number=_allocate_journal_number(delivery.document_date),
                entry_date=delivery.document_date,
                fiscal_period=fiscal_period,
                journal_type=JournalType.INVENTORY,
                narration=f"Delivery {delivery.number} — {delivery.customer}",
                currency=base_currency,
                exchange_rate=Decimal("1"),
                total_debit_base=total_cost,
                total_credit_base=total_cost,
                source_content_type=ContentType.objects.get_for_model(delivery),
                source_object_id=delivery.pk,
                source_doc_type=DocumentType.DELIVERY_NOTE,
                source_doc_number=delivery.number,
                idempotency_key=f"DN:{delivery.pk}",
                posted_at=timezone.now(),
                posted_by=user,
            )
            JournalLine.objects.create(
                entry=journal_entry,
                line_no=1,
                account=cogs_account,
                debit_txn=total_cost,
                debit_base=total_cost,
                credit_txn=ZERO,
                credit_base=ZERO,
                currency=base_currency,
                warehouse=delivery.warehouse,
                description=f"Cost of goods sold on {delivery.number}",
            )
            JournalLine.objects.create(
                entry=journal_entry,
                line_no=2,
                account=inventory_account,
                debit_txn=ZERO,
                debit_base=ZERO,
                credit_txn=total_cost,
                credit_base=total_cost,
                currency=base_currency,
                warehouse=delivery.warehouse,
                description=f"Shipped on {delivery.number}",
            )
            for movement in movements:
                movement.journal_entry = journal_entry
                movement.save(update_fields=["journal_entry"])
            delivery.journal_entry = journal_entry

        delivery.status = DocumentStatus.POSTED
        delivery.posted_at = timezone.now()
        delivery.posted_by = user
        delivery.save()
        audit.record_action(request, AuditAction.POST, delivery)

    return delivery
