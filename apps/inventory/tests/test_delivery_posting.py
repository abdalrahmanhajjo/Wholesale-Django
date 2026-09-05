"""
Delivery note posting: the outbound half of the physical/GL pair with goods
receipts (INV-007, SAL-005, SAL-010).

BRD coverage: INV-005, INV-007, SAL-005, SAL-010, BR-017..BR-019, GL-001,
GL-002.

Note: these tests need a real PostgreSQL test database (NFR-002) —
`python manage.py test apps.inventory`.
"""

from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from apps.accounts.models import User
from apps.catalog.models import Product, UnitOfMeasure
from apps.core.models import Currency, DocumentStatus
from apps.inventory import services
from apps.inventory.models import DeliveryNote, DeliveryNoteLine, StockBalance, Warehouse
from apps.ledger.models import Account
from apps.parties.models import Customer
from apps.sales.models import SalesOrder, SalesOrderLine


class PostDeliveryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.usd = Currency.objects.get(code="USD")
        cls.warehouse = Warehouse.objects.get(code="MAIN")
        cls.unit = UnitOfMeasure.objects.get(code="EA")
        cls.customer = Customer.objects.create(
            code="C-DN-1", name="Delivery Test Customer", currency=cls.usd
        )
        cls.product = Product.objects.create(
            sku="SKU-DN-1", name="Deliverable Widget", unit=cls.unit
        )

    def setUp(self):
        self.user = User.objects.create_user(
            username="dnclerk", email="dnclerk@example.com", password="testpass-12345"
        )
        # Two receipts at different costs, so the delivery is costed at the
        # blended weighted average, not either individual purchase price.
        services.post_stock_movement(
            product=self.product,
            warehouse=self.warehouse,
            movement_date="2026-08-30",
            movement_type="GOODS_RECEIPT",
            quantity=Decimal("10"),
            unit_cost=Decimal("10.00"),
            source=self.product,
            source_doc_type="",
            source_doc_number="",
            idempotency_key="SEED-DN-1",
            user=self.user,
        )
        services.post_stock_movement(
            product=self.product,
            warehouse=self.warehouse,
            movement_date="2026-08-31",
            movement_type="GOODS_RECEIPT",
            quantity=Decimal("10"),
            unit_cost=Decimal("20.00"),
            source=self.product,
            source_doc_type="",
            source_doc_number="",
            idempotency_key="SEED-DN-2",
            user=self.user,
        )

    def _delivery(self, quantity="5", sales_order_line=None):
        delivery = DeliveryNote.objects.create(
            number="DN-TEST-1",
            document_date=date(2026, 9, 1),
            warehouse=self.warehouse,
            customer=self.customer,
        )
        line = DeliveryNoteLine.objects.create(
            delivery=delivery,
            line_no=1,
            product=self.product,
            unit=self.unit,
            quantity=Decimal(quantity),
            sales_order_line=sales_order_line,
        )
        services.recalculate_delivery(delivery)
        return delivery, line

    def test_post_moves_stock_and_writes_the_weighted_average_cost(self):
        delivery, _ = self._delivery(quantity="5")
        services.post_delivery(delivery, self.user, request=None)

        balance = StockBalance.objects.get(product=self.product, warehouse=self.warehouse)
        self.assertEqual(
            balance.quantity_on_hand, Decimal("15.0000")
        )  # 20 received - 5 shipped
        self.assertEqual(
            balance.average_cost, Decimal("15.000000")
        )  # unchanged by an outbound

        delivery.refresh_from_db()
        line = delivery.lines.get()
        self.assertEqual(line.unit_cost, Decimal("15.000000"))
        self.assertEqual(line.total_cost, Decimal("75.0000"))
        self.assertEqual(delivery.total_cost_base, Decimal("75.0000"))
        self.assertEqual(delivery.status, DocumentStatus.POSTED)

    def test_post_books_a_balanced_cogs_journal(self):
        delivery, _ = self._delivery(quantity="5")
        services.post_delivery(delivery, self.user, request=None)
        delivery.refresh_from_db()

        entry = delivery.journal_entry
        self.assertIsNotNone(entry)
        self.assertEqual(entry.total_debit_base, entry.total_credit_base)
        self.assertEqual(entry.total_debit_base, Decimal("75.0000"))

        cogs_account = Account.objects.get(code="5010")
        cogs_line = entry.lines.get(account=cogs_account)
        self.assertEqual(cogs_line.debit_base, Decimal("75.0000"))

        inventory_account = Account.objects.get(code="1310")
        inventory_line = entry.lines.get(account=inventory_account)
        self.assertEqual(inventory_line.credit_base, Decimal("75.0000"))

    def test_post_updates_the_linked_sales_order_line(self):
        order = SalesOrder.objects.create(
            number="SO-DN-TEST",
            document_date=date(2026, 8, 29),
            posting_date=date(2026, 8, 29),
            currency=self.usd,
            customer=self.customer,
            warehouse=self.warehouse,
        )
        so_line = SalesOrderLine.objects.create(
            order=order,
            line_no=1,
            product=self.product,
            unit=self.unit,
            quantity=Decimal("5"),
            unit_price=Decimal("25.00"),
        )
        delivery, _ = self._delivery(quantity="5", sales_order_line=so_line)
        services.post_delivery(delivery, self.user, request=None)

        so_line.refresh_from_db()
        self.assertEqual(so_line.quantity_delivered, Decimal("5.0000"))

    def test_cannot_deliver_more_than_is_on_hand(self):
        delivery, _ = self._delivery(quantity="999")
        with self.assertRaises(ValidationError):
            services.post_delivery(delivery, self.user, request=None)

    def test_only_draft_can_be_posted(self):
        delivery, _ = self._delivery(quantity="5")
        services.post_delivery(delivery, self.user, request=None)
        with self.assertRaises(ValidationError):
            services.post_delivery(delivery, self.user, request=None)
