"""
Product catalogue.

BRD coverage: INV-001, INV-002, INV-012, FTD-008, BR-010.
"""

from decimal import Decimal

from django.db import models
from django.db.models import F, Q
from django.urls import reverse

from apps.core.models import MONEY, PCT, QTY, TimeStampedModel

ZERO = Decimal("0")


class UnitOfMeasure(models.Model):
    """
    MVP: one product has one base unit (BRD 14.1). `ratio_to_base` is present so
    packaging conversion (OD-06) can be introduced without a table rewrite, but
    the MVP keeps every product on its base unit.
    """

    code = models.CharField(max_length=10, unique=True)
    name = models.CharField(max_length=50)
    decimal_places = models.PositiveSmallIntegerField(default=2)
    base_unit = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="derived_units"
    )
    ratio_to_base = models.DecimalField(**QTY, default=Decimal("1"))
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "unit_of_measure"
        ordering = ["code"]
        constraints = [
            models.CheckConstraint(
                condition=Q(ratio_to_base__gt=0), name="uom_ratio_positive"
            ),
            models.CheckConstraint(
                condition=Q(base_unit__isnull=True) | ~Q(base_unit=F("id")),
                name="uom_not_self_base",
            ),
        ]

    def __str__(self):
        return self.code

    def get_absolute_url(self):
        return reverse("catalog:unit_edit", args=[self.pk])


class ProductCategory(models.Model):
    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=100)
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="children"
    )
    # Optional account overrides so a category can steer postings (CFG-007).
    revenue_account = models.ForeignKey(
        "ledger.Account", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    cogs_account = models.ForeignKey(
        "ledger.Account", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    inventory_account = models.ForeignKey(
        "ledger.Account", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "product_category"
        ordering = ["code"]
        verbose_name_plural = "product categories"
        constraints = [
            models.CheckConstraint(
                condition=Q(parent__isnull=True) | ~Q(parent=F("id")),
                name="product_category_not_self_parent",
            )
        ]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("catalog:category_edit", args=[self.pk])


class ProductType(models.TextChoices):
    STOCK = "STOCK", "Stocked item"
    NON_STOCK = "NON_STOCK", "Non-stock item"
    SERVICE = "SERVICE", "Service"


class Product(TimeStampedModel):
    sku = models.CharField(max_length=40, unique=True)
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    barcode = models.CharField(max_length=64, blank=True)
    category = models.ForeignKey(
        ProductCategory,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="products",
    )
    unit = models.ForeignKey(UnitOfMeasure, on_delete=models.PROTECT, related_name="products")
    product_type = models.CharField(
        max_length=10, choices=ProductType.choices, default=ProductType.STOCK
    )
    # INV-001: only stocked items move inventory and post COGS (SAL-010).
    is_inventory = models.BooleanField(default=True)

    sales_price = models.DecimalField(**MONEY, default=ZERO)
    purchase_price = models.DecimalField(**MONEY, default=ZERO)
    default_sales_tax_code = models.ForeignKey(
        "core.TaxCode", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    default_purchase_tax_code = models.ForeignKey(
        "core.TaxCode", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    preferred_vendor = models.ForeignKey(
        "parties.Vendor", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    reorder_level = models.DecimalField(**QTY, default=ZERO)  # RPT-018
    max_discount_percent = models.DecimalField(**PCT, default=Decimal("100"))  # FTD-008

    # Account overrides; fall back to category, then AccountMapping.
    revenue_account = models.ForeignKey(
        "ledger.Account", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    cogs_account = models.ForeignKey(
        "ledger.Account", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    inventory_account = models.ForeignKey(
        "ledger.Account", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    expense_account = models.ForeignKey(
        "ledger.Account", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "product"
        ordering = ["sku"]
        constraints = [
            models.CheckConstraint(
                condition=Q(sales_price__gte=0) & Q(purchase_price__gte=0),
                name="product_prices_nonneg",
            ),
            models.CheckConstraint(
                condition=Q(reorder_level__gte=0), name="product_reorder_nonneg"
            ),
            models.CheckConstraint(
                condition=Q(max_discount_percent__gte=0) & Q(max_discount_percent__lte=100),
                name="product_max_discount_range",
            ),
            # A service or non-stock item never carries inventory (SAL-010).
            models.CheckConstraint(
                condition=Q(product_type="STOCK") | Q(is_inventory=False),
                name="product_nonstock_not_inventory",
            ),
        ]
        indexes = [
            models.Index(fields=["name"], name="ix_product_name"),
            models.Index(fields=["barcode"], name="ix_product_barcode"),
            models.Index(fields=["is_active", "sku"], name="ix_product_selectable"),
            models.Index(fields=["category", "is_active"], name="ix_product_category"),
        ]

    def __str__(self):
        return f"{self.sku} {self.name}"

    def get_absolute_url(self):
        return reverse("catalog:product_detail", args=[self.pk])


class PriceKind(models.TextChoices):
    SALES = "SALES", "Sales"
    PURCHASE = "PURCHASE", "Purchase"


class ProductPrice(models.Model):
    """INV-002: optional per-currency, date-effective price list."""

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="prices")
    kind = models.CharField(max_length=8, choices=PriceKind.choices, default=PriceKind.SALES)
    currency = models.ForeignKey("core.Currency", on_delete=models.PROTECT, related_name="+")
    price = models.DecimalField(**MONEY)
    min_quantity = models.DecimalField(**QTY, default=ZERO)
    valid_from = models.DateField()
    valid_to = models.DateField(null=True, blank=True)

    class Meta:
        db_table = "product_price"
        ordering = ["product", "kind", "-valid_from"]
        constraints = [
            models.UniqueConstraint(
                fields=["product", "kind", "currency", "min_quantity", "valid_from"],
                name="product_price_unique_point",
            ),
            models.CheckConstraint(condition=Q(price__gte=0), name="product_price_nonneg"),
            models.CheckConstraint(
                condition=Q(valid_to__isnull=True) | Q(valid_to__gte=F("valid_from")),
                name="product_price_dates_ordered",
            ),
        ]
        indexes = [
            models.Index(
                fields=["product", "kind", "currency", "-valid_from"],
                name="ix_product_price_lookup",
            )
        ]

    def __str__(self):
        return f"{self.product_id} {self.kind} {self.currency_id} {self.price}"
