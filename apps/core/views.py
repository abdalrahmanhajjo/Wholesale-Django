"""
Core views.

Only the shell for now: a role-aware dashboard placeholder and the sign-in
redirect target. The real dashboard widgets (UX-001) come once the other
members' modules have data to show.
"""

from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.urls import reverse_lazy
from django.views.generic import CreateView, UpdateView

from apps.core.forms import CurrencyForm, TaxCodeForm
from apps.core.list_views import BooleanFilter, ChoiceFilter, Column, FilteredListView
from apps.core.mixins import ActionPermissionMixin, AuditedFormMixin
from apps.core.models import Company, Currency, FiscalPeriod, TaxCode, TaxTreatment, TaxApplicability
from apps.core.permissions import EXPORT_DATA, MANAGE_CONFIGURATION
from apps.ledger.models import Account, AccountMapping, MappingKey
from apps.core.forms import (
    CompanyForm,
    CurrencyForm,
    DocumentSequenceForm,
    PaymentTermForm,
    TaxCodeForm,
)
from apps.core.list_views import (
    BooleanFilter,
    ChoiceFilter,
    Column,
    DateRangeFilter,
    FilteredListView,
)
from apps.core.models import (
    Company,
    Currency,
    DocumentSequence,
    DocumentType,
    FiscalPeriod,
    PaymentTerm,
    PeriodStatus,
    SequenceReset,
    TaxApplicability,
    TaxCode,
    TaxTreatment,
)


@login_required
def dashboard(request):
    """
    Landing page after sign-in.

    Deliberately shows configuration readiness rather than fake figures: until
    Members 2, 3 and 4 post real documents there is nothing financial to report,
    and a dashboard of zeroes reads as a broken system rather than an empty one.
    """
    company = Company.objects.select_related("base_currency").first()
    open_periods = FiscalPeriod.objects.filter(status="OPEN").order_by("start_date")
    missing_mappings = sorted(
        set(dict(MappingKey.choices))
        - set(AccountMapping.objects.values_list("key", flat=True))
    )

    context = {
        "page_title": f"Good day, {request.user.full_name or request.user.username}.",
        "page_subtitle": "Configuration is in place. Operational modules arrive with the other slices.",
        "company": company,
        "base_currency": company.base_currency_id if company else None,
        "current_period": open_periods.first(),
        "open_period_count": open_periods.count(),
        "account_count": Account.objects.filter(is_active=True).count(),
        "postable_account_count": Account.objects.filter(
            is_active=True, is_postable=True
        ).count(),
        "missing_mappings": missing_mappings,
        "role": request.user.groups.first(),
    }
    return render(request, "core/dashboard.html", context)


class CurrencyListView(FilteredListView):
    """CFG-003: the currencies the business trades in."""

    model = Currency
    page_title = "Currencies"
    page_subtitle = "Currencies available on documents. One is the base currency (BR-002)."
    default_ordering = "code"
    create_url_name = "core:currency_create"
    create_label = "New currency"

    columns = [
        Column("code", "Code", sortable=True, link=True, css="font-mono text-xs"),
        Column("name", "Name", sortable=True),
        Column("symbol", "Symbol"),
        Column("decimal_places", "Decimals", align="right"),
        Column("is_base", "Base", badge=True, align="center"),
        Column("is_active", "Active", badge=True, align="center"),
    ]

    search_fields = ["code", "name"]

    filters = [
        BooleanFilter("is_active", "Status", true_label="Active", false_label="Inactive"),
    ]

    export_permission = EXPORT_DATA
    export_filename = "currencies"

    def get_summary(self):
        return [
            ("Currencies", Currency.objects.count()),
            ("Active", Currency.objects.filter(is_active=True).count()),
            ("Base", Currency.objects.filter(is_base=True).first() or "—"),
        ]


class CurrencyCreateView(AuditedFormMixin, ActionPermissionMixin, CreateView):
    model = Currency
    form_class = CurrencyForm
    template_name = "core/currency_form.html"
    required_permission = MANAGE_CONFIGURATION
    success_url = reverse_lazy("core:currency_list")
    extra_context = {"page_title": "New currency"}


class CurrencyUpdateView(AuditedFormMixin, ActionPermissionMixin, UpdateView):
    model = Currency
    form_class = CurrencyForm
    template_name = "core/currency_form.html"
    required_permission = MANAGE_CONFIGURATION
    success_url = reverse_lazy("core:currency_list")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = f"Edit {self.object.code}"
        return ctx


