from django.contrib import admin

from apps.payments.models import Allocation, MoneyAccount, Payment, PaymentMethod, Refund


@admin.register(MoneyAccount)
class MoneyAccountAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "account_type", "currency", "is_active")
    list_filter = ("account_type", "currency", "is_active")
    search_fields = ("code", "name", "bank_name", "account_number", "iban")


@admin.register(PaymentMethod)
class PaymentMethodAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "requires_reference", "default_money_account", "is_active")
    list_filter = ("requires_reference", "is_active")
    search_fields = ("code", "name")


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = (
        "number",
        "direction",
        "payment_date",
        "party",
        "amount_txn",
        "currency",
        "method",
        "status",
    )
    list_filter = ("direction", "status", "method", "money_account", "currency")
    search_fields = ("number", "reference", "customer__name", "vendor__name")
    readonly_fields = (
        "amount_base",
        "allocated_txn",
        "unallocated_txn",
        "created_at",
        "updated_at",
    )


admin.site.register(Allocation)
admin.site.register(Refund)
