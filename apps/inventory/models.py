"""
Warehouses, stock movements, valuation, and the two physical documents that
move goods: goods receipt (inbound) and delivery note (outbound), plus
transfers and adjustments.

BRD coverage: CFG-002, INV-003..INV-011, BR-017, BR-018, BR-019, SAL-005,
SAL-010, PUR-003, PUR-004, RPT-016, RPT-017, RPT-018.
"""

from decimal import Decimal

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.db.models import F, Q

from apps.core.models import COST, MONEY, QTY, DocumentStatus, TimeStampedModel

ZERO = Decimal("0")


class Warehouse(TimeStampedModel):
    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=100)
    address = models.TextField(blank=True)
    manager = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    # Optional per-warehouse inventory account (CFG-007).
    inventory_account = models.ForeignKey(
        "ledger.Account", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    allow_negative_stock = models.BooleanField(
        default=False,
        help_text=(
            "Let stock go below zero at this warehouse, overriding the "
            "company-wide setting. Use only where stock is counted after the fact."
        ),
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "warehouse"
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} {self.name}"


# ---------------------------------------------------------------------------
# Perpetual valuation (INV-003, INV-005, BR-018)
# ---------------------------------------------------------------------------
class StockBalance(models.Model):
    """
    Running quantity and weighted-average cost per product/warehouse.

    This row is the lock target for stock posting: the posting service takes
    SELECT ... FOR UPDATE on it before writing any movement, which serialises
    concurrent deliveries of the same item (NFR-003, NFR-008, UAT-12).

    It is a cache of the movement ledger, not the source of truth — INV-003
    requires it to equal the sum of posted StockMovement rows for any cutoff.
    """

    product = models.ForeignKey(
        "catalog.Product", on_delete=models.PROTECT, related_name="balances"
    )
    warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT, related_name="balances")
    quantity_on_hand = models.DecimalField(**QTY, default=ZERO)
    average_cost = models.DecimalField(**COST, default=ZERO)
    total_value = models.DecimalField(**MONEY, default=ZERO)
    quantity_reserved = models.DecimalField(**QTY, default=ZERO)
    last_movement_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "stock_balance"
        constraints = [
            models.UniqueConstraint(
                fields=["product", "warehouse"], name="stock_balance_unique"
            ),
            models.CheckConstraint(
                condition=Q(average_cost__gte=0), name="stock_balance_avg_cost_nonneg"
            ),
            models.CheckConstraint(
                condition=Q(quantity_reserved__gte=0), name="stock_balance_reserved_nonneg"
            ),
        ]
        indexes = [
            models.Index(fields=["warehouse", "product"], name="ix_stock_balance_wh"),
        ]

    def __str__(self):
        return f"{self.product_id}@{self.warehouse_id}: {self.quantity_on_hand}"


class MovementType(models.TextChoices):
    GOODS_RECEIPT = "GOODS_RECEIPT", "Goods receipt"
    DELIVERY = "DELIVERY", "Delivery"
    SALES_RETURN_IN = "SALES_RETURN_IN", "Sales return (restock)"
    PURCHASE_RETURN_OUT = "PURCHASE_RETURN_OUT", "Purchase return to vendor"
    TRANSFER_OUT = "TRANSFER_OUT", "Transfer out"
    TRANSFER_IN = "TRANSFER_IN", "Transfer in"
    ADJUSTMENT_IN = "ADJUSTMENT_IN", "Adjustment increase"
    ADJUSTMENT_OUT = "ADJUSTMENT_OUT", "Adjustment decrease"
    WRITE_OFF = "WRITE_OFF", "Write-off / damaged"
    OPENING = "OPENING", "Opening stock"


#: Movement types that increase quantity on hand.
INBOUND_TYPES = [
    "GOODS_RECEIPT",
    "SALES_RETURN_IN",
    "TRANSFER_IN",
    "ADJUSTMENT_IN",
    "OPENING",
]


