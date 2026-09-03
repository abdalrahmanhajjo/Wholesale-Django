"""
Purchase bill screens: duplicate vendor-invoice detection (PUR-006) and the
PUR-008 AP + tax posting.

BRD coverage: PUR-005..PUR-008, BR-006, BR-010..BR-012, ACC-004, ACC-005,
CFG-007, GL-001, GL-002, GL-010, GL-011.

Note: these tests need a real PostgreSQL test database (NFR-002) —
`python manage.py test apps.purchases`.
"""

from datetime import date
from decimal import Decimal

from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.catalog.models import Product, UnitOfMeasure
from apps.core.models import Currency, DocumentStatus, TaxCode
from apps.core.permissions import ACCOUNTANT, OWNER_ADMIN, PURCHASING
from apps.inventory import services as inventory_services
from apps.inventory.models import GoodsReceipt, GoodsReceiptLine, Warehouse
from apps.ledger.models import Account
from apps.parties.models import Vendor
from apps.purchases.models import PurchaseBill


class PurchaseBillScreenTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.usd = Currency.objects.get(code="USD")
        cls.warehouse = Warehouse.objects.get(code="MAIN")
        cls.unit = UnitOfMeasure.objects.get(code="EA")
        cls.tax = TaxCode.objects.get(code="VAT-P11")  # 11%, purchase-side
        cls.vendor = Vendor.objects.create(
            code="V-0002", name="Acme Supplies", currency=cls.usd
        )
        cls.product = Product.objects.create(
            sku="SKU-2", name="Widget", unit=cls.unit, purchase_price=Decimal("10.00")
        )

    def setUp(self):
        self.clerk = User.objects.create_user(
            username="pbuyer", email="pbuyer@example.com", password="testpass-12345"
        )
        self.clerk.groups.add(Group.objects.get(name=PURCHASING))
        self.accountant = User.objects.create_user(
            username="accountant", email="accountant@example.com", password="testpass-12345"
        )
        self.accountant.groups.add(Group.objects.get(name=ACCOUNTANT))
        self.owner = User.objects.create_user(
            username="owner3", email="owner3@example.com", password="testpass-12345"
        )
        self.owner.groups.add(Group.objects.get(name=OWNER_ADMIN))
        self.client.force_login(self.clerk)

    # -- helpers ----------------------------------------------------------------
    def _header(self, **overrides):
        data = {
            "vendor": self.vendor.pk,
            "purchase_order": "",
            "goods_receipt": "",
            "warehouse": self.warehouse.pk,
            "vendor_invoice_number": "INV-1001",
            "vendor_invoice_date": "2026-08-30",
            "document_date": "2026-08-31",
            "due_date": "",
            "currency": self.usd.pk,
            "exchange_rate": "1",
            "payment_term": "",
            "billing_address_text": "",
            "document_discount_kind": "NONE",
            "document_discount_value": "0",
            "notes": "",
        }
        data.update(overrides)
        return data

    def _one_line(self, **overrides):
        data = {
            "lines-TOTAL_FORMS": "1",
            "lines-INITIAL_FORMS": "0",
            "lines-MIN_NUM_FORMS": "0",
            "lines-MAX_NUM_FORMS": "1000",
            "lines-0-purchase_order_line": "",
            "lines-0-receipt_line": "",
            "lines-0-is_stock_line": "on",
            "lines-0-product": self.product.pk,
            "lines-0-expense_account": "",
            "lines-0-description": "",
            "lines-0-unit": self.unit.pk,
            "lines-0-warehouse": "",
            "lines-0-tax_code": self.tax.pk,
            "lines-0-quantity": "10",
            "lines-0-unit_price": "10.00",
            "lines-0-discount_percent": "0",
        }
        data.update(overrides)
        return data

    def _create_bill(self, header=None, lines=None):
        data = {**self._header(**(header or {})), **(lines or self._one_line())}
        response = self.client.post(reverse("purchases:bill_create"), data)
        self.assertEqual(response.status_code, 302, getattr(response, "context", None))
        return PurchaseBill.objects.latest("id")

    # -- create and duplicate detection (PUR-005, PUR-006) ------------------------
    def test_create_allocates_a_sequential_number(self):
        first = self._create_bill()
        second = self._create_bill(header={"vendor_invoice_number": "INV-1002"})
        self.assertEqual(first.number, "BILL-00001")
        self.assertEqual(second.number, "BILL-00002")
        self.assertEqual(first.status, DocumentStatus.DRAFT)

    def test_duplicate_vendor_invoice_number_is_rejected(self):
        self._create_bill(header={"vendor_invoice_number": "INV-DUP"})
        data = {
            **self._header(vendor_invoice_number="INV-DUP"),
            **self._one_line(),
        }
        response = self.client.post(reverse("purchases:bill_create"), data)
        self.assertEqual(response.status_code, 200)  # redisplayed, not saved
        self.assertEqual(PurchaseBill.objects.count(), 1)
        self.assertContains(response, "already billed")

    def test_line_and_header_totals_follow_the_arithmetic_contract(self):
        bill = self._create_bill()
        line = bill.lines.get()
        self.assertEqual(line.taxable_base_txn, Decimal("100.0000"))
        self.assertEqual(line.tax_txn, Decimal("11.0000"))
        self.assertEqual(line.total_txn, Decimal("111.0000"))
        self.assertEqual(bill.total_txn, Decimal("111.0000"))

    def test_a_bill_cannot_be_saved_with_zero_lines(self):
        data = {**self._header(), **self._one_line()}
        data["lines-0-product"] = ""
        data["lines-0-is_stock_line"] = ""
        data["lines-0-tax_code"] = ""
        data["lines-0-unit"] = ""
        data["lines-0-quantity"] = ""
        data["lines-0-unit_price"] = ""
        response = self.client.post(reverse("purchases:bill_create"), data)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(PurchaseBill.objects.exists())

    # -- posting (PUR-008, BR-006, GL-002) -----------------------------------------
    def test_post_creates_a_balanced_ap_and_tax_journal(self):
        bill = self._create_bill()
        self.client.force_login(self.accountant)
        response = self.client.post(reverse("purchases:bill_post", args=[bill.pk]))
        self.assertRedirects(response, reverse("purchases:bill_detail", args=[bill.pk]))

        bill.refresh_from_db()
        self.assertEqual(bill.status, DocumentStatus.POSTED)
        self.assertEqual(bill.open_txn, bill.total_txn)

        entry = bill.journal_entry
        self.assertEqual(entry.total_debit_base, entry.total_credit_base)
        self.assertEqual(entry.total_debit_base, Decimal("111.0000"))

        ap_account = Account.objects.get(code="2110")
        ap_line = entry.lines.get(account=ap_account)
        self.assertEqual(ap_line.credit_base, Decimal("111.0000"))
        self.assertEqual(ap_line.vendor, self.vendor)

        inventory_account = Account.objects.get(code="1310")
        inventory_line = entry.lines.get(account=inventory_account)
        self.assertEqual(inventory_line.debit_base, Decimal("100.0000"))

        input_tax_account = Account.objects.get(code="1410")
        tax_line = entry.lines.get(account=input_tax_account)
        self.assertEqual(tax_line.debit_base, Decimal("11.0000"))

    def test_posting_clears_grni_instead_of_inventory_when_receipted(self):
        receipt = GoodsReceipt.objects.create(
            number="GRN-TEST",
            vendor=self.vendor,
            warehouse=self.warehouse,
            document_date=date(2026, 8, 31),
        )
        receipt_line = GoodsReceiptLine.objects.create(
            receipt=receipt,
            line_no=1,
            product=self.product,
            unit=self.unit,
            quantity_received=Decimal("10"),
            quantity_accepted=Decimal("10"),
        )
        inventory_services.recalculate_receipt(receipt)
        inventory_services.post_goods_receipt(receipt, self.owner, request=None)

        bill = self._create_bill(
            lines=self._one_line(**{"lines-0-receipt_line": receipt_line.pk})
        )
        self.client.force_login(self.accountant)
        self.client.post(reverse("purchases:bill_post", args=[bill.pk]))

        bill.refresh_from_db()
        grni_account = Account.objects.get(code="2150")
        self.assertTrue(bill.journal_entry.lines.filter(account=grni_account).exists())
        inventory_account = Account.objects.get(code="1310")
        self.assertFalse(bill.journal_entry.lines.filter(account=inventory_account).exists())

        receipt_line.refresh_from_db()
        self.assertEqual(receipt_line.quantity_billed, Decimal("10"))

    # -- permissions (ACC-004) -----------------------------------------------------
    def test_purchasing_cannot_post_a_bill(self):
        bill = self._create_bill()
        response = self.client.post(reverse("purchases:bill_post", args=[bill.pk]))
        self.assertEqual(response.status_code, 403)

    def test_only_draft_bill_can_be_edited(self):
        bill = self._create_bill()
        self.client.force_login(self.accountant)
        self.client.post(reverse("purchases:bill_post", args=[bill.pk]))

        # Accountant can post a bill but isn't granted purchases.change_purchasebill
        # (that's Purchasing's CRUD, per the role matrix) — Owner/Admin has both,
        # so it isolates the lock check from a permission check, same as the
        # equivalent purchase-order test.
        self.client.force_login(self.owner)
        response = self.client.get(reverse("purchases:bill_edit", args=[bill.pk]))
        self.assertRedirects(response, reverse("purchases:bill_detail", args=[bill.pk]))

    # -- list (UX-002) ----------------------------------------------------------------
    def test_list_renders(self):
        self._create_bill()
        response = self.client.get(reverse("purchases:bill_list"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total_count"], 1)
