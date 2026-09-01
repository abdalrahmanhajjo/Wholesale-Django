"""Purchasing in the admin — internal visibility only (BRD §12)."""

from django.contrib import admin

from apps.purchases.models import (
    PurchaseBill,
    PurchaseBillLine,
    PurchaseOrder,
    PurchaseOrderLine,
)


class PurchaseOrderLineInline(admin.TabularInline):
    model = PurchaseOrderLine
    extra = 0
    fields = ("line_no", "product", "quantity", "unit_price", "tax_code", "total_txn")
    readonly_fields = ("total_txn",)


@admin.register(PurchaseOrder)
class PurchaseOrderAdmin(admin.ModelAdmin):
    list_display = ("number", "vendor", "document_date", "status", "total_txn")
    list_filter = ("status", "vendor")
    search_fields = ("number", "vendor__name", "vendor_reference")
    date_hierarchy = "document_date"
    inlines = [PurchaseOrderLineInline]
    # PUR-002: the workflow, not a dropdown, moves an order through approval.
    readonly_fields = (
        "status",
        "submitted_at",
        "approved_at",
        "approved_by",
        "approval_reason",
    )


class PurchaseBillLineInline(admin.TabularInline):
    model = PurchaseBillLine
    extra = 0
    fields = (
        "line_no",
        "is_stock_line",
        "product",
        "expense_account",
        "quantity",
        "unit_price",
        "tax_code",
        "total_txn",
    )
    readonly_fields = ("total_txn",)


@admin.register(PurchaseBill)
class PurchaseBillAdmin(admin.ModelAdmin):
    list_display = (
        "number",
        "vendor",
        "vendor_invoice_number",
        "document_date",
        "status",
        "total_txn",
        "open_txn",
    )
    list_filter = ("status", "vendor")
    search_fields = ("number", "vendor__name", "vendor_invoice_number")
    date_hierarchy = "document_date"
    inlines = [PurchaseBillLineInline]
    # PUR-008: the post action, not a dropdown, moves a bill to POSTED.
    readonly_fields = (
        "status",
        "posted_at",
        "posted_by",
        "journal_entry",
        "open_txn",
        "open_base",
    )