class StockMovement(models.Model):
    """
    Immutable stock ledger line (INV-004, RPT-017). One row per product per
    warehouse per event. `direction` is +1 inbound / -1 outbound so quantity is
    always stored positive and running totals are a single SUM.

    BR-019: every movement links to a source document or an approved adjustment.
    """

    movement_date = models.DateField(db_index=True)
    movement_type = models.CharField(max_length=20, choices=MovementType.choices)
    direction = models.SmallIntegerField(help_text="+1 inbound, -1 outbound")
    product = models.ForeignKey(
        "catalog.Product", on_delete=models.PROTECT, related_name="movements"
    )
    warehouse = models.ForeignKey(
        Warehouse, on_delete=models.PROTECT, related_name="movements"
    )
    quantity = models.DecimalField(**QTY)
    unit_cost = models.DecimalField(
        **COST, help_text="Weighted-average cost at movement time."
    )
    total_cost = models.DecimalField(**MONEY)

    # Running values captured at posting so the stock card needs no recomputation.
    balance_quantity_after = models.DecimalField(**QTY, default=ZERO)
    balance_value_after = models.DecimalField(**MONEY, default=ZERO)
    average_cost_after = models.DecimalField(**COST, default=ZERO)

    # BR-019 / SC-03 source link.
    source_content_type = models.ForeignKey(
        ContentType, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    source_object_id = models.BigIntegerField(null=True, blank=True)
    source = GenericForeignKey("source_content_type", "source_object_id")
    source_doc_type = models.CharField(max_length=4, blank=True)
    source_doc_number = models.CharField(max_length=32, blank=True)

    journal_entry = models.ForeignKey(
        "ledger.JournalEntry",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="stock_movements",
    )
    # GL-002 idempotency for the stock side of a post.
    idempotency_key = models.CharField(max_length=120, unique=True)

    reverses = models.OneToOneField(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="reversed_by"
    )
    notes = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )

    class Meta:
        db_table = "stock_movement"
        ordering = ["movement_date", "id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(direction__in=[-1, 1]), name="stock_movement_direction_valid"
            ),
            models.CheckConstraint(
                condition=Q(quantity__gt=0), name="stock_movement_quantity_positive"
            ),
            models.CheckConstraint(
                condition=Q(unit_cost__gte=0) & Q(total_cost__gte=0),
                name="stock_movement_cost_nonneg",
            ),
            # Direction must agree with the movement type.
            models.CheckConstraint(
                condition=(
                    (
                        Q(
                            movement_type__in=[
                                "GOODS_RECEIPT",
                                "SALES_RETURN_IN",
                                "TRANSFER_IN",
                                "ADJUSTMENT_IN",
                                "OPENING",
                            ]
                        )
                        & Q(direction=1)
                    )
                    | (
                        Q(
                            movement_type__in=[
                                "DELIVERY",
                                "PURCHASE_RETURN_OUT",
                                "TRANSFER_OUT",
                                "ADJUSTMENT_OUT",
                                "WRITE_OFF",
                            ]
                        )
                        & Q(direction=-1)
                    )
                ),
                name="stock_movement_direction_matches_type",
            ),
            # BR-019: a movement without a document must be an adjustment/opening.
            models.CheckConstraint(
                condition=(
                    Q(source_content_type__isnull=False)
                    | Q(movement_type__in=["ADJUSTMENT_IN", "ADJUSTMENT_OUT", "OPENING"])
                ),
                name="stock_movement_has_source",
            ),
        ]
        indexes = [
            models.Index(
                fields=["product", "warehouse", "movement_date"], name="ix_movement_card"
            ),
            models.Index(
                fields=["source_content_type", "source_object_id"], name="ix_movement_source"
            ),
            models.Index(
                fields=["movement_type", "movement_date"], name="ix_movement_type_date"
            ),
            models.Index(fields=["journal_entry"], name="ix_movement_journal"),
        ]

    def __str__(self):
        return f"{self.movement_type} {self.product_id} {self.direction * self.quantity}"


