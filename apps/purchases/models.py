"""
Purchase cycle: orders, bills, returns and vendor debit notes.
(Goods receipts live in `inventory` per BRD 11.2 app boundaries.)

BRD coverage: PUR-001..PUR-012, RET-005..RET-010, BR-010..BR-013, BR-015,
FTD-006..FTD-009, Appendix A.
"""

from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import F, Q

from apps.core.models import (
    MONEY,
    PCT,
    QTY,
    DocumentLineBase,
    DocumentStatus,
    FinancialDocumentBase,
    TimeStampedModel,
)
from apps.sales.models import DiscountKind, ReturnDisposition

ZERO = Decimal("0")


class ThreeWayMatchStatus(models.TextChoices):
    """PUR-003 / PUR-012: how a PO line's ordered qty compares to what was
    received and billed against it."""

    OPEN = "OPEN", "Not received"
    PARTIAL = "PARTIAL", "Partial"
    MATCHED = "MATCHED", "Matched"
    OVER = "OVER", "Over-billed"
    CANCELLED = "CANCELLED", "Cancelled"


# ---------------------------------------------------------------------------
# Purchase order (PUR-001, PUR-002)
# ---------------------------------------------------------------------------
class PurchaseOrder(FinancialDocumentBase):
    vendor = models.ForeignKey(
        "parties.Vendor", on_delete=models.PROTECT, related_name="purchase_orders"
    )
    warehouse = models.ForeignKey(
        "inventory.Warehouse", on_delete=models.PROTECT, related_name="+"
    )
    payment_term = models.ForeignKey(
        "core.PaymentTerm", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    expected_date = models.DateField(null=True, blank=True)
    vendor_reference = models.CharField(max_length=64, blank=True)
    delivery_address_text = models.TextField(blank=True)
    buyer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    document_discount_kind = models.CharField(
        max_length=8, choices=DiscountKind.choices, default=DiscountKind.NONE
    )
    document_discount_value = models.DecimalField(**PCT, default=ZERO)

    class Meta:
        db_table = "purchase_order"
        ordering = ["-document_date", "-id"]
        constraints = [
            models.CheckConstraint(condition=Q(exchange_rate__gt=0), name="po_rate_positive"),
            models.CheckConstraint(
                condition=Q(total_txn__gte=0) & Q(total_base__gte=0), name="po_total_nonneg"
            ),
        ]
        indexes = [
            models.Index(fields=["vendor", "-document_date"], name="ix_po_vendor_date"),
            models.Index(fields=["status", "-document_date"], name="ix_po_status_date"),
        ]

    def get_absolute_url(self):
        from django.urls import reverse

        return reverse("purchases:po_detail", args=[self.pk])


class PurchaseOrderLine(DocumentLineBase):
    order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey("catalog.Product", on_delete=models.PROTECT, related_name="+")
    unit = models.ForeignKey(
        "catalog.UnitOfMeasure", on_delete=models.PROTECT, related_name="+"
    )
    tax_code = models.ForeignKey(
        "core.TaxCode", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    warehouse = models.ForeignKey(
        "inventory.Warehouse",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    # PUR-003 / PUR-012 three-way match counters
    quantity_received = models.DecimalField(**QTY, default=ZERO)
    quantity_billed = models.DecimalField(**QTY, default=ZERO)
    quantity_cancelled = models.DecimalField(**QTY, default=ZERO)

    class Meta:
        db_table = "purchase_order_line"
        ordering = ["order", "line_no"]
        constraints = [
            models.UniqueConstraint(fields=["order", "line_no"], name="po_line_unique_no"),
            models.CheckConstraint(condition=Q(quantity__gt=0), name="po_line_qty_positive"),
            models.CheckConstraint(
                condition=Q(unit_price__gte=0), name="po_line_price_nonneg"
            ),
            models.CheckConstraint(
                condition=Q(quantity_received__gte=0) & Q(quantity_billed__gte=0),
                name="po_line_progress_nonneg",
            ),
            models.CheckConstraint(
                condition=Q(discount_percent__gte=0) & Q(discount_percent__lte=100),
                name="po_line_discount_range",
            ),
        ]
        indexes = [models.Index(fields=["product"], name="ix_po_line_product")]

    @property
    def match_status(self):
        """Three-way match: ordered (minus cancelled) vs received vs billed."""
        effective_qty = self.quantity - self.quantity_cancelled
        if effective_qty <= 0:
            return ThreeWayMatchStatus.CANCELLED
        if self.quantity_billed > self.quantity_received:
            return ThreeWayMatchStatus.OVER
        if self.quantity_received >= effective_qty and self.quantity_billed >= effective_qty:
            return ThreeWayMatchStatus.MATCHED
        if self.quantity_received > 0 or self.quantity_billed > 0:
            return ThreeWayMatchStatus.PARTIAL
        return ThreeWayMatchStatus.OPEN

    def get_match_status_display(self):
        return ThreeWayMatchStatus(self.match_status).label


# ---------------------------------------------------------------------------
# Purchase bill (PUR-005..PUR-011)
# ---------------------------------------------------------------------------
class PurchaseBill(FinancialDocumentBase):
    vendor = models.ForeignKey(
        "parties.Vendor", on_delete=models.PROTECT, related_name="bills"
    )
    warehouse = models.ForeignKey(
        "inventory.Warehouse",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    purchase_order = models.ForeignKey(
        PurchaseOrder, null=True, blank=True, on_delete=models.PROTECT, related_name="bills"
    )
    goods_receipt = models.ForeignKey(
        "inventory.GoodsReceipt",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="bills",
    )
    payment_term = models.ForeignKey(
        "core.PaymentTerm", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    # PUR-006: the vendor's own invoice number, unique per vendor.
    vendor_invoice_number = models.CharField(max_length=64)
    vendor_invoice_date = models.DateField(null=True, blank=True)
    duplicate_override_reason = models.CharField(max_length=255, blank=True)

    vendor_name_snapshot = models.CharField(max_length=200, blank=True)
    vendor_tax_id_snapshot = models.CharField(max_length=50, blank=True)
    billing_address_text = models.TextField(blank=True)

    document_discount_kind = models.CharField(
        max_length=8, choices=DiscountKind.choices, default=DiscountKind.NONE
    )
    document_discount_value = models.DecimalField(**PCT, default=ZERO)
    payable_account = models.ForeignKey(
        "ledger.Account", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    is_reversed = models.BooleanField(default=False)

    class Meta:
        db_table = "purchase_bill"
        ordering = ["-document_date", "-id"]
        constraints = [
            # PUR-006: hard duplicate detection per vendor.
            models.UniqueConstraint(
                fields=["vendor", "vendor_invoice_number"],
                name="pb_vendor_invoice_unique",
            ),
            models.CheckConstraint(condition=Q(exchange_rate__gt=0), name="pb_rate_positive"),
            models.CheckConstraint(
                condition=Q(total_txn__gte=0) & Q(total_base__gte=0), name="pb_total_nonneg"
            ),
            models.CheckConstraint(
                condition=Q(allocated_txn__gte=0)
                & Q(credited_txn__gte=0)
                & Q(allocated_txn__lte=F("total_txn"))
                & Q(credited_txn__lte=F("total_txn")),
                name="pb_settlement_within_total",
            ),
            models.CheckConstraint(condition=Q(open_txn__gte=0), name="pb_open_nonneg"),
            models.CheckConstraint(
                condition=Q(open_txn=F("total_txn") - F("allocated_txn") - F("credited_txn")),
                name="pb_open_is_derived",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(status__in=["POSTED", "PARTIAL", "COMPLETED", "REVERSED"])
                    | Q(journal_entry__isnull=False)
                ),
                name="pb_posted_has_journal",
            ),
            models.CheckConstraint(
                condition=Q(due_date__isnull=True) | Q(due_date__gte=F("document_date")),
                name="pb_due_after_document_date",
            ),
        ]
        indexes = [
            models.Index(fields=["vendor", "-document_date"], name="ix_pb_vendor_date"),
            models.Index(fields=["status", "due_date"], name="ix_pb_status_due"),
            models.Index(fields=["posting_date"], name="ix_pb_posting_date"),
            models.Index(
                fields=["vendor", "due_date"],
                condition=Q(open_txn__gt=0),
                name="ix_pb_open_items",
            ),
        ]

    def get_absolute_url(self):
        from django.urls import reverse

        return reverse("purchases:bill_detail", args=[self.pk])


class PurchaseBillLine(DocumentLineBase):
    bill = models.ForeignKey(PurchaseBill, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey(
        "catalog.Product", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    unit = models.ForeignKey(
        "catalog.UnitOfMeasure",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    tax_code = models.ForeignKey(
        "core.TaxCode", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    warehouse = models.ForeignKey(
        "inventory.Warehouse",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    purchase_order_line = models.ForeignKey(
        PurchaseOrderLine, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    receipt_line = models.ForeignKey(
        "inventory.GoodsReceiptLine",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="bill_lines",
    )
    # Appendix A: stock lines hit Inventory, non-stock lines hit an expense account.
    is_stock_line = models.BooleanField(default=True)
    expense_account = models.ForeignKey(
        "ledger.Account", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    product_sku_snapshot = models.CharField(max_length=40, blank=True)
    quantity_returned = models.DecimalField(**QTY, default=ZERO)

    class Meta:
        db_table = "purchase_bill_line"
        ordering = ["bill", "line_no"]
        constraints = [
            models.UniqueConstraint(fields=["bill", "line_no"], name="pb_line_unique_no"),
            models.CheckConstraint(condition=Q(quantity__gt=0), name="pb_line_qty_positive"),
            models.CheckConstraint(
                condition=Q(unit_price__gte=0), name="pb_line_price_nonneg"
            ),
            models.CheckConstraint(
                condition=Q(line_discount_txn__lte=F("gross_txn")),
                name="pb_line_discount_within_gross",
            ),
            models.CheckConstraint(
                condition=Q(quantity_returned__gte=0)
                & Q(quantity_returned__lte=F("quantity")),
                name="pb_line_returned_within_billed",
            ),
            # A stock line must name a product; an expense line must name an account.
            models.CheckConstraint(
                condition=(
                    (Q(is_stock_line=True) & Q(product__isnull=False))
                    | (Q(is_stock_line=False) & Q(expense_account__isnull=False))
                ),
                name="pb_line_target_present",
            ),
            models.CheckConstraint(
                condition=Q(tax_rate_percent__gte=0) & Q(tax_rate_percent__lte=100),
                name="pb_line_tax_rate_range",
            ),
        ]
        indexes = [
            models.Index(fields=["product"], name="ix_pb_line_product"),
            models.Index(fields=["tax_code"], name="ix_pb_line_tax"),
        ]


# ---------------------------------------------------------------------------
# Purchase return (RET-005, RET-008)
# ---------------------------------------------------------------------------
class PurchaseReturn(TimeStampedModel):
    number = models.CharField(max_length=32, unique=True)
    document_date = models.DateField(db_index=True)
    vendor = models.ForeignKey(
        "parties.Vendor", on_delete=models.PROTECT, related_name="returns"
    )
    warehouse = models.ForeignKey(
        "inventory.Warehouse", on_delete=models.PROTECT, related_name="+"
    )
    original_bill = models.ForeignKey(
        PurchaseBill, null=True, blank=True, on_delete=models.PROTECT, related_name="returns"
    )
    original_receipt = models.ForeignKey(
        "inventory.GoodsReceipt",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="returns",
    )
    status = models.CharField(
        max_length=10, choices=DocumentStatus.choices, default=DocumentStatus.DRAFT
    )
    reason = models.TextField(help_text="Mandatory (RET-008).")
    total_cost_base = models.DecimalField(**MONEY, default=ZERO)
    journal_entry = models.ForeignKey(
        "ledger.JournalEntry",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    posted_at = models.DateTimeField(null=True, blank=True)
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )

    class Meta:
        db_table = "purchase_return"
        ordering = ["-document_date", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(original_bill__isnull=False) | Q(original_receipt__isnull=False),
                name="purchase_return_has_source",
            ),
            models.CheckConstraint(
                condition=~Q(reason=""), name="purchase_return_reason_required"
            ),
        ]
        indexes = [models.Index(fields=["vendor", "-document_date"], name="ix_pr_vendor_date")]

    def __str__(self):
        return self.number

    def get_absolute_url(self):
        from django.urls import reverse

        return reverse("purchases:pr_detail", args=[self.pk])


class PurchaseReturnLine(models.Model):
    purchase_return = models.ForeignKey(
        PurchaseReturn, on_delete=models.CASCADE, related_name="lines"
    )
    line_no = models.PositiveSmallIntegerField()
    bill_line = models.ForeignKey(
        PurchaseBillLine,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="return_lines",
    )
    receipt_line = models.ForeignKey(
        "inventory.GoodsReceiptLine",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="return_lines",
    )
    product = models.ForeignKey("catalog.Product", on_delete=models.PROTECT, related_name="+")
    quantity = models.DecimalField(**QTY)
    disposition = models.CharField(
        max_length=16, choices=ReturnDisposition.choices, default=ReturnDisposition.RESTOCK
    )
    unit_cost = models.DecimalField(**MONEY, default=ZERO)
    total_cost = models.DecimalField(**MONEY, default=ZERO)
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = "purchase_return_line"
        ordering = ["purchase_return", "line_no"]
        constraints = [
            models.UniqueConstraint(
                fields=["purchase_return", "line_no"], name="pr_line_unique_no"
            ),
            models.CheckConstraint(condition=Q(quantity__gt=0), name="pr_line_qty_positive"),
        ]

    def __str__(self):
        return f"{self.purchase_return_id}/{self.line_no} {self.quantity}"


# ---------------------------------------------------------------------------
# Vendor debit note (RET-006, RET-007)
# ---------------------------------------------------------------------------
class VendorDebitNote(FinancialDocumentBase):
    """
    Reduces AP and reverses inventory/expense and input tax on the original
    basis (RET-006). Unapplied balance is a receivable from the vendor that can
    be applied to open bills or refunded (RET-007).
    """

    vendor = models.ForeignKey(
        "parties.Vendor", on_delete=models.PROTECT, related_name="debit_notes"
    )
    original_bill = models.ForeignKey(
        PurchaseBill,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="debit_notes",
    )
    purchase_return = models.ForeignKey(
        PurchaseReturn,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="debit_notes",
    )
    vendor_credit_reference = models.CharField(max_length=64, blank=True)
    reason = models.TextField(help_text="Mandatory (RET-008).")
    vendor_name_snapshot = models.CharField(max_length=200, blank=True)
    refunded_txn = models.DecimalField(**MONEY, default=ZERO)
    is_reversed = models.BooleanField(default=False)

    class Meta:
        db_table = "vendor_debit_note"
        ordering = ["-document_date", "-id"]
        constraints = [
            models.CheckConstraint(condition=Q(exchange_rate__gt=0), name="dbn_rate_positive"),
            models.CheckConstraint(condition=Q(total_txn__gte=0), name="dbn_total_nonneg"),
            models.CheckConstraint(
                condition=Q(allocated_txn__gte=0)
                & Q(refunded_txn__gte=0)
                & Q(allocated_txn__lte=F("total_txn"))
                & Q(refunded_txn__lte=F("total_txn")),
                name="dbn_settlement_within_total",
            ),
            models.CheckConstraint(
                condition=Q(open_txn=F("total_txn") - F("allocated_txn") - F("refunded_txn")),
                name="dbn_open_is_derived",
            ),
            models.CheckConstraint(condition=Q(open_txn__gte=0), name="dbn_open_nonneg"),
            models.CheckConstraint(
                condition=(
                    ~Q(status__in=["POSTED", "PARTIAL", "COMPLETED", "REVERSED"])
                    | Q(journal_entry__isnull=False)
                ),
                name="dbn_posted_has_journal",
            ),
        ]
        indexes = [
            models.Index(fields=["vendor", "-document_date"], name="ix_dbn_vendor_date"),
            models.Index(
                fields=["vendor"], condition=Q(open_txn__gt=0), name="ix_dbn_open_credit"
            ),
        ]

    def get_absolute_url(self):
        from django.urls import reverse

        return reverse("purchases:dbn_detail", args=[self.pk])


class VendorDebitNoteLine(DocumentLineBase):
    debit_note = models.ForeignKey(
        VendorDebitNote, on_delete=models.CASCADE, related_name="lines"
    )
    product = models.ForeignKey(
        "catalog.Product", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    unit = models.ForeignKey(
        "catalog.UnitOfMeasure",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    tax_code = models.ForeignKey(
        "core.TaxCode", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    bill_line = models.ForeignKey(
        PurchaseBillLine,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="debit_lines",
    )
    return_line = models.ForeignKey(
        PurchaseReturnLine, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    is_stock_line = models.BooleanField(default=True)
    expense_account = models.ForeignKey(
        "ledger.Account", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        db_table = "vendor_debit_note_line"
        ordering = ["debit_note", "line_no"]
        constraints = [
            models.UniqueConstraint(
                fields=["debit_note", "line_no"], name="dbn_line_unique_no"
            ),
            models.CheckConstraint(condition=Q(quantity__gt=0), name="dbn_line_qty_positive"),
            models.CheckConstraint(
                condition=Q(tax_rate_percent__gte=0) & Q(tax_rate_percent__lte=100),
                name="dbn_line_tax_rate_range",
            ),
        ]
        indexes = [models.Index(fields=["product"], name="ix_dbn_line_product")]
