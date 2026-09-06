from django.contrib import admin

from apps.payments.models import (
    Allocation,
    MoneyAccount,
    Payment,
    PaymentMethod,
    Refund,
    StripeCheckout,
)


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
        "fee_base",
        "allocated_txn",
        "unallocated_txn",
        "created_at",
        "updated_at",
    )


@admin.register(StripeCheckout)
class StripeCheckoutAdmin(admin.ModelAdmin):
    list_display = ("session_id", "invoice", "amount_txn", "fee_txn", "status", "payment")
    list_filter = ("status", "currency")
    search_fields = ("session_id", "payment_intent_id", "charge_id", "invoice__number")
    # Everything here is written by Stripe or by the settlement service. Editing
    # it by hand would decouple the ledger from what actually happened.
    readonly_fields = (
        "invoice",
        "session_id",
        "payment_intent_id",
        "charge_id",
        "url",
        "currency",
        "amount_txn",
        "fee_txn",
        "payment",
        "paid_at",
        "expires_at",
        "created_at",
        "updated_at",
    )


admin.site.register(Allocation)
admin.site.register(Refund)
