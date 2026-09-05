from django.contrib import admin

from apps.sales.models import SalesInvoice, SalesOrder, SalesOrderLine


class SalesOrderLineInline(admin.TabularInline):
    model = SalesOrderLine
    extra = 0


@admin.register(SalesOrder)
class SalesOrderAdmin(admin.ModelAdmin):
    list_display = ("number", "customer", "document_date", "status", "total_txn")
    list_filter = ("status",)
    search_fields = ("number", "customer__name", "customer_reference")
    inlines = [SalesOrderLineInline]
    autocomplete_fields = ("customer",)


@admin.register(SalesInvoice)
class SalesInvoiceAdmin(admin.ModelAdmin):
    list_display = ("number", "customer", "document_date", "status", "total_txn", "open_txn")
    list_filter = ("status",)
    search_fields = ("number", "customer__name")
