"""Inventory in the admin — internal visibility only (BRD §12)."""

from django.contrib import admin

from apps.inventory.models import (
    AdjustmentReason,
    GoodsReceipt,
    GoodsReceiptLine,
    StockAdjustment,
    StockAdjustmentLine,
    StockBalance,
    StockMovement,
    StockTransfer,
    StockTransferLine,
    Warehouse,
)


class GoodsReceiptLineInline(admin.TabularInline):
    model = GoodsReceiptLine
    extra = 0
    fields = (
        "line_no",
        "product",
        "quantity_received",
        "quantity_accepted",
        "quantity_rejected",
        "unit_cost",
        "total_cost",
    )
    readonly_fields = ("quantity_accepted", "unit_cost", "total_cost")


@admin.register(GoodsReceipt)
class GoodsReceiptAdmin(admin.ModelAdmin):
    list_display = (
        "number",
        "vendor",
        "warehouse",
        "document_date",
        "status",
        "total_cost_base",
    )
    list_filter = ("status", "warehouse")
    search_fields = ("number", "vendor__name", "vendor_delivery_note")
    date_hierarchy = "document_date"
    inlines = [GoodsReceiptLineInline]
    # INV-006: the post action, not a dropdown, moves a receipt to POSTED.
    readonly_fields = ("status", "posted_at", "posted_by", "journal_entry")


@admin.register(Warehouse)
class WarehouseAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "manager", "is_active")
    search_fields = ("code", "name")


@admin.register(StockBalance)
class StockBalanceAdmin(admin.ModelAdmin):
    list_display = ("product", "warehouse", "quantity_on_hand", "average_cost", "total_value")
    list_filter = ("warehouse",)
    search_fields = ("product__sku", "product__name")


@admin.register(StockMovement)
class StockMovementAdmin(admin.ModelAdmin):
    list_display = (
        "movement_date",
        "movement_type",
        "product",
        "warehouse",
        "direction",
        "quantity",
        "unit_cost",
    )
    list_filter = ("movement_type", "warehouse")
    search_fields = ("product__sku", "source_doc_number")
    date_hierarchy = "movement_date"
    # INV-004: the ledger is immutable once written.
    readonly_fields = [f.name for f in StockMovement._meta.fields]

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class StockTransferLineInline(admin.TabularInline):
    model = StockTransferLine
    extra = 0
    fields = ("line_no", "product", "quantity", "unit_cost", "total_cost")
    readonly_fields = ("unit_cost", "total_cost")


@admin.register(StockTransfer)
class StockTransferAdmin(admin.ModelAdmin):
    list_display = (
        "number",
        "from_warehouse",
        "to_warehouse",
        "document_date",
        "status",
        "total_cost_base",
    )
    list_filter = ("status", "from_warehouse", "to_warehouse")
    search_fields = ("number", "reason")
    date_hierarchy = "document_date"
    inlines = [StockTransferLineInline]
    readonly_fields = ("status", "posted_at", "posted_by", "journal_entry")


class StockAdjustmentLineInline(admin.TabularInline):
    model = StockAdjustmentLine
    extra = 0
    fields = ("line_no", "product", "quantity_delta", "unit_cost", "value_delta", "note")
    readonly_fields = ("unit_cost", "value_delta")


@admin.register(StockAdjustment)
class StockAdjustmentAdmin(admin.ModelAdmin):
    list_display = (
        "number",
        "warehouse",
        "reason",
        "document_date",
        "status",
        "total_value_base",
    )
    list_filter = ("status", "warehouse", "reason")
    search_fields = ("number", "narration")
    date_hierarchy = "document_date"
    inlines = [StockAdjustmentLineInline]
    readonly_fields = (
        "status",
        "approved_at",
        "approved_by",
        "posted_at",
        "posted_by",
        "journal_entry",
    )


@admin.register(AdjustmentReason)
class AdjustmentReasonAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "increases_stock", "requires_approval", "is_active")
    search_fields = ("code", "name")
