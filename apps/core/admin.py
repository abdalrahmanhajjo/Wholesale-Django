"""
Django admin for the configuration tables.

BRD §12: "Django Admin may support internal configuration, but the primary
workflows require purpose-built templates." So the admin exists to let the team
create and inspect reference data from day one — it is not the settings UI the
BRD asks for, and it is not what an accountant will use.

Everything posted is registered read-only where it exists at all: the audit log
is append-only (ACC-005) and fiscal periods must be closed through the
permission-checked workflow, not by editing a dropdown.
"""

from django.contrib import admin

from apps.core.models import (
    AuditEvent,
    Company,
    Currency,
    DocumentSequence,
    ExchangeRate,
    FiscalPeriod,
    FiscalYear,
    PaymentTerm,
    TaxCode,
)


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = ("name", "base_currency", "timezone", "allow_negative_stock")
    fieldsets = (
        ("Identity", {"fields": ("name", "legal_name", "tax_id", "registration_no", "logo")}),
        (
            "Contact",
            {
                "fields": (
                    "email",
                    "phone",
                    "address_line1",
                    "address_line2",
                    "city",
                    "state",
                    "postal_code",
                    "country",
                )
            },
        ),
        (
            "Financial",
            {"fields": ("base_currency", "fiscal_year_start_month", "timezone", "language")},
        ),
        (
            "Policy (CFG-010)",
            {
                "fields": (
                    "allow_negative_stock",
                    "rounding_tolerance",
                    "price_decimal_places",
                    "qty_decimal_places",
                    "require_po_approval",
                    "require_so_approval",
                    "block_duplicate_vendor_invoice",
                    "warn_duplicate_customer_ref",
                )
            },
        ),
    )

    def has_add_permission(self, request):
        # Singleton (BRD 3.1): one legal entity in the MVP.
        return not Company.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Currency)
class CurrencyAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "symbol", "decimal_places", "is_base", "is_active")
    list_filter = ("is_active", "is_base")
    search_fields = ("code", "name")


@admin.register(ExchangeRate)
class ExchangeRateAdmin(admin.ModelAdmin):
    list_display = ("currency", "rate_date", "rate", "source")
    list_filter = ("currency",)
    date_hierarchy = "rate_date"
    search_fields = ("currency__code",)


@admin.register(TaxCode)
class TaxCodeAdmin(admin.ModelAdmin):
    list_display = (
        "code",
        "name",
        "rate_percent",
        "treatment",
        "applies_to",
        "is_inclusive",
        "is_recoverable",
        "is_active",
    )
    list_filter = ("treatment", "applies_to", "is_active", "is_inclusive")
    search_fields = ("code", "name")


@admin.register(PaymentTerm)
class PaymentTermAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "net_days", "end_of_month", "is_active")
    list_filter = ("is_active",)
    search_fields = ("code", "name")


class FiscalPeriodInline(admin.TabularInline):
    model = FiscalPeriod
    extra = 0
    fields = ("period_no", "name", "start_date", "end_date", "status")
    readonly_fields = ("status",)
    show_change_link = True


@admin.register(FiscalYear)
class FiscalYearAdmin(admin.ModelAdmin):
    list_display = ("code", "start_date", "end_date", "status")
    inlines = [FiscalPeriodInline]


@admin.register(FiscalPeriod)
class FiscalPeriodAdmin(admin.ModelAdmin):
    list_display = ("name", "fiscal_year", "start_date", "end_date", "status", "closed_by")
    list_filter = ("status", "fiscal_year")
    # CFG-009 / ACC-008: closing and reopening happen through the permission-checked
    # workflow with a reason and an audit event, never by editing a dropdown here.
    readonly_fields = (
        "status",
        "closed_at",
        "closed_by",
        "close_reason",
        "reopened_at",
        "reopened_by",
        "reopen_reason",
    )


@admin.register(DocumentSequence)
class DocumentSequenceAdmin(admin.ModelAdmin):
    list_display = (
        "document_type",
        "series",
        "prefix",
        "next_number",
        "padding",
        "reset_policy",
        "is_active",
    )
    list_filter = ("document_type", "is_active")


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    """Append-only (ACC-005, NFR-017). Readable here, never writable."""

    list_display = ("occurred_at", "user", "action", "object_repr", "correlation_id")
    list_filter = ("action", "occurred_at")
    search_fields = ("object_repr", "reason")
    date_hierarchy = "occurred_at"
    readonly_fields = [f.name for f in AuditEvent._meta.fields] + ["target"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
