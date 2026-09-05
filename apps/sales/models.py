"""
Sales cycle: orders, invoices, returns and credit notes.
(Delivery notes live in `inventory` per BRD 11.2 app boundaries.)

BRD coverage: SAL-001..SAL-014, RET-001..RET-004, RET-008..RET-010,
BR-010, BR-011, BR-012, BR-015, FTD-006..FTD-009, Appendix A.
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

ZERO = Decimal("0")


class DiscountKind(models.TextChoices):
    NONE = "NONE", "None"
    PERCENT = "PERCENT", "Percentage"
    AMOUNT = "AMOUNT", "Fixed amount"


# ---------------------------------------------------------------------------
# Sales order (SAL-001..SAL-004)
# ---------------------------------------------------------------------------
class SalesOrder(FinancialDocumentBase):
    customer = models.ForeignKey(
        "parties.Customer", on_delete=models.PROTECT, related_name="sales_orders"
    )
    warehouse = models.ForeignKey(
        "inventory.Warehouse", on_delete=models.PROTECT, related_name="+"
    )
    payment_term = models.ForeignKey(
        "core.PaymentTerm", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    expected_date = models.DateField(null=True, blank=True)
    customer_reference = models.CharField(
        max_length=64,
        blank=True,
        help_text="The customer’s own purchase order number, printed on their invoice.",
    )
    billing_address_text = models.TextField(blank=True)
    shipping_address_text = models.TextField(blank=True)
    salesperson = models.ForeignKey(
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
        db_table = "sales_order"
        ordering = ["-document_date", "-id"]
        constraints = [
            models.CheckConstraint(condition=Q(exchange_rate__gt=0), name="so_rate_positive"),
            models.CheckConstraint(
                condition=Q(total_txn__gte=0) & Q(total_base__gte=0), name="so_total_nonneg"
            ),
        ]
        indexes = [
            models.Index(fields=["customer", "-document_date"], name="ix_so_customer_date"),
            models.Index(fields=["status", "-document_date"], name="ix_so_status_date"),
            models.Index(fields=["customer_reference"], name="ix_so_customer_ref"),
        ]

    def get_absolute_url(self):
        from django.urls import reverse

        return reverse("sales:so_detail", args=[self.pk])


class SalesOrderLine(DocumentLineBase):
    order = models.ForeignKey(SalesOrder, on_delete=models.CASCADE, related_name="lines")
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
    # SAL-005 / SAL-006 fulfilment counters
    quantity_delivered = models.DecimalField(**QTY, default=ZERO)
    quantity_invoiced = models.DecimalField(**QTY, default=ZERO)
    quantity_cancelled = models.DecimalField(**QTY, default=ZERO)

    class Meta:
        db_table = "sales_order_line"
        ordering = ["order", "line_no"]
        constraints = [
            models.UniqueConstraint(fields=["order", "line_no"], name="so_line_unique_no"),
            models.CheckConstraint(condition=Q(quantity__gt=0), name="so_line_qty_positive"),
            models.CheckConstraint(
                condition=Q(unit_price__gte=0), name="so_line_price_nonneg"
            ),
            models.CheckConstraint(
                condition=Q(discount_percent__gte=0) & Q(discount_percent__lte=100),
                name="so_line_discount_range",
            ),
            # SAL-005: over-delivery is blocked unless authorised (service-level
            # override raises quantity, which this constraint then permits).
            models.CheckConstraint(
                condition=Q(quantity_delivered__gte=0) & Q(quantity_invoiced__gte=0),
                name="so_line_fulfilment_nonneg",
            ),
        ]
        indexes = [models.Index(fields=["product"], name="ix_so_line_product")]


# ---------------------------------------------------------------------------
# Sales invoice (SAL-006..SAL-012)
# ---------------------------------------------------------------------------
class SalesInvoice(FinancialDocumentBase):
    customer = models.ForeignKey(
        "parties.Customer", on_delete=models.PROTECT, related_name="invoices"
    )
    warehouse = models.ForeignKey(
        "inventory.Warehouse",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    sales_order = models.ForeignKey(
        SalesOrder, null=True, blank=True, on_delete=models.PROTECT, related_name="invoices"
    )
    payment_term = models.ForeignKey(
        "core.PaymentTerm", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    customer_reference = models.CharField(max_length=64, blank=True)

    # PTY-003: snapshots so a reprint never changes.
    customer_name_snapshot = models.CharField(max_length=200, blank=True)
    customer_tax_id_snapshot = models.CharField(max_length=50, blank=True)
    billing_address_text = models.TextField(blank=True)
    shipping_address_text = models.TextField(blank=True)

    document_discount_kind = models.CharField(
        max_length=8, choices=DiscountKind.choices, default=DiscountKind.NONE
    )
    document_discount_value = models.DecimalField(**PCT, default=ZERO)

    receivable_account = models.ForeignKey(
        "ledger.Account", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    is_reversed = models.BooleanField(default=False)
    reversed_by_journal = models.ForeignKey(
        "ledger.JournalEntry",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )

    class Meta:
        db_table = "sales_invoice"
        ordering = ["-document_date", "-id"]
        constraints = [
            models.CheckConstraint(condition=Q(exchange_rate__gt=0), name="si_rate_positive"),
            models.CheckConstraint(
                condition=Q(total_txn__gte=0) & Q(total_base__gte=0), name="si_total_nonneg"
            ),
            # BR-009: settlement can never exceed the invoice.
            models.CheckConstraint(
                condition=Q(allocated_txn__gte=0)
                & Q(credited_txn__gte=0)
                & Q(allocated_txn__lte=F("total_txn"))
                & Q(credited_txn__lte=F("total_txn")),
                name="si_settlement_within_total",
            ),
            # The open amount is derived and must never go negative (BR-009).
            models.CheckConstraint(condition=Q(open_txn__gte=0), name="si_open_nonneg"),
            models.CheckConstraint(
                condition=Q(open_txn=F("total_txn") - F("allocated_txn") - F("credited_txn")),
                name="si_open_is_derived",
            ),
            # SAL-009 / SC-03: a posted invoice always carries its journal.
            models.CheckConstraint(
                condition=(
                    ~Q(status__in=["POSTED", "PARTIAL", "COMPLETED", "REVERSED"])
                    | Q(journal_entry__isnull=False)
                ),
                name="si_posted_has_journal",
            ),
            models.CheckConstraint(
                condition=Q(due_date__isnull=True) | Q(due_date__gte=F("document_date")),
                name="si_due_after_document_date",
            ),
        ]
        indexes = [
            models.Index(fields=["customer", "-document_date"], name="ix_si_customer_date"),
            models.Index(fields=["status", "due_date"], name="ix_si_status_due"),
            models.Index(fields=["posting_date"], name="ix_si_posting_date"),
            # RPT-006 / RPT-022: the ageing and outstanding-document queries.
            models.Index(
                fields=["customer", "due_date"],
                condition=Q(open_txn__gt=0),
                name="ix_si_open_items",
            ),
            models.Index(fields=["currency", "status"], name="ix_si_currency_status"),
        ]

    def get_absolute_url(self):
        from django.urls import reverse

        return reverse("sales:invoice_detail", args=[self.pk])


class SalesInvoiceLine(DocumentLineBase):
    invoice = models.ForeignKey(SalesInvoice, on_delete=models.CASCADE, related_name="lines")
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
    sales_order_line = models.ForeignKey(
        SalesOrderLine, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    delivery_line = models.ForeignKey(
        "inventory.DeliveryNoteLine",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="invoice_lines",
    )
    # Posting targets snapshotted at posting (CFG-007).
    revenue_account = models.ForeignKey(
        "ledger.Account", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    product_sku_snapshot = models.CharField(max_length=40, blank=True)
    quantity_returned = models.DecimalField(**QTY, default=ZERO)  # RET-001

    class Meta:
        db_table = "sales_invoice_line"
        ordering = ["invoice", "line_no"]
        constraints = [
            models.UniqueConstraint(fields=["invoice", "line_no"], name="si_line_unique_no"),
            models.CheckConstraint(condition=Q(quantity__gt=0), name="si_line_qty_positive"),
            models.CheckConstraint(
                condition=Q(unit_price__gte=0), name="si_line_price_nonneg"
            ),
            models.CheckConstraint(
                condition=Q(line_discount_txn__gte=0)
                & Q(allocated_document_discount_txn__gte=0),
                name="si_line_discount_nonneg",
            ),
            # FTD-008: discount cannot exceed the gross it is applied to.
            models.CheckConstraint(
                condition=Q(line_discount_txn__lte=F("gross_txn")),
                name="si_line_discount_within_gross",
            ),
            # BR-015 eligibility ceiling for returns.
            models.CheckConstraint(
                condition=Q(quantity_returned__gte=0)
                & Q(quantity_returned__lte=F("quantity")),
                name="si_line_returned_within_invoiced",
            ),
            models.CheckConstraint(
                condition=Q(tax_rate_percent__gte=0) & Q(tax_rate_percent__lte=100),
                name="si_line_tax_rate_range",
            ),
        ]
        indexes = [
            models.Index(fields=["product"], name="ix_si_line_product"),
            models.Index(fields=["tax_code"], name="ix_si_line_tax"),
        ]


# ---------------------------------------------------------------------------
# Sales return (RET-001, RET-002, RET-008)
# ---------------------------------------------------------------------------
class ReturnDisposition(models.TextChoices):
    RESTOCK = "RESTOCK", "Return to saleable stock"
    WRITE_OFF = "WRITE_OFF", "Damaged / write-off"
    NO_STOCK_EFFECT = "NO_STOCK_EFFECT", "No stock effect"


class SalesReturn(TimeStampedModel):
    """Physical/authorisation side of a customer return. Money follows on a credit note."""

    number = models.CharField(max_length=32, unique=True)
    document_date = models.DateField(db_index=True)
    customer = models.ForeignKey(
        "parties.Customer", on_delete=models.PROTECT, related_name="returns"
    )
    warehouse = models.ForeignKey(
        "inventory.Warehouse", on_delete=models.PROTECT, related_name="+"
    )
    original_invoice = models.ForeignKey(
        SalesInvoice, null=True, blank=True, on_delete=models.PROTECT, related_name="returns"
    )
    original_delivery = models.ForeignKey(
        "inventory.DeliveryNote",
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

    submitted_at = models.DateTimeField(null=True, blank=True)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    approval_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = "sales_return"
        ordering = ["-document_date", "-id"]
        constraints = [
            # RET-001: a return must reference an eligible source document.
            models.CheckConstraint(
                condition=Q(original_invoice__isnull=False)
                | Q(original_delivery__isnull=False),
                name="sales_return_has_source",
            ),
            models.CheckConstraint(
                condition=~Q(reason=""), name="sales_return_reason_required"
            ),
        ]
        indexes = [
            models.Index(fields=["customer", "-document_date"], name="ix_sr_customer_date")
        ]

    def __str__(self):
        return self.number

    def get_absolute_url(self):
        from django.urls import reverse

        return reverse("sales:return_detail", args=[self.pk])


class SalesReturnLine(models.Model):
    sales_return = models.ForeignKey(
        SalesReturn, on_delete=models.CASCADE, related_name="lines"
    )
    line_no = models.PositiveSmallIntegerField()
    invoice_line = models.ForeignKey(
        SalesInvoiceLine,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="return_lines",
    )
    delivery_line = models.ForeignKey(
        "inventory.DeliveryNoteLine",
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
        db_table = "sales_return_line"
        ordering = ["sales_return", "line_no"]
        constraints = [
            models.UniqueConstraint(
                fields=["sales_return", "line_no"], name="sr_line_unique_no"
            ),
            models.CheckConstraint(condition=Q(quantity__gt=0), name="sr_line_qty_positive"),
        ]

    def __str__(self):
        return f"{self.sales_return_id}/{self.line_no} {self.quantity}"


# ---------------------------------------------------------------------------
# Sales credit note (RET-003, RET-004, SAL-007)
# ---------------------------------------------------------------------------
class SalesCreditNote(FinancialDocumentBase):
    """
    Reduces AR and reverses revenue/tax proportionally from the original
    snapshots (RET-003). Its unapplied part becomes customer credit (RET-004),
    which `payments.PaymentAllocation` and `payments.Refund` can consume.
    """

    customer = models.ForeignKey(
        "parties.Customer", on_delete=models.PROTECT, related_name="credit_notes"
    )
    original_invoice = models.ForeignKey(
        SalesInvoice,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="credit_notes",
    )
    sales_return = models.ForeignKey(
        SalesReturn,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="credit_notes",
    )
    reason = models.TextField(help_text="Mandatory (RET-008).")
    customer_name_snapshot = models.CharField(max_length=200, blank=True)
    billing_address_text = models.TextField(blank=True)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )

    # Unapplied credit still available to allocate or refund (RET-009, BR-016).
    refunded_txn = models.DecimalField(**MONEY, default=ZERO)
    is_reversed = models.BooleanField(default=False)

    class Meta:
        db_table = "sales_credit_note"
        ordering = ["-document_date", "-id"]
        constraints = [
            models.CheckConstraint(condition=Q(exchange_rate__gt=0), name="cn_rate_positive"),
            models.CheckConstraint(condition=Q(total_txn__gte=0), name="cn_total_nonneg"),
            # BR-016 / RET-009: applied + refunded can never exceed the credit.
            models.CheckConstraint(
                condition=Q(allocated_txn__gte=0)
                & Q(refunded_txn__gte=0)
                & Q(allocated_txn__lte=F("total_txn"))
                & Q(refunded_txn__lte=F("total_txn")),
                name="cn_settlement_within_total",
            ),
            models.CheckConstraint(
                condition=Q(open_txn=F("total_txn") - F("allocated_txn") - F("refunded_txn")),
                name="cn_open_is_derived",
            ),
            models.CheckConstraint(condition=Q(open_txn__gte=0), name="cn_open_nonneg"),
            models.CheckConstraint(
                condition=(
                    ~Q(status__in=["POSTED", "PARTIAL", "COMPLETED", "REVERSED"])
                    | Q(journal_entry__isnull=False)
                ),
                name="cn_posted_has_journal",
            ),
        ]
        indexes = [
            models.Index(fields=["customer", "-document_date"], name="ix_cn_customer_date"),
            models.Index(
                fields=["customer"], condition=Q(open_txn__gt=0), name="ix_cn_open_credit"
            ),
        ]

    def get_absolute_url(self):
        from django.urls import reverse

        return reverse("sales:credit_note_detail", args=[self.pk])


class SalesCreditNoteLine(DocumentLineBase):
    credit_note = models.ForeignKey(
        SalesCreditNote, on_delete=models.CASCADE, related_name="lines"
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
    invoice_line = models.ForeignKey(
        SalesInvoiceLine,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="credit_lines",
    )
    return_line = models.ForeignKey(
        SalesReturnLine, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    revenue_account = models.ForeignKey(
        "ledger.Account", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        db_table = "sales_credit_note_line"
        ordering = ["credit_note", "line_no"]
        constraints = [
            models.UniqueConstraint(
                fields=["credit_note", "line_no"], name="cn_line_unique_no"
            ),
            models.CheckConstraint(condition=Q(quantity__gt=0), name="cn_line_qty_positive"),
            models.CheckConstraint(
                condition=Q(tax_rate_percent__gte=0) & Q(tax_rate_percent__lte=100),
                name="cn_line_tax_rate_range",
            ),
        ]
        indexes = [models.Index(fields=["product"], name="ix_cn_line_product")]
