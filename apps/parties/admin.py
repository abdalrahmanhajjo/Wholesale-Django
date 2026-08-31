"""Customers and vendors in the admin (PTY-001, PTY-002)."""

from django.contrib import admin

from apps.parties.models import Address, Contact, Customer, Vendor


class AddressInline(admin.TabularInline):
    model = Address
    extra = 0
    fields = ("label", "address_type", "line1", "city", "country", "is_default")


class ContactInline(admin.TabularInline):
    model = Contact
    extra = 0
    fields = ("name", "job_title", "email", "phone", "is_primary")


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = (
        "code",
        "name",
        "currency",
        "payment_term",
        "credit_limit",
        "credit_hold",
        "is_active",
    )
    list_filter = ("is_active", "credit_hold", "currency")
    search_fields = ("code", "name", "legal_name", "tax_id", "email")
    inlines = [AddressInline, ContactInline]
    # PTY-008: deactivate, never delete, once posted history exists.
    actions = ["deactivate"]

    @admin.action(description="Deactivate selected customers (PTY-008)")
    def deactivate(self, request, queryset):
        from django.utils import timezone

        updated = queryset.update(is_active=False, deactivated_at=timezone.now())
        self.message_user(request, f"{updated} customer(s) deactivated.")


@admin.register(Vendor)
class VendorAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "currency", "payment_term", "is_active")
    list_filter = ("is_active", "currency")
    search_fields = ("code", "name", "legal_name", "tax_id", "email")
    inlines = [AddressInline, ContactInline]
