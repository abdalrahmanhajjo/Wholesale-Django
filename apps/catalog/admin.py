"""Product catalogue in the admin (INV-001, INV-002)."""

from django.contrib import admin

from apps.catalog.models import Product, ProductCategory, ProductPrice, UnitOfMeasure


class ProductPriceInline(admin.TabularInline):
    model = ProductPrice
    extra = 0
    fields = ("kind", "currency", "price", "min_quantity", "valid_from", "valid_to")


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = (
        "sku",
        "name",
        "category",
        "unit",
        "product_type",
        "sales_price",
        "purchase_price",
        "reorder_level",
        "is_active",
    )
    list_filter = ("is_active", "product_type", "is_inventory", "category")
    search_fields = ("sku", "name", "barcode")
    inlines = [ProductPriceInline]
    autocomplete_fields = ("category", "unit", "preferred_vendor")


@admin.register(ProductCategory)
class ProductCategoryAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "parent", "is_active")
    search_fields = ("code", "name")


@admin.register(UnitOfMeasure)
class UnitOfMeasureAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "decimal_places", "is_active")
    search_fields = ("code", "name")
