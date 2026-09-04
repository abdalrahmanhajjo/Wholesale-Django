"""Catalog screens: units, categories and products."""

from django.db.models import Count, Q
from django.urls import reverse_lazy
from django.views.generic import CreateView, UpdateView

from apps.catalog.forms import ProductCategoryForm, UnitOfMeasureForm
from apps.catalog.models import ProductCategory, UnitOfMeasure
from apps.core.list_views import BooleanFilter, Column, FilteredListView
from apps.core.mixins import ActionPermissionMixin, AuditedFormMixin
from apps.core.permissions import EXPORT_DATA, MANAGE_CONFIGURATION


# ---------------------------------------------------------------------------
# Units of measure
# ---------------------------------------------------------------------------
class UnitOfMeasureListView(FilteredListView):
    model = UnitOfMeasure
    page_title = "Units of measure"
    page_subtitle = "How quantities are counted, and how packaging converts to a base unit."
    default_ordering = "code"
    create_url_name = "catalog:unit_create"
    create_label = "New unit"

    columns = [
        Column("code", "Code", sortable=True, link=True, css="font-mono text-xs"),
        Column("name", "Name", sortable=True),
        Column("decimal_places", "Decimals", align="right"),
        Column("base_unit", "Base unit", css="font-mono text-xs"),
        Column("ratio_to_base", "Ratio", align="right"),
        Column("is_active", "Active", badge=True, align="center"),
    ]
    search_fields = ["code", "name"]
    filters = [
        BooleanFilter("is_active", "Status", true_label="Active", false_label="Inactive")
    ]
    export_permission = EXPORT_DATA
    export_filename = "units-of-measure"

    def get_queryset(self):
        return super().get_queryset().select_related("base_unit")

    def get_summary(self):
        totals = UnitOfMeasure.objects.aggregate(
            total=Count("id"),
            active=Count("id", filter=Q(is_active=True)),
            base=Count("id", filter=Q(base_unit__isnull=True)),
        )
        return [
            ("Units", totals["total"]),
            ("Active", totals["active"]),
            ("Base units", totals["base"]),
        ]


class UnitOfMeasureCreateView(AuditedFormMixin, ActionPermissionMixin, CreateView):
    model = UnitOfMeasure
    form_class = UnitOfMeasureForm
    template_name = "core/settings_form.html"
    required_permission = MANAGE_CONFIGURATION
    success_url = reverse_lazy("catalog:unit_list")
    extra_context = {"page_title": "New unit", "cancel_url": "/catalog/units/"}


class UnitOfMeasureUpdateView(AuditedFormMixin, ActionPermissionMixin, UpdateView):
    model = UnitOfMeasure
    form_class = UnitOfMeasureForm
    template_name = "core/settings_form.html"
    required_permission = MANAGE_CONFIGURATION
    success_url = reverse_lazy("catalog:unit_list")
    extra_context = {"cancel_url": "/catalog/units/"}

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = f"Edit {self.object.code}"
        return ctx


# ---------------------------------------------------------------------------
# Product categories
# ---------------------------------------------------------------------------
class ProductCategoryListView(FilteredListView):
    model = ProductCategory
    page_title = "Product categories"
    page_subtitle = "Groups products, and can steer their postings (CFG-007)."
    default_ordering = "code"
    create_url_name = "catalog:category_create"
    create_label = "New category"

    columns = [
        Column("code", "Code", sortable=True, link=True, css="font-mono text-xs"),
        Column("name", "Name", sortable=True),
        Column("parent", "Parent"),
        Column("revenue_account", "Revenue"),
        Column("cogs_account", "COGS"),
        Column("inventory_account", "Inventory"),
        Column("is_active", "Active", badge=True, align="center"),
    ]
    search_fields = ["code", "name"]
    filters = [
        BooleanFilter("is_active", "Status", true_label="Active", false_label="Inactive")
    ]
    export_permission = EXPORT_DATA
    export_filename = "product-categories"

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related("parent", "revenue_account", "cogs_account", "inventory_account")
        )

    def get_summary(self):
        totals = ProductCategory.objects.aggregate(
            total=Count("id"), active=Count("id", filter=Q(is_active=True))
        )
        return [("Categories", totals["total"]), ("Active", totals["active"])]


class ProductCategoryCreateView(AuditedFormMixin, ActionPermissionMixin, CreateView):
    model = ProductCategory
    form_class = ProductCategoryForm
    template_name = "core/settings_form.html"
    required_permission = MANAGE_CONFIGURATION
    success_url = reverse_lazy("catalog:category_list")
    extra_context = {"page_title": "New category", "cancel_url": "/catalog/categories/"}


class ProductCategoryUpdateView(AuditedFormMixin, ActionPermissionMixin, UpdateView):
    model = ProductCategory
    form_class = ProductCategoryForm
    template_name = "core/settings_form.html"
    required_permission = MANAGE_CONFIGURATION
    success_url = reverse_lazy("catalog:category_list")
    extra_context = {"cancel_url": "/catalog/categories/"}

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = f"Edit {self.object.code}"
        return ctx