# ---------------------------------------------------------------------------
# Shared behaviour for stock documents
# ---------------------------------------------------------------------------
class StockDocumentBase(TimeStampedModel):
    number = models.CharField(max_length=32, unique=True)
    document_date = models.DateField(db_index=True)
    warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT, related_name="+")
    status = models.CharField(
        max_length=10, choices=DocumentStatus.choices, default=DocumentStatus.DRAFT
    )
    reference = models.CharField(max_length=64, blank=True)
    notes = models.TextField(blank=True)

    posted_at = models.DateTimeField(null=True, blank=True)
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    journal_entry = models.ForeignKey(
        "ledger.JournalEntry",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    total_cost_base = models.DecimalField(**MONEY, default=ZERO)

    class Meta:
        abstract = True

    def __str__(self):
        return self.number


# ---------------------------------------------------------------------------
# Goods receipt (PUR-003, PUR-004, INV-006)
# ---------------------------------------------------------------------------
class GoodsReceipt(StockDocumentBase):
    vendor = models.ForeignKey(
        "parties.Vendor", on_delete=models.PROTECT, related_name="goods_receipts"
    )
    purchase_order = models.ForeignKey(
        "purchases.PurchaseOrder",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="receipts",
        help_text=(
            "The purchase order this receipt fulfils. Leave empty only for an "
            "authorised direct receipt with no order behind it."
        ),
    )
    vendor_delivery_note = models.CharField(max_length=64, blank=True)
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )

    class Meta:
        db_table = "goods_receipt"
        ordering = ["-document_date", "-id"]
        constraints = [
            # A posted receipt must carry its journal (GL-001, SC-03).
            models.CheckConstraint(
                condition=(
                    ~Q(status__in=["POSTED", "PARTIAL", "COMPLETED"])
                    | Q(journal_entry__isnull=False)
                    | Q(total_cost_base=0)
                ),
                name="goods_receipt_posted_has_journal",
            )
        ]
        indexes = [
            models.Index(fields=["vendor", "-document_date"], name="ix_gr_vendor_date"),
            models.Index(fields=["status", "-document_date"], name="ix_gr_status_date"),
        ]

    def get_absolute_url(self):
        from django.urls import reverse

        return reverse("inventory:gr_detail", args=[self.pk])


class GoodsReceiptLine(models.Model):
    receipt = models.ForeignKey(GoodsReceipt, on_delete=models.CASCADE, related_name="lines")
    line_no = models.PositiveSmallIntegerField()
    purchase_order_line = models.ForeignKey(
        "purchases.PurchaseOrderLine",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="receipt_lines",
    )
    product = models.ForeignKey("catalog.Product", on_delete=models.PROTECT, related_name="+")
    description = models.CharField(max_length=255, blank=True)
    unit = models.ForeignKey(
        "catalog.UnitOfMeasure", on_delete=models.PROTECT, related_name="+"
    )

    quantity_received = models.DecimalField(**QTY)
    quantity_accepted = models.DecimalField(**QTY)  # PUR-004
    quantity_rejected = models.DecimalField(**QTY, default=ZERO)
    rejection_reason = models.CharField(max_length=255, blank=True)

    unit_cost = models.DecimalField(**COST, default=ZERO)
    total_cost = models.DecimalField(**MONEY, default=ZERO)
    quantity_billed = models.DecimalField(**QTY, default=ZERO)  # PUR-005: no double billing
    quantity_returned = models.DecimalField(**QTY, default=ZERO)  # RET-005 eligibility

    class Meta:
        db_table = "goods_receipt_line"
        ordering = ["receipt", "line_no"]
        constraints = [
            models.UniqueConstraint(fields=["receipt", "line_no"], name="gr_line_unique_no"),
            models.CheckConstraint(
                condition=Q(quantity_received__gt=0), name="gr_line_qty_positive"
            ),
            models.CheckConstraint(
                condition=Q(quantity_accepted__gte=0) & Q(quantity_rejected__gte=0),
                name="gr_line_split_nonneg",
            ),
            models.CheckConstraint(
                condition=Q(quantity_accepted=F("quantity_received") - F("quantity_rejected")),
                name="gr_line_split_sums",
            ),
            # PUR-005 / RET-005: cannot bill or return more than accepted.
            models.CheckConstraint(
                condition=Q(quantity_billed__lte=F("quantity_accepted")),
                name="gr_line_billed_within_accepted",
            ),
            models.CheckConstraint(
                condition=Q(quantity_returned__lte=F("quantity_accepted")),
                name="gr_line_returned_within_accepted",
            ),
        ]
        indexes = [models.Index(fields=["product"], name="ix_gr_line_product")]

    def __str__(self):
        return f"{self.receipt_id}/{self.line_no} {self.quantity_received}"


