"""Catalog screens: units, categories and products."""

from django.contrib import messages
from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.views.generic import CreateView, DetailView, UpdateView, View

from apps.catalog.forms import (
    ProductCategoryForm,
    ProductForm,
    ProductPriceForm,
    UnitOfMeasureForm,
)
from apps.catalog.models import (
    Product,
    ProductCategory,
    ProductPrice,
    ProductType,
    UnitOfMeasure,
)
from apps.core import audit
from apps.core.list_views import BooleanFilter, ChoiceFilter, Column, FilteredListView
from apps.core.mixins import ActionPermissionMixin, AuditedFormMixin
from apps.core.models import AuditEvent
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


# ---------------------------------------------------------------------------
# Products (CFG-011)
# ---------------------------------------------------------------------------
class ProductListView(FilteredListView):
    model = Product
    page_title = "Products"
    page_subtitle = "Everything you buy and sell."
    default_ordering = "sku"
    paginate_by = 25
    create_url_name = "catalog:product_create"
    create_label = "New product"

    columns = [
        Column("sku", "SKU", sortable=True, link=True, css="font-mono text-xs"),
        Column("name", "Name", sortable=True),
        Column("category", "Category"),
        Column("unit", "Unit", css="font-mono text-xs"),
        Column("get_product_type_display", "Type", sortable=True, order_by="product_type"),
        Column("is_inventory", "Stocked", align="center"),
        Column("sales_price", "Sales price", align="right", money=True, sortable=True),
        Column("is_active", "Active", badge=True, align="center"),
    ]

    search_fields = ["sku", "name", "barcode", "description"]
    trigram_search_fields = ["name"]

    filters = [
        ChoiceFilter("product_type", "Type", ProductType.choices),
        BooleanFilter(
            "is_inventory", "Stock", true_label="Stocked", false_label="Not stocked"
        ),
        BooleanFilter("is_active", "Status", true_label="Active", false_label="Inactive"),
    ]
    export_permission = EXPORT_DATA
    export_filename = "products"

    def get_queryset(self):
        return super().get_queryset().select_related("category", "unit")

    def get_summary(self):
        totals = Product.objects.aggregate(
            total=Count("id"),
            active=Count("id", filter=Q(is_active=True)),
            stocked=Count("id", filter=Q(is_inventory=True, is_active=True)),
        )
        return [
            ("Products", totals["total"]),
            ("Active", totals["active"]),
            ("Stocked", totals["stocked"]),
        ]


class ProductCreateView(AuditedFormMixin, ActionPermissionMixin, CreateView):
    model = Product
    form_class = ProductForm
    template_name = "catalog/product_form.html"
    required_permission = MANAGE_CONFIGURATION
    extra_context = {"page_title": "New product", "cancel_url": "/catalog/products/"}

    def get_success_url(self):
        return reverse("catalog:product_detail", args=[self.object.pk])


class ProductUpdateView(AuditedFormMixin, ActionPermissionMixin, UpdateView):
    model = Product
    form_class = ProductForm
    template_name = "catalog/product_form.html"
    required_permission = MANAGE_CONFIGURATION
    extra_context = {"cancel_url": "/catalog/products/"}

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = f"Edit {self.object.sku}"
        return ctx

    def get_success_url(self):
        return reverse("catalog:product_detail", args=[self.object.pk])


class ProductDetailView(ActionPermissionMixin, DetailView):
    model = Product
    template_name = "catalog/product_detail.html"
    required_permission = "catalog.view_product"
    context_object_name = "product"

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related(
                "category",
                "unit",
                "default_sales_tax_code",
                "default_purchase_tax_code",
                "preferred_vendor",
                "revenue_account",
                "cogs_account",
                "inventory_account",
                "expense_account",
            )
            .prefetch_related("prices__currency")
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(
            page_title=self.object.name,
            page_subtitle=f"Product {self.object.sku}",
            audit_events=AuditEvent.objects.filter(
                content_type__app_label="catalog",
                content_type__model="product",
                object_id=self.object.pk,
            ).select_related("user")[:20],
        )
        return ctx


# ---------------------------------------------------------------------------
# Price list (CFG-011)
# ---------------------------------------------------------------------------
class ProductPriceCreateView(AuditedFormMixin, ActionPermissionMixin, CreateView):
    model = ProductPrice
    form_class = ProductPriceForm
    template_name = "core/settings_form.html"
    required_permission = MANAGE_CONFIGURATION
    extra_context = {"page_title": "New price"}

    def get_product(self):
        return get_object_or_404(Product, pk=self.kwargs["product_pk"])

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["product"] = self.get_product()
        return kwargs

    def form_valid(self, form):
        form.instance.product = self.get_product()
        return super().form_valid(form)

    def get_success_url(self):
        return reverse("catalog:product_detail", args=[self.object.product_id])


class ProductPriceUpdateView(AuditedFormMixin, ActionPermissionMixin, UpdateView):
    model = ProductPrice
    form_class = ProductPriceForm
    template_name = "core/settings_form.html"
    required_permission = MANAGE_CONFIGURATION
    extra_context = {"page_title": "Edit price"}

    def get_success_url(self):
        return reverse("catalog:product_detail", args=[self.object.product_id])


class ProductPriceDeleteView(ActionPermissionMixin, View):
    """POST-only. A price list entry is configuration, not a posted document."""

    required_permission = MANAGE_CONFIGURATION

    def post(self, request, pk):
        price = get_object_or_404(ProductPrice, pk=pk)
        product_id = price.product_id
        with transaction.atomic():
            audit.record_delete(request, price)
            price.delete()
        messages.success(request, "Price removed.")
        return redirect("catalog:product_detail", pk=product_id)
