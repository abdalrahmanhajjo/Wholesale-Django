"""
Goods receipt screens and the INV-006 stock + GRNI posting.

BRD coverage: PUR-003, PUR-004, INV-003..INV-006, BR-017..BR-019, ACC-004,
ACC-005, CFG-008, NFR-008.

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
from apps.core.models import Currency, DocumentStatus
from apps.core.permissions import OWNER_ADMIN, WAREHOUSE
from apps.inventory import services
from apps.inventory.models import GoodsReceipt, StockBalance, Warehouse
from apps.parties.models import Vendor


class GoodsReceiptScreenTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.usd = Currency.objects.get(code="USD")
        cls.warehouse = Warehouse.objects.get(code="MAIN")
        cls.unit = UnitOfMeasure.objects.get(code="EA")
        cls.vendor = Vendor.objects.create(
            code="V-0001", name="Acme Supplies", currency=cls.usd
        )
        cls.product = Product.objects.create(
            sku="SKU-1", name="Widget", unit=cls.unit, purchase_price=Decimal("10.00")
        )

    def setUp(self):
        self.clerk = User.objects.create_user(
            username="whclerk", email="whclerk@example.com", password="testpass-12345"
        )
        self.clerk.groups.add(Group.objects.get(name=WAREHOUSE))
        self.owner = User.objects.create_user(
            username="owner2", email="owner2@example.com", password="testpass-12345"
        )
        self.owner.groups.add(Group.objects.get(name=OWNER_ADMIN))
        self.client.force_login(self.clerk)

    # -- helpers --------------------------------------------------------------
    def _header(self, **overrides):
        data = {
            "vendor": self.vendor.pk,
            "purchase_order": "",
            "warehouse": self.warehouse.pk,
            "document_date": "2026-08-31",
            "vendor_delivery_note": "",
            "received_by": "",
            "reference": "",
            "notes": "",
        }
        data.update(overrides)
        return data

    def _one_line(self, received="10", rejected="0"):
        return {
            "lines-TOTAL_FORMS": "1",
            "lines-INITIAL_FORMS": "0",
            "lines-MIN_NUM_FORMS": "0",
            "lines-MAX_NUM_FORMS": "1000",
            "lines-0-purchase_order_line": "",
            "lines-0-product": self.product.pk,
            "lines-0-description": "",
            "lines-0-unit": self.unit.pk,
            "lines-0-quantity_received": received,
            "lines-0-quantity_rejected": rejected,
            "lines-0-rejection_reason": "",
        }

    def _create_receipt(self, header=None, lines=None):
        data = {**self._header(**(header or {})), **(lines or self._one_line())}
        response = self.client.post(reverse("inventory:gr_create"), data)
        self.assertEqual(response.status_code, 302, getattr(response, "context", None))
        return GoodsReceipt.objects.latest("id")

    # -- create, split and cost (PUR-003, PUR-004) -----------------------------
    def test_create_allocates_a_sequential_number(self):
        first = self._create_receipt()
        second = self._create_receipt()
        self.assertEqual(first.number, "GRN-00001")
        self.assertEqual(second.number, "GRN-00002")
        self.assertEqual(first.status, DocumentStatus.DRAFT)

    def test_accepted_quantity_and_cost_are_derived(self):
        receipt = self._create_receipt(lines=self._one_line(received="10", rejected="3"))
        line = receipt.lines.get()
        self.assertEqual(line.quantity_accepted, Decimal("7.0000"))
        self.assertEqual(line.unit_cost, Decimal("10.000000"))
        self.assertEqual(line.total_cost, Decimal("70.0000"))
        self.assertEqual(receipt.total_cost_base, Decimal("70.0000"))

    def test_cannot_reject_more_than_received(self):
        data = {
            **self._header(),
            **self._one_line(received="5", rejected="6"),
        }
        response = self.client.post(reverse("inventory:gr_create"), data)
        self.assertEqual(response.status_code, 200)  # redisplayed, not saved
        self.assertFalse(GoodsReceipt.objects.exists())

    # -- posting (INV-006, BR-019, NFR-008) ------------------------------------
    def test_post_updates_stock_balance_with_weighted_average(self):
        receipt = self._create_receipt(lines=self._one_line(received="10", rejected="0"))
        response = self.client.post(reverse("inventory:gr_post", args=[receipt.pk]))
        self.assertRedirects(response, reverse("inventory:gr_detail", args=[receipt.pk]))

        balance = StockBalance.objects.get(product=self.product, warehouse=self.warehouse)
        self.assertEqual(balance.quantity_on_hand, Decimal("10.0000"))
        self.assertEqual(balance.average_cost, Decimal("10.000000"))
        self.assertEqual(balance.total_value, Decimal("100.0000"))

        receipt.refresh_from_db()
        self.assertEqual(receipt.status, DocumentStatus.POSTED)
        self.assertIsNotNone(receipt.journal_entry)

    def test_post_writes_a_balanced_journal(self):
        receipt = self._create_receipt(lines=self._one_line(received="10", rejected="0"))
        self.client.post(reverse("inventory:gr_post", args=[receipt.pk]))
        receipt.refresh_from_db()

        entry = receipt.journal_entry
        self.assertEqual(entry.total_debit_base, entry.total_credit_base)
        self.assertEqual(entry.total_debit_base, Decimal("100.0000"))
        self.assertEqual(entry.lines.count(), 2)

    def test_second_receipt_blends_into_the_weighted_average(self):
        first = self._create_receipt(lines=self._one_line(received="10", rejected="0"))
        self.client.post(reverse("inventory:gr_post", args=[first.pk]))

        self.product.purchase_price = Decimal("20.00")
        self.product.save(update_fields=["purchase_price"])
        second = self._create_receipt(lines=self._one_line(received="10", rejected="0"))
        self.client.post(reverse("inventory:gr_post", args=[second.pk]))

        balance = StockBalance.objects.get(product=self.product, warehouse=self.warehouse)
        self.assertEqual(balance.quantity_on_hand, Decimal("20.0000"))
        self.assertEqual(balance.average_cost, Decimal("15.000000"))

    def test_only_draft_can_be_posted(self):
        receipt = self._create_receipt()
        self.client.post(reverse("inventory:gr_post", args=[receipt.pk]))
        receipt.refresh_from_db()  # the HTTP post above changed status in the DB
        with self.assertRaises(ValidationError):
            services.post_goods_receipt(receipt, self.clerk, request=None)

    def test_posted_receipt_cannot_be_edited(self):
        receipt = self._create_receipt()
        self.client.post(reverse("inventory:gr_post", args=[receipt.pk]))
        response = self.client.get(reverse("inventory:gr_edit", args=[receipt.pk]))
        self.assertRedirects(response, reverse("inventory:gr_detail", args=[receipt.pk]))

    # -- permissions (ACC-004) --------------------------------------------------
    def test_purchasing_cannot_post_a_receipt(self):
        from apps.core.permissions import PURCHASING

        receipt = self._create_receipt()
        buyer = User.objects.create_user(
            username="buyer2", email="buyer2@example.com", password="testpass-12345"
        )
        buyer.groups.add(Group.objects.get(name=PURCHASING))
        self.client.force_login(buyer)
        response = self.client.post(reverse("inventory:gr_post", args=[receipt.pk]))
        self.assertEqual(response.status_code, 403)

    # -- list (UX-002) -----------------------------------------------------------
    def test_list_renders(self):
        self._create_receipt()
        response = self.client.get(reverse("inventory:gr_list"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total_count"], 1)