# ---------------------------------------------------------------------------
# Delivery note (SAL-005, INV-007, SAL-010)
# ---------------------------------------------------------------------------
class DeliveryNote(StockDocumentBase):
    customer = models.ForeignKey(
        "parties.Customer", on_delete=models.PROTECT, related_name="deliveries"
    )
    sales_order = models.ForeignKey(
        "sales.SalesOrder",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="deliveries",
        help_text=(
            "The sales order this delivery fulfils. Leave empty only for an "
            "authorised direct delivery with no order behind it."
        ),
    )
    shipping_address_text = models.TextField(blank=True)  # PTY-003 snapshot
    carrier = models.CharField(max_length=100, blank=True)
    tracking_reference = models.CharField(max_length=64, blank=True)
    delivered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )

    def get_absolute_url(self):
        from django.urls import reverse

        return reverse("sales:delivery_detail", args=[self.pk])

    class Meta:
        db_table = "delivery_note"
        ordering = ["-document_date", "-id"]
        constraints = [
            # SAL-010: a posted delivery has a COGS journal *unless* every line was
            # non-stock, in which case there is legitimately nothing to post.
            models.CheckConstraint(
                condition=(
                    ~Q(status__in=["POSTED", "PARTIAL", "COMPLETED"])
                    | Q(journal_entry__isnull=False)
                    | Q(total_cost_base=0)
                ),
                name="delivery_note_posted_has_journal",
            )
        ]
        indexes = [
            models.Index(fields=["customer", "-document_date"], name="ix_dn_customer_date"),
            models.Index(fields=["status", "-document_date"], name="ix_dn_status_date"),
        ]


class DeliveryNoteLine(models.Model):
    delivery = models.ForeignKey(DeliveryNote, on_delete=models.CASCADE, related_name="lines")
    line_no = models.PositiveSmallIntegerField()
    sales_order_line = models.ForeignKey(
        "sales.SalesOrderLine",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="delivery_lines",
    )
    product = models.ForeignKey("catalog.Product", on_delete=models.PROTECT, related_name="+")
    description = models.CharField(max_length=255, blank=True)
    unit = models.ForeignKey(
        "catalog.UnitOfMeasure", on_delete=models.PROTECT, related_name="+"
    )

    quantity = models.DecimalField(**QTY)
    unit_cost = models.DecimalField(**COST, default=ZERO, help_text="Average cost at posting.")
    total_cost = models.DecimalField(**MONEY, default=ZERO)
    quantity_invoiced = models.DecimalField(**QTY, default=ZERO)  # SAL-006
    quantity_returned = models.DecimalField(**QTY, default=ZERO)  # RET-001 eligibility

    class Meta:
        db_table = "delivery_note_line"
        ordering = ["delivery", "line_no"]
        constraints = [
            models.UniqueConstraint(fields=["delivery", "line_no"], name="dn_line_unique_no"),
            models.CheckConstraint(condition=Q(quantity__gt=0), name="dn_line_qty_positive"),
            models.CheckConstraint(
                condition=Q(quantity_invoiced__lte=F("quantity")),
                name="dn_line_invoiced_within_delivered",
            ),
            # BR-015: returns cannot exceed the delivered quantity.
            models.CheckConstraint(
                condition=Q(quantity_returned__lte=F("quantity")),
                name="dn_line_returned_within_delivered",
            ),
        ]
        indexes = [models.Index(fields=["product"], name="ix_dn_line_product")]

    def __str__(self):
        return f"{self.delivery_id}/{self.line_no} {self.quantity}"


