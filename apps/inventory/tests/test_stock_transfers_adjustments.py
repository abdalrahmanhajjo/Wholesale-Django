"""
Stock transfers and stock adjustments (INV-008, INV-009, BR-017).

BRD coverage: INV-008, INV-009, BR-017..BR-019, ACC-004, ACC-005, CFG-008,
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
from apps.core.models import DocumentStatus
from apps.core.permissions import OWNER_ADMIN, WAREHOUSE
from apps.inventory import services
from apps.inventory.models import (
    AdjustmentReason,
    StockAdjustment,
    StockBalance,
    StockTransfer,
    Warehouse,
)
from apps.ledger.models import Account

ZERO = Decimal("0")


class StockTransferTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.main = Warehouse.objects.get(code="MAIN")
        cls.second = Warehouse.objects.create(code="SECOND", name="Second Warehouse")
        cls.unit = UnitOfMeasure.objects.get(code="EA")
        cls.product = Product.objects.create(
            sku="SKU-TRF-1", name="Transferable Widget", unit=cls.unit
        )
        services.post_stock_movement(
            product=cls.product,
            warehouse=cls.main,
            movement_date="2026-08-31",
            movement_type="GOODS_RECEIPT",
            quantity=Decimal("20"),
            unit_cost=Decimal("10.00"),
            source=cls.product,
            source_doc_type="",
            source_doc_number="",
            idempotency_key="SEED-TRF-1",
            user=None,
        )

    def setUp(self):
        self.clerk = User.objects.create_user(
            username="trfclerk", email="trfclerk@example.com", password="testpass-12345"
        )
        self.clerk.groups.add(Group.objects.get(name=WAREHOUSE))
        self.client.force_login(self.clerk)

    def _create_transfer(self, quantity="5"):
        data = {
            "from_warehouse": self.main.pk,
            "to_warehouse": self.second.pk,
            "document_date": "2026-08-31",
            "reason": "Rebalancing",
            "notes": "",
            "lines-TOTAL_FORMS": "1",
            "lines-INITIAL_FORMS": "0",
            "lines-MIN_NUM_FORMS": "0",
            "lines-MAX_NUM_FORMS": "1000",
            "lines-0-product": self.product.pk,
            "lines-0-quantity": quantity,
        }
        response = self.client.post(reverse("inventory:st_create"), data)
        self.assertEqual(response.status_code, 302, getattr(response, "context", None))
        return StockTransfer.objects.latest("id")

    def test_create_allocates_a_sequential_number_and_estimates_cost(self):
        transfer = self._create_transfer()
        self.assertTrue(transfer.number.startswith("TRF-"))
        line = transfer.lines.get()
        self.assertEqual(line.unit_cost, Decimal("10.000000"))
        self.assertEqual(transfer.total_cost_base, Decimal("50.0000"))

    def test_same_warehouse_is_rejected_by_the_form(self):
        data = {
            "from_warehouse": self.main.pk,
            "to_warehouse": self.main.pk,
            "document_date": "2026-08-31",
            "reason": "",
            "notes": "",
            "lines-TOTAL_FORMS": "1",
            "lines-INITIAL_FORMS": "0",
            "lines-MIN_NUM_FORMS": "0",
            "lines-MAX_NUM_FORMS": "1000",
            "lines-0-product": self.product.pk,
            "lines-0-quantity": "1",
        }
        response = self.client.post(reverse("inventory:st_create"), data)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(StockTransfer.objects.exists())

    def test_post_moves_stock_and_carries_the_source_cost_forward(self):
        transfer = self._create_transfer(quantity="5")
        response = self.client.post(reverse("inventory:st_post", args=[transfer.pk]))
        self.assertRedirects(response, reverse("inventory:st_detail", args=[transfer.pk]))

        source_balance = StockBalance.objects.get(product=self.product, warehouse=self.main)
        dest_balance = StockBalance.objects.get(product=self.product, warehouse=self.second)
        self.assertEqual(source_balance.quantity_on_hand, Decimal("15.0000"))
        self.assertEqual(dest_balance.quantity_on_hand, Decimal("5.0000"))
        self.assertEqual(dest_balance.average_cost, Decimal("10.000000"))

        transfer.refresh_from_db()
        self.assertEqual(transfer.status, DocumentStatus.POSTED)

    def test_no_journal_when_warehouses_share_the_default_inventory_account(self):
        transfer = self._create_transfer(quantity="5")
        self.client.post(reverse("inventory:st_post", args=[transfer.pk]))
        transfer.refresh_from_db()
        self.assertIsNone(transfer.journal_entry)

    def test_journal_posted_when_warehouses_have_distinct_inventory_accounts(self):
        self.main.inventory_account = Account.objects.get(code="1310")
        self.main.save(update_fields=["inventory_account"])
        self.second.inventory_account = Account.objects.get(code="1510")
        self.second.save(update_fields=["inventory_account"])

        transfer = self._create_transfer(quantity="5")
        self.client.post(reverse("inventory:st_post", args=[transfer.pk]))
        transfer.refresh_from_db()

        self.assertIsNotNone(transfer.journal_entry)
        entry = transfer.journal_entry
        self.assertEqual(entry.total_debit_base, entry.total_credit_base)
        self.assertEqual(
            entry.total_debit_base, Decimal("100.0000")
        )  # 2 x 50 through clearing
        self.assertEqual(entry.lines.count(), 4)

    def test_cannot_transfer_more_than_is_on_hand(self):
        transfer = self._create_transfer(quantity="999")
        with self.assertRaises(ValidationError):
            services.post_stock_transfer(transfer, self.clerk, request=None)

    def test_purchasing_cannot_post_a_transfer(self):
        from apps.core.permissions import PURCHASING

        transfer = self._create_transfer()
        buyer = User.objects.create_user(
            username="buyer3", email="buyer3@example.com", password="testpass-12345"
        )
        buyer.groups.add(Group.objects.get(name=PURCHASING))
        self.client.force_login(buyer)
        response = self.client.post(reverse("inventory:st_post", args=[transfer.pk]))
        self.assertEqual(response.status_code, 403)


class StockAdjustmentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.warehouse = Warehouse.objects.get(code="MAIN")
        cls.unit = UnitOfMeasure.objects.get(code="EA")
        cls.product = Product.objects.create(
            sku="SKU-ADJ-1",
            name="Adjustable Widget",
            unit=cls.unit,
            purchase_price=Decimal("8.00"),
        )
        cls.count_up = AdjustmentReason.objects.get(code="COUNT-UP")
        cls.damage = AdjustmentReason.objects.get(code="DAMAGE")
        cls.no_approval_reason = AdjustmentReason.objects.create(
            code="TEST-NO-APPROVAL",
            name="Routine correction (test only)",
            increases_stock=True,
            gain_loss_account=Account.objects.get(code="4830"),
            requires_approval=False,
        )

    def setUp(self):
        self.clerk = User.objects.create_user(
            username="adjclerk", email="adjclerk@example.com", password="testpass-12345"
        )
        self.clerk.groups.add(Group.objects.get(name=WAREHOUSE))
        self.owner = User.objects.create_user(
            username="owner4", email="owner4@example.com", password="testpass-12345"
        )
        self.owner.groups.add(Group.objects.get(name=OWNER_ADMIN))
        self.client.force_login(self.clerk)

    def _create_adjustment(self, reason, delta, unit_cost=""):
        data = {
            "warehouse": self.warehouse.pk,
            "reason": reason.pk,
            "document_date": "2026-08-31",
            "narration": "",
            "attachment_reference": "",
            "lines-TOTAL_FORMS": "1",
            "lines-INITIAL_FORMS": "0",
            "lines-MIN_NUM_FORMS": "0",
            "lines-MAX_NUM_FORMS": "1000",
            "lines-0-product": self.product.pk,
            "lines-0-quantity_delta": delta,
            "lines-0-unit_cost": unit_cost,
            "lines-0-note": "",
        }
        response = self.client.post(reverse("inventory:sa_create"), data)
        self.assertEqual(response.status_code, 302, getattr(response, "context", None))
        return StockAdjustment.objects.latest("id")

    def test_create_allocates_a_sequential_number(self):
        adjustment = self._create_adjustment(self.count_up, "10", "9.00")
        self.assertTrue(adjustment.number.startswith("ADJ-"))
        self.assertEqual(adjustment.status, DocumentStatus.DRAFT)

    def test_direction_mismatch_is_rejected(self):
        # DAMAGE only ever decreases stock — a positive delta must be refused.
        data = {
            "warehouse": self.warehouse.pk,
            "reason": self.damage.pk,
            "document_date": "2026-08-31",
            "narration": "",
            "attachment_reference": "",
            "lines-TOTAL_FORMS": "1",
            "lines-INITIAL_FORMS": "0",
            "lines-MIN_NUM_FORMS": "0",
            "lines-MAX_NUM_FORMS": "1000",
            "lines-0-product": self.product.pk,
            "lines-0-quantity_delta": "5",
            "lines-0-unit_cost": "",
            "lines-0-note": "",
        }
        response = self.client.post(reverse("inventory:sa_create"), data, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(StockAdjustment.objects.exists())

    def test_full_approval_workflow_increases_stock_and_books_the_gain(self):
        adjustment = self._create_adjustment(self.count_up, "10", "9.00")

        self.client.post(reverse("inventory:sa_submit", args=[adjustment.pk]))
        adjustment.refresh_from_db()
        self.assertEqual(adjustment.status, DocumentStatus.SUBMITTED)

        self.client.force_login(self.owner)
        self.client.post(reverse("inventory:sa_approve", args=[adjustment.pk]))
        adjustment.refresh_from_db()
        self.assertEqual(adjustment.status, DocumentStatus.APPROVED)

        self.client.post(reverse("inventory:sa_post", args=[adjustment.pk]))
        adjustment.refresh_from_db()
        self.assertEqual(adjustment.status, DocumentStatus.POSTED)
        self.assertIsNotNone(adjustment.journal_entry)

        balance = StockBalance.objects.get(product=self.product, warehouse=self.warehouse)
        self.assertEqual(balance.quantity_on_hand, Decimal("10.0000"))
        self.assertEqual(balance.average_cost, Decimal("9.000000"))

        entry = adjustment.journal_entry
        self.assertEqual(entry.total_debit_base, entry.total_credit_base)
        self.assertEqual(entry.total_debit_base, Decimal("90.0000"))
        gain_line = entry.lines.get(credit_base__gt=0)
        self.assertEqual(gain_line.account.code, "4830")

    def test_reason_not_requiring_approval_skips_straight_to_approved(self):
        adjustment = self._create_adjustment(self.no_approval_reason, "3", "5.00")
        self.client.post(reverse("inventory:sa_submit", args=[adjustment.pk]))
        adjustment.refresh_from_db()
        self.assertEqual(adjustment.status, DocumentStatus.APPROVED)

    def test_rejecting_requires_a_reason(self):
        adjustment = self._create_adjustment(self.count_up, "10", "9.00")
        self.client.post(reverse("inventory:sa_submit", args=[adjustment.pk]))
        self.client.force_login(self.owner)
        self.client.post(reverse("inventory:sa_reject", args=[adjustment.pk]), {"reason": ""})
        adjustment.refresh_from_db()
        self.assertEqual(adjustment.status, DocumentStatus.SUBMITTED)

        self.client.post(
            reverse("inventory:sa_reject", args=[adjustment.pk]), {"reason": "Miscounted"}
        )
        adjustment.refresh_from_db()
        self.assertEqual(adjustment.status, DocumentStatus.REJECTED)

    def test_decrease_beyond_on_hand_is_blocked_at_posting(self):
        adjustment = self._create_adjustment(self.damage, "-999")
        self.client.force_login(self.owner)
        self.client.post(reverse("inventory:sa_approve", args=[adjustment.pk]))
        with self.assertRaises(ValidationError):
            services.post_stock_adjustment(adjustment, self.owner, request=None)

    def test_purchasing_cannot_approve_an_adjustment(self):
        from apps.core.permissions import PURCHASING

        adjustment = self._create_adjustment(self.count_up, "10", "9.00")
        self.client.post(reverse("inventory:sa_submit", args=[adjustment.pk]))
        buyer = User.objects.create_user(
            username="buyer4", email="buyer4@example.com", password="testpass-12345"
        )
        buyer.groups.add(Group.objects.get(name=PURCHASING))
        self.client.force_login(buyer)
        response = self.client.post(reverse("inventory:sa_approve", args=[adjustment.pk]))
        self.assertEqual(response.status_code, 403)