class TaxCodeListView(FilteredListView):
    """CFG-004: the tax codes available on documents."""

    model = TaxCode
    page_title = "Tax codes"
    page_subtitle = "Rates and treatments applied to sales and purchase lines."
    default_ordering = "code"
    create_url_name = "core:taxcode_create"
    create_label = "New tax code"

    columns = [
        Column("code", "Code", sortable=True, link=True, css="font-mono text-xs"),
        Column("name", "Name", sortable=True),
        Column("rate_percent", "Rate %", align="right", sortable=True),
        Column("get_treatment_display", "Treatment"),
        Column("get_applies_to_display", "Applies to"),
        Column("is_inclusive", "Inclusive", align="center"),
        Column("is_recoverable", "Recoverable", align="center"),
        Column("is_active", "Active", badge=True, align="center"),
    ]

    search_fields = ["code", "name"]

    filters = [
        ChoiceFilter("treatment", "Treatment", TaxTreatment.choices),
        ChoiceFilter("applies_to", "Applies to", TaxApplicability.choices),
        BooleanFilter("is_active", "Status", true_label="Active", false_label="Inactive"),
    ]

    export_permission = EXPORT_DATA
    export_filename = "tax-codes"

    def get_summary(self):
        return [
            ("Tax codes", TaxCode.objects.count()),
            ("Active", TaxCode.objects.filter(is_active=True).count()),
            ("Standard rated", TaxCode.objects.filter(treatment=TaxTreatment.STANDARD).count()),
        ]


class TaxCodeCreateView(AuditedFormMixin, ActionPermissionMixin, CreateView):
    model = TaxCode
    form_class = TaxCodeForm
    template_name = "core/taxcode_form.html"
    required_permission = MANAGE_CONFIGURATION
    success_url = reverse_lazy("core:taxcode_list")
    extra_context = {"page_title": "New tax code"}


class TaxCodeUpdateView(AuditedFormMixin, ActionPermissionMixin, UpdateView):
    model = TaxCode
    form_class = TaxCodeForm
    template_name = "core/taxcode_form.html"
    required_permission = MANAGE_CONFIGURATION
    success_url = reverse_lazy("core:taxcode_list")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = f"Edit {self.object.code}"
        return ctx

# ---------------------------------------------------------------------------
# Payment terms (CFG-005)
# ---------------------------------------------------------------------------
class PaymentTermListView(FilteredListView):
    model = PaymentTerm
    page_title = "Payment terms"
    page_subtitle = "When an invoice falls due, and any early-settlement discount."
    default_ordering = "code"
    create_url_name = "core:paymentterm_create"
    create_label = "New payment term"

    columns = [
        Column("code", "Code", sortable=True, link=True, css="font-mono text-xs"),
        Column("name", "Name", sortable=True),
        Column("net_days", "Net days", align="right", sortable=True),
        Column("end_of_month", "End of month", align="center"),
        Column("discount_percent", "Discount %", align="right"),
        Column("discount_days", "Discount days", align="right"),
        Column("is_active", "Active", badge=True, align="center"),
    ]
    search_fields = ["code", "name"]
    filters = [
        BooleanFilter("is_active", "Status", true_label="Active", false_label="Inactive")
    ]
    export_permission = EXPORT_DATA
    export_filename = "payment-terms"

    def get_summary(self):
        return [
            ("Payment terms", PaymentTerm.objects.count()),
            ("Active", PaymentTerm.objects.filter(is_active=True).count()),
        ]


class PaymentTermCreateView(AuditedFormMixin, ActionPermissionMixin, CreateView):
    model = PaymentTerm
    form_class = PaymentTermForm
    template_name = "core/settings_form.html"
    required_permission = MANAGE_CONFIGURATION
    success_url = reverse_lazy("core:paymentterm_list")
    extra_context = {
        "page_title": "New payment term",
        "cancel_url": "/settings/payment-terms/",
    }


class PaymentTermUpdateView(AuditedFormMixin, ActionPermissionMixin, UpdateView):
    model = PaymentTerm
    form_class = PaymentTermForm
    template_name = "core/settings_form.html"
    required_permission = MANAGE_CONFIGURATION
    success_url = reverse_lazy("core:paymentterm_list")
    extra_context = {"cancel_url": "/settings/payment-terms/"}

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = f"Edit {self.object.code}"
        return ctx


