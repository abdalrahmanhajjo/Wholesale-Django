"""
Chart of accounts and journals in the admin.

The ledger app belongs to Member 4, but the chart of accounts and account
mappings are Member 1's configuration responsibility (plan Day 3, CFG-006 /
CFG-007), so they are registered here. Journals are read-only everywhere:
BR-004 makes a posted journal immutable, and the database enforces it.
"""

from django.contrib import admin

from apps.ledger.models import Account, AccountMapping, JournalEntry, JournalLine


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = (
        "code",
        "name",
        "account_type",
        "subtype",
        "normal_balance",
        "is_postable",
        "is_control",
        "control_type",
        "is_contra",
        "is_active",
    )
    list_filter = (
        "account_type",
        "subtype",
        "is_postable",
        "is_control",
        "is_contra",
        "is_active",
    )
    search_fields = ("code", "name")
    ordering = ("code",)


@admin.register(AccountMapping)
class AccountMappingAdmin(admin.ModelAdmin):
    """CFG-007. Posting refuses to run when one of these is missing."""

    list_display = ("key", "account", "notes")
    search_fields = ("key", "account__code", "account__name")

    def has_delete_permission(self, request, obj=None):
        return False


class JournalLineInline(admin.TabularInline):
    model = JournalLine
    extra = 0
    can_delete = False
    fields = (
        "line_no",
        "account",
        "description",
        "debit_base",
        "credit_base",
        "customer",
        "vendor",
    )
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(JournalEntry)
class JournalEntryAdmin(admin.ModelAdmin):
    """Read-only: a posted journal is corrected by reversal, never by editing (BR-004)."""

    list_display = (
        "number",
        "entry_date",
        "journal_type",
        "status",
        "total_debit_base",
        "total_credit_base",
        "source_doc_number",
    )
    list_filter = ("journal_type", "status", "is_manual")
    search_fields = ("number", "narration", "source_doc_number")
    date_hierarchy = "entry_date"
    inlines = [JournalLineInline]
    readonly_fields = [f.name for f in JournalEntry._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
