"""
The weighted-average costing engine and the stock ledger / valuation screens.

BRD coverage: INV-003..INV-005, BR-017, BR-018, BR-019, RPT-016..RPT-018,
NFR-008.

Note: these tests need a real PostgreSQL test database (NFR-002) —
`python manage.py test apps.inventory`.
"""

from decimal import Decimal

from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.catalog.models import Product, UnitOfMeasure
from apps.core.models import DocumentType
from apps.core.permissions import OWNER_ADMIN
from apps.inventory import services
from apps.inventory.models import MovementType, StockBalance, StockMovement, Warehouse

ZERO = Decimal("0")


class PostStockMovementTests(TestCase):
    """Direct tests of the shared costing engine (post_stock_movement)."""

    @classmethod
    def setUpTestData(cls):
        cls.warehouse = Warehouse.objects.get(code="MAIN")
        cls.unit = UnitOfMeasure.objects.get(code="EA")
        cls.product = Product.objects.create(
            sku="SKU-ENGINE-1", name="Gadget", unit=cls.unit, purchase_price=Decimal("10.00")
        )

    def setUp(self):
        self.user = User.objects.create_user(
            username="engineer", email="engineer@example.com", password="testpass-12345"
        )

    def _receive(self, quantity, unit_cost, key):
        return services.post_stock_movement(
            product=self.product,
            warehouse=self.warehouse,
            movement_date="2026-08-31",
            movement_type=MovementType.GOODS_RECEIPT,
            quantity=Decimal(quantity),
            unit_cost=Decimal(unit_cost),
            source=self.product,
            source_doc_type=DocumentType.GOODS_RECEIPT,
            source_doc_number=key,
            idempotency_key=key,
            user=self.user,
        )

    def test_inbound_movement_blends_the_weighted_average(self):
        self._receive("10", "10.00", "T-1")
        self._receive("10", "20.00", "T-2")

        balance = StockBalance.objects.get(product=self.product, warehouse=self.warehouse)
        self.assertEqual(balance.quantity_on_hand, Decimal("20.0000"))
        self.assertEqual(balance.average_cost, Decimal("15.000000"))
        self.assertEqual(balance.total_value, Decimal("300.0000"))

    def test_outbound_movement_costs_at_the_current_average_not_the_caller_value(self):
        self._receive("10", "10.00", "T-3")
        self._receive("10", "20.00", "T-4")

        movement = services.post_stock_movement(
            product=self.product,
            warehouse=self.warehouse,
            movement_date="2026-08-31",
            movement_type=MovementType.ADJUSTMENT_OUT,
            quantity=Decimal("5"),
            unit_cost=Decimal("999.00"),  # must be ignored for an outbound line
            source=self.product,
            source_doc_type="",
            source_doc_number="",
            idempotency_key="T-5",
            user=self.user,
        )

        self.assertEqual(movement.unit_cost, Decimal("15.000000"))
        self.assertEqual(movement.total_cost, Decimal("75.0000"))

        balance = StockBalance.objects.get(product=self.product, warehouse=self.warehouse)
        self.assertEqual(balance.quantity_on_hand, Decimal("15.0000"))
        self.assertEqual(balance.average_cost, Decimal("15.000000"))
        self.assertEqual(balance.total_value, Decimal("225.0000"))

    def test_outbound_movement_leaves_zero_value_at_zero_quantity(self):
        self._receive("10", "10.00", "T-6")

        services.post_stock_movement(
            product=self.product,
            warehouse=self.warehouse,
            movement_date="2026-08-31",
            movement_type=MovementType.ADJUSTMENT_OUT,
            quantity=Decimal("10"),
            source=self.product,
            source_doc_type="",
            source_doc_number="",
            idempotency_key="T-7",
            user=self.user,
        )

        balance = StockBalance.objects.get(product=self.product, warehouse=self.warehouse)
        self.assertEqual(balance.quantity_on_hand, ZERO)
        self.assertEqual(balance.total_value, ZERO)

    def test_negative_stock_is_blocked_by_the_database_policy(self):
        self._receive("5", "10.00", "T-8")

        with self.assertRaises(ValidationError):
            services.post_stock_movement(
                product=self.product,
                warehouse=self.warehouse,
                movement_date="2026-08-31",
                movement_type=MovementType.ADJUSTMENT_OUT,
                quantity=Decimal("10"),
                source=self.product,
                source_doc_type="",
                source_doc_number="",
                idempotency_key="T-9",
                user=self.user,
            )

    def test_negative_stock_is_allowed_when_the_warehouse_opts_in(self):
        self.warehouse.allow_negative_stock = True
        self.warehouse.save(update_fields=["allow_negative_stock"])
        self._receive("5", "10.00", "T-10")

        movement = services.post_stock_movement(
            product=self.product,
            warehouse=self.warehouse,
            movement_date="2026-08-31",
            movement_type=MovementType.ADJUSTMENT_OUT,
            quantity=Decimal("10"),
            source=self.product,
            source_doc_type="",
            source_doc_number="",
            idempotency_key="T-11",
            user=self.user,
        )
        self.assertEqual(movement.balance_quantity_after, Decimal("-5.0000"))


class StockLedgerAndValuationScreenTests(TestCase):
    """The two read screens this module adds (RPT-016..RPT-018)."""

    @classmethod
    def setUpTestData(cls):
        cls.warehouse = Warehouse.objects.get(code="MAIN")
        cls.unit = UnitOfMeasure.objects.get(code="EA")
        cls.product = Product.objects.create(
            sku="SKU-SCREEN-1",
            name="Widget Pro",
            unit=cls.unit,
            purchase_price=Decimal("10.00"),
        )

    def setUp(self):
        self.owner = User.objects.create_user(
            username="owner3", email="owner3@example.com", password="testpass-12345"
        )
        self.owner.groups.add(Group.objects.get(name=OWNER_ADMIN))
        self.client.force_login(self.owner)
        services.post_stock_movement(
            product=self.product,
            warehouse=self.warehouse,
            movement_date="2026-08-31",
            movement_type=MovementType.GOODS_RECEIPT,
            quantity=Decimal("10"),
            unit_cost=Decimal("10.00"),
            source=self.product,
            source_doc_type=DocumentType.GOODS_RECEIPT,
            source_doc_number="GRN-SEED",
            idempotency_key="SEED-1",
            user=self.owner,
        )

    def test_stock_ledger_lists_posted_movements(self):
        response = self.client.get(reverse("inventory:stock_ledger"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total_count"], StockMovement.objects.count())
        self.assertContains(response, "SKU-SCREEN-1")

    def test_stock_ledger_search_narrows_to_one_product(self):
        response = self.client.get(reverse("inventory:stock_ledger"), {"q": "SKU-SCREEN-1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total_count"], 1)

    def test_valuation_view_shows_the_running_balance(self):
        response = self.client.get(reverse("inventory:stock_valuation"))
        self.assertEqual(response.status_code, 200)
        balance = StockBalance.objects.get(product=self.product, warehouse=self.warehouse)
        self.assertEqual(balance.quantity_on_hand, Decimal("10.0000"))
        self.assertContains(response, "SKU-SCREEN-1")

    def test_screens_require_login(self):
        self.client.logout()
        response = self.client.get(reverse("inventory:stock_ledger"))
        self.assertNotEqual(response.status_code, 200)