# ---------------------------------------------------------------------------
# Transfers (INV-008)
# ---------------------------------------------------------------------------
class StockTransfer(TimeStampedModel):
    number = models.CharField(max_length=32, unique=True)
    document_date = models.DateField(db_index=True)
    from_warehouse = models.ForeignKey(
        Warehouse, on_delete=models.PROTECT, related_name="transfers_out"
    )
    to_warehouse = models.ForeignKey(
        Warehouse, on_delete=models.PROTECT, related_name="transfers_in"
    )
    status = models.CharField(
        max_length=10, choices=DocumentStatus.choices, default=DocumentStatus.DRAFT
    )
    reason = models.CharField(max_length=255, blank=True)
    notes = models.TextField(blank=True)
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
        db_table = "stock_transfer"
        ordering = ["-document_date", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=~Q(from_warehouse=F("to_warehouse")),
                name="stock_transfer_different_warehouses",
            )
        ]

    def __str__(self):
        return self.number


class StockTransferLine(models.Model):
    transfer = models.ForeignKey(StockTransfer, on_delete=models.CASCADE, related_name="lines")
    line_no = models.PositiveSmallIntegerField()
    product = models.ForeignKey("catalog.Product", on_delete=models.PROTECT, related_name="+")
    quantity = models.DecimalField(**QTY)
    unit_cost = models.DecimalField(**COST, default=ZERO)
    total_cost = models.DecimalField(**MONEY, default=ZERO)

    class Meta:
        db_table = "stock_transfer_line"
        ordering = ["transfer", "line_no"]
        constraints = [
            models.UniqueConstraint(fields=["transfer", "line_no"], name="st_line_unique_no"),
            models.CheckConstraint(condition=Q(quantity__gt=0), name="st_line_qty_positive"),
        ]

    def __str__(self):
        return f"{self.transfer_id}/{self.line_no} {self.quantity}"


# ---------------------------------------------------------------------------
# Adjustments (INV-009)
# ---------------------------------------------------------------------------
class AdjustmentReason(models.Model):
    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=100)
    increases_stock = models.BooleanField(default=True)
    gain_loss_account = models.ForeignKey(
        "ledger.Account", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    requires_approval = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "adjustment_reason"
        ordering = ["code"]

    def __str__(self):
        return self.name


class StockAdjustment(TimeStampedModel):
    number = models.CharField(max_length=32, unique=True)
    document_date = models.DateField(db_index=True)
    warehouse = models.ForeignKey(
        Warehouse, on_delete=models.PROTECT, related_name="adjustments"
    )
    reason = models.ForeignKey(AdjustmentReason, on_delete=models.PROTECT, related_name="+")
    status = models.CharField(
        max_length=10, choices=DocumentStatus.choices, default=DocumentStatus.DRAFT
    )
    narration = models.TextField(blank=True)
    attachment_reference = models.CharField(max_length=255, blank=True)
    total_value_base = models.DecimalField(**MONEY, default=ZERO)

    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
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
        db_table = "stock_adjustment"
        ordering = ["-document_date", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    ~Q(status__in=["POSTED", "COMPLETED"]) | Q(journal_entry__isnull=False)
                ),
                name="stock_adjustment_posted_has_journal",
            )
        ]

    def __str__(self):
        return self.number


class StockAdjustmentLine(models.Model):
    adjustment = models.ForeignKey(
        StockAdjustment, on_delete=models.CASCADE, related_name="lines"
    )
    line_no = models.PositiveSmallIntegerField()
    product = models.ForeignKey("catalog.Product", on_delete=models.PROTECT, related_name="+")
    quantity_delta = models.DecimalField(
        **QTY, help_text="Signed: positive increases stock, negative decreases it."
    )
    unit_cost = models.DecimalField(**COST, default=ZERO)
    value_delta = models.DecimalField(**MONEY, default=ZERO)
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = "stock_adjustment_line"
        ordering = ["adjustment", "line_no"]
        constraints = [
            models.UniqueConstraint(
                fields=["adjustment", "line_no"], name="sa_line_unique_no"
            ),
            models.CheckConstraint(condition=~Q(quantity_delta=0), name="sa_line_qty_nonzero"),
        ]

    def __str__(self):
        return f"{self.adjustment_id}/{self.line_no} {self.quantity_delta}"
