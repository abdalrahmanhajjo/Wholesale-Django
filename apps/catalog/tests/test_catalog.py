"""
Catalog screens and pricing (CFG-011, SAL-010, FTD-008).

The rules under test are the ones the schema cannot express: a unit that
converts to itself, a category that is its own ancestor, a service that
carries stock, and which price applies on a given day.
"""

from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from apps.catalog.models import (
    PriceKind,
    Product,
    ProductCategory,
    ProductPrice,
    ProductType,
    UnitOfMeasure,
)
from apps.catalog.pricing import price_for
from apps.core.models import Currency
from apps.core.permissions import OWNER_ADMIN
from apps.core.tests.factories import make_user


class CatalogScreenTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.usd = Currency.objects.get(code="USD")
        cls.each = UnitOfMeasure.objects.get(code="EA")
        cls.category = ProductCategory.objects.get(code="GEN")
        cls.product = Product.objects.create(
            sku="SUG-1KG",
            name="White Sugar 1 kg",
            unit=cls.each,
            category=cls.category,
            sales_price=Decimal("2.50"),
        )

    def setUp(self):
        self.user = make_user("cat-admin", OWNER_ADMIN)
        self.client.force_login(self.user)

    # -- units -------------------------------------------------------------
    def test_a_base_unit_must_have_a_ratio_of_one(self):
        response = self.client.post(
            reverse("catalog:unit_create"),
            {
                "code": "PLT",
                "name": "Pallet",
                "decimal_places": 0,
                "base_unit": "",
                "ratio_to_base": "5",
                "is_active": "on",
            },
        )
        self.assertEqual(response.status_code, 200)  # redisplayed, not saved
        self.assertFalse(UnitOfMeasure.objects.filter(code="PLT").exists())

    def test_a_derived_unit_saves(self):
        response = self.client.post(
            reverse("catalog:unit_create"),
            {
                "code": "PLT",
                "name": "Pallet",
                "decimal_places": 0,
                "base_unit": self.each.pk,
                "ratio_to_base": "48",
                "is_active": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(UnitOfMeasure.objects.get(code="PLT").base_unit, self.each)

    # -- categories --------------------------------------------------------
    def test_a_category_cannot_be_its_own_ancestor(self):
        child = ProductCategory.objects.create(
            code="CHILD", name="Child", parent=self.category
        )
        # Try to make the parent a child of its own child.
        response = self.client.post(
            reverse("catalog:category_edit", args=[self.category.pk]),
            {"code": "GEN", "name": "General", "parent": child.pk, "is_active": "on"},
        )
        self.assertEqual(response.status_code, 200)
        self.category.refresh_from_db()
        self.assertIsNone(self.category.parent)

    # -- products ----------------------------------------------------------
    def test_a_service_cannot_be_an_inventory_item(self):
        """SAL-010: only a stocked item carries stock."""
        response = self.client.post(
            reverse("catalog:product_create"),
            {
                "sku": "SVC-1",
                "name": "Delivery service",
                "unit": self.each.pk,
                "product_type": ProductType.SERVICE,
                "is_inventory": "on",
                "sales_price": "10",
                "purchase_price": "0",
                "reorder_level": "0",
                "max_discount_percent": "100",
                "is_active": "on",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Product.objects.filter(sku="SVC-1").exists())

    def test_duplicate_sku_is_blocked_case_insensitively(self):
        response = self.client.post(
            reverse("catalog:product_create"),
            {
                "sku": "sug-1kg",
                "name": "Another sugar",
                "unit": self.each.pk,
                "product_type": ProductType.STOCK,
                "is_inventory": "on",
                "sales_price": "1",
                "purchase_price": "1",
                "reorder_level": "0",
                "max_discount_percent": "100",
                "is_active": "on",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Product.objects.filter(name="Another sugar").count(), 0)

    # -- prices ------------------------------------------------------------
    def _price(self, price, min_quantity="0", valid_from=date(2026, 1, 1), valid_to=None):
        return ProductPrice.objects.create(
            product=self.product,
            kind=PriceKind.SALES,
            currency=self.usd,
            price=Decimal(price),
            min_quantity=Decimal(min_quantity),
            valid_from=valid_from,
            valid_to=valid_to,
        )

    def test_quantity_break_wins_over_the_base_price(self):
        self._price("2.50")
        self._price("2.00", min_quantity="100")

        on = date(2026, 6, 1)
        self.assertEqual(
            price_for(self.product, on, quantity=50, currency=self.usd), Decimal("2.50")
        )
        self.assertEqual(
            price_for(self.product, on, quantity=100, currency=self.usd), Decimal("2.00")
        )

    def test_a_price_outside_its_window_does_not_apply(self):
        self._price("2.00", valid_from=date(2026, 1, 1), valid_to=date(2026, 3, 31))

        self.assertIsNone(
            price_for(self.product, date(2026, 6, 1), quantity=1, currency=self.usd)
        )

    def test_no_price_list_returns_none_so_the_caller_falls_back(self):
        self.assertIsNone(
            price_for(self.product, date(2026, 6, 1), quantity=1, currency=self.usd)
        )

    def test_overlapping_windows_are_refused(self):
        self._price("2.50", valid_from=date(2026, 1, 1))

        response = self.client.post(
            reverse("catalog:price_create", args=[self.product.pk]),
            {
                "kind": PriceKind.SALES,
                "currency": self.usd.pk,
                "price": "2.20",
                "min_quantity": "0",
                "valid_from": "2026-06-01",
                "valid_to": "",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.product.prices.count(), 1)
