"""
Purchase returns and vendor debit notes (RET-005..RET-008, BR-015, BR-017).

BRD coverage: RET-005..RET-008, BR-006, BR-010..BR-012, BR-015, BR-017,
ACC-004, ACC-005, CFG-007, GL-001, GL-002, GL-010, GL-011.

Note: these tests need a real PostgreSQL test database (NFR-002) —
`python manage.py test apps.purchases`.
"""

from decimal import Decimal

from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.catalog.models import Product, UnitOfMeasure
from apps.core.models import Currency, DocumentStatus, TaxCode
from apps.core.permissions import ACCOUNTANT, OWNER_ADMIN, PURCHASING
from apps.inventory import services as inventory_services
from apps.inventory.models import StockBalance, Warehouse
from apps.ledger.models import Account
from apps.parties.models import Vendor
from apps.purchases import services
from apps.purchases.models import PurchaseReturn, VendorDebitNote


class PurchaseReturnAndDebitNoteTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.usd = Currency.objects.get(code="USD")
        cls.warehouse = Warehouse.objects.get(code="MAIN")
        cls.unit = UnitOfMeasure.objects.get(code="EA")
        cls.tax = TaxCode.objects.get(code="VAT-P11")  # 11%, purchase-side
        cls.vendor = Vendor.objects.create(
            code="V-0003", name="Returns Test Vendor", currency=cls.usd
        )
        cls.product = Product.objects.create(
            sku="SKU-RET-1",
            name="Returnable Widget",
            unit=cls.unit,
            purchase_price=Decimal("10.00"),
        )

    def setUp(self):
        self.clerk = User.objects.create_user(
            username="retclerk", email="retclerk@example.com", password="testpass-12345"
        )
        self.clerk.groups.add(Group.objects.get(name=PURCHASING))
        self.accountant = User.objects.create_user(
            username="retaccountant",
            email="retaccountant@example.com",
            password="testpass-12345",
        )
        self.accountant.groups.add(Group.objects.get(name=ACCOUNTANT))
        self.owner = User.objects.create_user(
            username="retowner", email="retowner@example.com", password="testpass-12345"
        )
        self.owner.groups.add(Group.objects.get(name=OWNER_ADMIN))
        self.client.force_login(self.clerk)

        # post_purchase_bill only ever books the AP/tax journal — it never
        # touches StockBalance (only a GoodsReceipt does that) — so a bill
        # entered without one, as here, leaves nothing on hand to return.
        # Seed the balance directly through the shared costing engine, same
        # as apps/inventory's own test setup.
        inventory_services.post_stock_movement(
            product=self.product,
            warehouse=self.warehouse,
            movement_date="2026-08-31",
            movement_type="GOODS_RECEIPT",
            quantity=Decimal("10"),
            unit_cost=Decimal("10.00"),
            source=self.product,
            source_doc_type="",
            source_doc_number="",
            idempotency_key="SEED-RET-1",
            user=self.clerk,
        )

        # A posted bill (no receipt) gives us a bill line to return against,
        # mirroring test_purchase_bills.py's own setup.
        self.bill = self._create_bill()
        self.client.force_login(self.accountant)
        self.client.post(reverse("purchases:bill_post", args=[self.bill.pk]))
        self.bill.refresh_from_db()
        self.bill_line = self.bill.lines.get()
        self.client.force_login(self.clerk)

    # -- bill helpers (mirrors test_purchase_bills.py) -------------------------
    def _bill_header(self, **overrides):
        data = {
            "vendor": self.vendor.pk,
            "purchase_order": "",
            "goods_receipt": "",
            "warehouse": self.warehouse.pk,
            "vendor_invoice_number": "INV-RET-1001",
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

    def _bill_one_line(self, **overrides):
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

    def _create_bill(self):
        from apps.purchases.models import PurchaseBill

        data = {**self._bill_header(), **self._bill_one_line()}
        response = self.client.post(reverse("purchases:bill_create"), data)
        self.assertEqual(response.status_code, 302, getattr(response, "context", None))
        return PurchaseBill.objects.latest("id")

    # -- purchase return helpers ------------------------------------------------
    def _return_header(self, **overrides):
        data = {
            "vendor": self.vendor.pk,
            "warehouse": self.warehouse.pk,
            "original_bill": self.bill.pk,
            "original_receipt": "",
            "document_date": "2026-09-01",
            "reason": "Vendor sent the wrong colour.",
        }
        data.update(overrides)
        return data

    def _return_one_line(self, quantity="4", disposition="RESTOCK", **overrides):
        data = {
            "lines-TOTAL_FORMS": "1",
            "lines-INITIAL_FORMS": "0",
            "lines-MIN_NUM_FORMS": "0",
            "lines-MAX_NUM_FORMS": "1000",
            "lines-0-bill_line": self.bill_line.pk,
            "lines-0-receipt_line": "",
            "lines-0-product": self.product.pk,
            "lines-0-quantity": quantity,
            "lines-0-disposition": disposition,
            "lines-0-note": "",
        }
        data.update(overrides)
        return data

    def _create_return(self, header=None, lines=None):
        data = {**self._return_header(**(header or {})), **(lines or self._return_one_line())}
        response = self.client.post(reverse("purchases:pr_create"), data)
        self.assertEqual(response.status_code, 302, getattr(response, "context", None))
        return PurchaseReturn.objects.latest("id")

    # -- create (RET-005, RET-008) ------------------------------------------------
    def test_create_allocates_a_sequential_number_and_requires_a_reason(self):
        purchase_return = self._create_return()
        self.assertTrue(purchase_return.number.startswith("PRT-"))
        self.assertEqual(purchase_return.status, DocumentStatus.DRAFT)

        data = {**self._return_header(reason=""), **self._return_one_line()}
        response = self.client.post(reverse("purchases:pr_create"), data)
        self.assertEqual(response.status_code, 200)

    # -- posting moves stock (RET-005, INV-005) ------------------------------------
    def test_post_ships_restocked_lines_back_out_at_the_current_average(self):
        purchase_return = self._create_return(lines=self._return_one_line(quantity="4"))
        self.client.force_login(self.accountant)
        response = self.client.post(reverse("purchases:pr_post", args=[purchase_return.pk]))
        self.assertRedirects(
            response, reverse("purchases:pr_detail", args=[purchase_return.pk])
        )

        balance = StockBalance.objects.get(product=self.product, warehouse=self.warehouse)
        self.assertEqual(
            balance.quantity_on_hand, Decimal("6.0000")
        )  # 10 received - 4 returned

        purchase_return.refresh_from_db()
        self.assertEqual(purchase_return.status, DocumentStatus.POSTED)
        self.assertIsNone(purchase_return.journal_entry)  # money follows on the debit note
        line = purchase_return.lines.get()
        self.assertEqual(line.unit_cost, Decimal("10.000000"))
        self.assertEqual(line.total_cost, Decimal("40.0000"))

        self.bill_line.refresh_from_db()
        self.assertEqual(self.bill_line.quantity_returned, Decimal("4.0000"))

    def test_no_stock_effect_line_does_not_move_stock(self):
        purchase_return = self._create_return(
            lines=self._return_one_line(quantity="4", disposition="NO_STOCK_EFFECT")
        )
        self.client.force_login(self.accountant)
        self.client.post(reverse("purchases:pr_post", args=[purchase_return.pk]))

        balance = StockBalance.objects.get(product=self.product, warehouse=self.warehouse)
        self.assertEqual(balance.quantity_on_hand, Decimal("10.0000"))  # untouched

    def test_cannot_return_more_than_was_billed(self):
        purchase_return = self._create_return(lines=self._return_one_line(quantity="999"))
        with self.assertRaises(ValidationError):
            services.post_purchase_return(purchase_return, self.accountant, request=None)

    def test_purchasing_cannot_post_a_return(self):
        purchase_return = self._create_return()
        response = self.client.post(reverse("purchases:pr_post", args=[purchase_return.pk]))
        self.assertEqual(response.status_code, 403)

    # -- vendor debit note (RET-006, RET-007) --------------------------------------
    def _debit_note_header(self, **overrides):
        data = {
            "vendor": self.vendor.pk,
            "original_bill": self.bill.pk,
            "purchase_return": "",
            "vendor_credit_reference": "",
            "document_date": "2026-09-02",
            "currency": self.usd.pk,
            "exchange_rate": "1",
            "reason": "Credit for returned goods.",
            "notes": "",
        }
        data.update(overrides)
        return data

    def _debit_note_one_line(self, **overrides):
        data = {
            "lines-TOTAL_FORMS": "1",
            "lines-INITIAL_FORMS": "0",
            "lines-MIN_NUM_FORMS": "0",
            "lines-MAX_NUM_FORMS": "1000",
            "lines-0-bill_line": self.bill_line.pk,
            "lines-0-return_line": "",
            "lines-0-is_stock_line": "on",
            "lines-0-product": self.product.pk,
            "lines-0-expense_account": "",
            "lines-0-description": "",
            "lines-0-unit": self.unit.pk,
            "lines-0-tax_code": self.tax.pk,
            "lines-0-quantity": "4",
            "lines-0-unit_price": "10.00",
            "lines-0-discount_percent": "0",
        }
        data.update(overrides)
        return data

    def _create_debit_note(self, header=None, lines=None):
        data = {
            **self._debit_note_header(**(header or {})),
            **(lines or self._debit_note_one_line()),
        }
        response = self.client.post(reverse("purchases:dbn_create"), data)
        self.assertEqual(response.status_code, 302, getattr(response, "context", None))
        return VendorDebitNote.objects.latest("id")

    def test_post_books_a_balanced_ap_inventory_and_tax_reversal(self):
        note = self._create_debit_note()
        self.client.force_login(self.accountant)
        response = self.client.post(reverse("purchases:dbn_post", args=[note.pk]))
        self.assertRedirects(response, reverse("purchases:dbn_detail", args=[note.pk]))

        note.refresh_from_db()
        self.assertEqual(note.status, DocumentStatus.POSTED)
        self.assertEqual(note.open_txn, note.total_txn)

        entry = note.journal_entry
        self.assertEqual(entry.total_debit_base, entry.total_credit_base)
        self.assertEqual(entry.total_debit_base, Decimal("44.4000"))  # 40 + 11% tax

        ap_account = Account.objects.get(code="2110")
        ap_line = entry.lines.get(account=ap_account)
        self.assertEqual(ap_line.debit_base, Decimal("44.4000"))
        self.assertEqual(ap_line.vendor, self.vendor)

        inventory_account = Account.objects.get(code="1310")
        inventory_line = entry.lines.get(account=inventory_account)
        self.assertEqual(inventory_line.credit_base, Decimal("40.0000"))

        input_tax_account = Account.objects.get(code="1410")
        tax_line = entry.lines.get(account=input_tax_account)
        self.assertEqual(tax_line.credit_base, Decimal("4.4000"))

        self.bill_line.refresh_from_db()
        self.assertEqual(self.bill_line.quantity_returned, Decimal("4.0000"))

    def test_non_stock_line_credits_purchase_returns_not_purchase_expense(self):
        note = self._create_debit_note(
            lines=self._debit_note_one_line(
                **{
                    "lines-0-is_stock_line": "",
                    "lines-0-product": "",
                    "lines-0-bill_line": "",
                    "lines-0-tax_code": "",
                    "lines-0-description": "Freight credit",
                }
            )
        )
        self.client.force_login(self.accountant)
        self.client.post(reverse("purchases:dbn_post", args=[note.pk]))
        note.refresh_from_db()

        purchase_returns_account = Account.objects.get(code="5150")
        self.assertTrue(
            note.journal_entry.lines.filter(account=purchase_returns_account).exists()
        )

    def test_debit_note_from_a_posted_return_does_not_double_count_eligibility(self):
        purchase_return = self._create_return(lines=self._return_one_line(quantity="4"))
        self.client.force_login(self.accountant)
        self.client.post(reverse("purchases:pr_post", args=[purchase_return.pk]))
        self.bill_line.refresh_from_db()
        self.assertEqual(self.bill_line.quantity_returned, Decimal("4.0000"))

        return_line = purchase_return.lines.get()
        self.client.force_login(self.clerk)
        note = self._create_debit_note(
            header={"purchase_return": purchase_return.pk},
            lines=self._debit_note_one_line(**{"lines-0-return_line": return_line.pk}),
        )
        self.client.force_login(self.accountant)
        self.client.post(reverse("purchases:dbn_post", args=[note.pk]))

        self.bill_line.refresh_from_db()
        self.assertEqual(self.bill_line.quantity_returned, Decimal("4.0000"))  # unchanged

    def test_purchasing_cannot_post_a_debit_note(self):
        note = self._create_debit_note()
        response = self.client.post(reverse("purchases:dbn_post", args=[note.pk]))
        self.assertEqual(response.status_code, 403)

    # -- lists (UX-002) -----------------------------------------------------------
    def test_lists_render(self):
        self._create_return()
        self._create_debit_note()
        response = self.client.get(reverse("purchases:pr_list"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total_count"], 1)
        response = self.client.get(reverse("purchases:dbn_list"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total_count"], 1)