# ---------------------------------------------------------------------------
# Company settings (CFG-001, CFG-010)
# ---------------------------------------------------------------------------
class CompanySettingsView(AuditedFormMixin, ActionPermissionMixin, UpdateView):
    """
    A singleton screen: one row, edited in place. There is no list and no
    create — the company is seeded, and BRD 3.1 allows exactly one.
    """

    model = Company
    form_class = CompanyForm
    template_name = "core/settings_form.html"
    required_permission = MANAGE_CONFIGURATION
    success_url = reverse_lazy("core:company_settings")
    extra_context = {
        "page_title": "Company settings",
        "cancel_url": "/",
        "form_hint": "These values appear on every document and drive system-wide policy.",
    }

    def get_object(self, queryset=None):
        return Company.objects.select_related("base_currency").first()


# ---------------------------------------------------------------------------
# Number series (CFG-008)
# ---------------------------------------------------------------------------
class DocumentSequenceListView(FilteredListView):
    model = DocumentSequence
    page_title = "Number series"
    page_subtitle = "How each document type is numbered. Used by numbering.next_number()."
    default_ordering = "document_type"
    create_url_name = "core:sequence_create"
    create_label = "New series"

    columns = [
        Column("get_document_type_display", "Document", sortable=True, link=True,
               order_by="document_type"),
        Column("series", "Series", css="font-mono text-xs"),
        Column("prefix", "Prefix", css="font-mono text-xs"),
        Column("padding", "Padding", align="right"),
        Column("next_number", "Next number", align="right", sortable=True),
        Column("get_reset_policy_display", "Resets"),
        Column("period_key", "Period", css="font-mono text-xs"),
        Column("is_active", "Active", badge=True, align="center"),
    ]
    search_fields = ["series", "prefix"]
    filters = [
        ChoiceFilter("document_type", "Document type", DocumentType.choices),
        ChoiceFilter("reset_policy", "Resets", SequenceReset.choices),
        BooleanFilter("is_active", "Status", true_label="Active", false_label="Inactive"),
    ]
    export_permission = EXPORT_DATA
    export_filename = "number-series"

    def get_summary(self):
        return [
            ("Series", DocumentSequence.objects.count()),
            ("Active", DocumentSequence.objects.filter(is_active=True).count()),
        ]


class DocumentSequenceCreateView(AuditedFormMixin, ActionPermissionMixin, CreateView):
    model = DocumentSequence
    form_class = DocumentSequenceForm
    template_name = "core/settings_form.html"
    required_permission = MANAGE_CONFIGURATION
    success_url = reverse_lazy("core:sequence_list")
    extra_context = {"page_title": "New number series", "cancel_url": "/settings/number-series/"}


class DocumentSequenceUpdateView(AuditedFormMixin, ActionPermissionMixin, UpdateView):
    model = DocumentSequence
    form_class = DocumentSequenceForm
    template_name = "core/settings_form.html"
    required_permission = MANAGE_CONFIGURATION
    success_url = reverse_lazy("core:sequence_list")
    extra_context = {"cancel_url": "/settings/number-series/"}

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = f"Edit {self.object}"
        return ctx


# ---------------------------------------------------------------------------
# Fiscal calendar (CFG-009) — read-only here. Member 4 owns close/reopen.
# ---------------------------------------------------------------------------
class FiscalPeriodListView(FilteredListView):
    model = FiscalPeriod
    page_title = "Fiscal periods"
    page_subtitle = (
        "Posting windows. Closing and reopening happen through the accounting "
        "workflow, not from this screen (CFG-009, ACC-008)."
    )
    default_ordering = "start_date"
    paginate_by = 50

    columns = [
        Column("name", "Period", sortable=True),
        Column("fiscal_year", "Year", sortable=True),
        Column("period_no", "No.", align="right"),
        Column("start_date", "Starts", sortable=True),
        Column("end_date", "Ends", sortable=True),
        Column("status", "Status", badge=True, align="center"),
        Column("closed_by", "Closed by"),
    ]
    search_fields = ["name", "fiscal_year__code"]
    filters = [
        ChoiceFilter("status", "Status", PeriodStatus.choices),
        DateRangeFilter("start_date", "Starting between"),
    ]
    export_permission = EXPORT_DATA
    export_filename = "fiscal-periods"

    def get_queryset(self):
        return super().get_queryset().select_related("fiscal_year", "closed_by")

    def get_summary(self):
        return [
            ("Periods", FiscalPeriod.objects.count()),
            ("Open", FiscalPeriod.objects.filter(status=PeriodStatus.OPEN).count()),
            ("Closed", FiscalPeriod.objects.exclude(status=PeriodStatus.OPEN).count()),
        ]