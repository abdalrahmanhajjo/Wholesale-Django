"""
Purchase order screens and the PUR-002 approval workflow.

BRD coverage: PUR-001, PUR-002, BR-003, BR-010, BR-011, BR-012, ACC-004,
ACC-005, CFG-008, CFG-010, NFR-008.

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
from apps.core.models import AuditAction, AuditEvent, Currency, DocumentStatus, TaxCode
from apps.core.permissions import OWNER_ADMIN, PURCHASING
from apps.inventory.models import Warehouse
from apps.parties.models import Vendor
from apps.purchases import services
from apps.purchases.models import PurchaseOrder


class PurchaseOrderScreenTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.usd = Currency.objects.get(code="USD")
        cls.warehouse = Warehouse.objects.get(code="MAIN")
        cls.unit = UnitOfMeasure.objects.get(code="EA")
        cls.tax = TaxCode.objects.get(code="VAT-P11")  # 11%, purchase-side
        cls.vendor = Vendor.objects.create(
            code="V-0001", name="Acme Supplies", currency=cls.usd
        )
        cls.product = Product.objects.create(
            sku="SKU-1", name="Widget", unit=cls.unit, purchase_price=Decimal("10.00")
        )

    def setUp(self):
        self.buyer = User.objects.create_user(
            username="buyer", email="buyer@example.com", password="testpass-12345"
        )
        self.buyer.groups.add(Group.objects.get(name=PURCHASING))
        self.owner = User.objects.create_user(
            username="owner", email="owner@example.com", password="testpass-12345"
        )
        self.owner.groups.add(Group.objects.get(name=OWNER_ADMIN))
        self.client.force_login(self.buyer)

    # -- helpers -------------------------------------------------------------
    def _header(self, **overrides):
        data = {
            "vendor": self.vendor.pk,
            "warehouse": self.warehouse.pk,
            "document_date": "2026-08-31",
            "expected_date": "",
            "due_date": "",
            "currency": self.usd.pk,
            "exchange_rate": "1",
            "payment_term": "",
            "buyer": "",
            "vendor_reference": "",
            "delivery_address_text": "",
            "document_discount_kind": "NONE",
            "document_discount_value": "0",
            "notes": "",
        }
        data.update(overrides)
        return data

    def _one_line(self):
        return {
            "lines-TOTAL_FORMS": "1",
            "lines-INITIAL_FORMS": "0",
            "lines-MIN_NUM_FORMS": "0",
            "lines-MAX_NUM_FORMS": "1000",
            "lines-0-product": self.product.pk,
            "lines-0-description": "",
            "lines-0-unit": self.unit.pk,
            "lines-0-warehouse": "",
            "lines-0-tax_code": self.tax.pk,
            "lines-0-quantity": "10",
            "lines-0-unit_price": "10.00",
            "lines-0-discount_percent": "10",
        }

    def _create_order(self, header=None, lines=None):
        data = {**self._header(**(header or {})), **(lines or self._one_line())}
        response = self.client.post(reverse("purchases:po_create"), data)
        self.assertEqual(response.status_code, 302, getattr(response, "context", None))
        return PurchaseOrder.objects.latest("id")

    # -- create and totals (PUR-001, BR-010, BR-011, BR-012) -----------------
    def test_create_allocates_a_sequential_number(self):
        first = self._create_order()
        second = self._create_order()
        self.assertEqual(first.number, "PO-00001")
        self.assertEqual(second.number, "PO-00002")
        self.assertEqual(first.status, DocumentStatus.DRAFT)

    def test_line_and_header_totals_follow_the_arithmetic_contract(self):
        order = self._create_order()
        line = order.lines.get()

        self.assertEqual(line.gross_txn, Decimal("100.0000"))
        self.assertEqual(line.line_discount_txn, Decimal("10.0000"))
        self.assertEqual(line.net_txn, Decimal("90.0000"))
        self.assertEqual(line.tax_txn, Decimal("9.9000"))
        self.assertEqual(line.total_txn, Decimal("99.9000"))

        self.assertEqual(order.subtotal_txn, Decimal("100.0000"))
        self.assertEqual(order.tax_txn, Decimal("9.9000"))
        self.assertEqual(order.total_txn, Decimal("99.9000"))

    def test_header_discount_is_allocated_across_lines_and_foots_exactly(self):
        lines = {
            "lines-TOTAL_FORMS": "2",
            "lines-INITIAL_FORMS": "0",
            "lines-MIN_NUM_FORMS": "0",
            "lines-MAX_NUM_FORMS": "1000",
            "lines-0-product": self.product.pk,
            "lines-0-description": "",
            "lines-0-unit": self.unit.pk,
            "lines-0-warehouse": "",
            "lines-0-tax_code": self.tax.pk,
            "lines-0-quantity": "10",
            "lines-0-unit_price": "10.00",
            "lines-0-discount_percent": "10",
            "lines-1-product": self.product.pk,
            "lines-1-description": "",
            "lines-1-unit": self.unit.pk,
            "lines-1-warehouse": "",
            "lines-1-tax_code": self.tax.pk,
            "lines-1-quantity": "5",
            "lines-1-unit_price": "20.00",
            "lines-1-discount_percent": "0",
        }
        order = self._create_order(
            header={"document_discount_kind": "AMOUNT", "document_discount_value": "19.00"},
            lines=lines,
        )
        first, second = order.lines.order_by("line_no")

        self.assertEqual(first.allocated_document_discount_txn, Decimal("9.0000"))
        self.assertEqual(second.allocated_document_discount_txn, Decimal("10.0000"))
        # The two shares must foot to the header discount exactly (BR-011).
        self.assertEqual(
            first.allocated_document_discount_txn + second.allocated_document_discount_txn,
            order.document_discount_txn,
        )
        self.assertEqual(order.total_txn, Decimal("189.8100"))

    def test_an_order_cannot_be_saved_with_zero_lines(self):
        data = {**self._header(), **self._one_line()}
        data["lines-0-product"] = ""
        data["lines-0-tax_code"] = ""
        data["lines-0-unit"] = ""
        data["lines-0-quantity"] = ""
        data["lines-0-unit_price"] = ""
        response = self.client.post(reverse("purchases:po_create"), data)
        self.assertEqual(response.status_code, 200)  # redisplayed, not saved
        self.assertFalse(PurchaseOrder.objects.exists())

    # -- approval workflow (PUR-002) -----------------------------------------
    def test_submit_requires_at_least_one_line(self):
        order = PurchaseOrder.objects.create(
            number="PO-TEST",
            vendor=self.vendor,
            warehouse=self.warehouse,
            document_date="2026-08-31",
            posting_date="2026-08-31",
            currency=self.usd,
        )
        with self.assertRaises(ValidationError):
            services.submit_purchase_order(order, self.buyer, request=None)

    def test_full_lifecycle_submit_approve(self):
        order = self._create_order()

        response = self.client.post(reverse("purchases:po_submit", args=[order.pk]))
        self.assertRedirects(response, reverse("purchases:po_detail", args=[order.pk]))
        order.refresh_from_db()
        self.assertEqual(order.status, DocumentStatus.SUBMITTED)
        self.assertIsNotNone(order.submitted_at)

        # Purchasing may submit but not sign off — separation of duties.
        response = self.client.post(reverse("purchases:po_approve", args=[order.pk]))
        self.assertEqual(response.status_code, 403)

        self.client.force_login(self.owner)
        response = self.client.post(reverse("purchases:po_approve", args=[order.pk]))
        self.assertRedirects(response, reverse("purchases:po_detail", args=[order.pk]))
        order.refresh_from_db()
        self.assertEqual(order.status, DocumentStatus.APPROVED)
        self.assertEqual(order.approved_by, self.owner)

        event = AuditEvent.objects.filter(
            object_id=order.pk, action=AuditAction.APPROVE
        ).latest("occurred_at")
        self.assertEqual(event.user, self.owner)

    def test_reject_requires_a_reason(self):
        order = self._create_order()
        self.client.post(reverse("purchases:po_submit", args=[order.pk]))

        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("purchases:po_reject", args=[order.pk]), {"reason": ""}
        )
        self.assertRedirects(response, reverse("purchases:po_detail", args=[order.pk]))
        order.refresh_from_db()
        self.assertEqual(order.status, DocumentStatus.SUBMITTED)  # unchanged

        response = self.client.post(
            reverse("purchases:po_reject", args=[order.pk]),
            {"reason": "Price above the vendor's last quote."},
        )
        order.refresh_from_db()
        self.assertEqual(order.status, DocumentStatus.REJECTED)
        self.assertEqual(order.approval_reason, "Price above the vendor's last quote.")

    def test_rejected_order_can_be_resubmitted(self):
        order = self._create_order()
        self.client.post(reverse("purchases:po_submit", args=[order.pk]))
        self.client.force_login(self.owner)
        self.client.post(reverse("purchases:po_reject", args=[order.pk]), {"reason": "No."})

        self.client.force_login(self.buyer)
        response = self.client.post(reverse("purchases:po_submit", args=[order.pk]))
        self.assertRedirects(response, reverse("purchases:po_detail", args=[order.pk]))
        order.refresh_from_db()
        self.assertEqual(order.status, DocumentStatus.SUBMITTED)

    def test_approve_only_from_submitted(self):
        order = self._create_order()  # still DRAFT
        with self.assertRaises(ValidationError):
            services.approve_purchase_order(order, self.owner, request=None)

    # -- editing is locked once approved (BR-004) ----------------------------
    def test_approved_order_cannot_be_edited(self):
        order = self._create_order()
        self.client.post(reverse("purchases:po_submit", args=[order.pk]))
        self.client.force_login(self.owner)
        self.client.post(reverse("purchases:po_approve", args=[order.pk]))

        response = self.client.get(reverse("purchases:po_edit", args=[order.pk]))
        self.assertRedirects(response, reverse("purchases:po_detail", args=[order.pk]))

    # -- permissions (ACC-004) ------------------------------------------------
    def test_direct_url_access_to_approve_is_denied_without_permission(self):
        order = self._create_order()
        self.client.post(reverse("purchases:po_submit", args=[order.pk]))
        response = self.client.post(reverse("purchases:po_approve", args=[order.pk]))
        self.assertEqual(response.status_code, 403)

    def test_read_only_user_cannot_create(self):
        auditor = User.objects.create_user(
            username="ro",
            email="ro@example.com",
            password="testpass-12345",
            is_read_only=True,
        )
        auditor.groups.add(Group.objects.get(name=OWNER_ADMIN))
        self.client.force_login(auditor)
        response = self.client.post(reverse("purchases:po_create"), {})
        self.assertEqual(response.status_code, 403)

    # -- list (UX-002) ---------------------------------------------------------
    def test_list_renders(self):
        self._create_order()
        response = self.client.get(reverse("purchases:po_list"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total_count"], 1)


class ProductPayloadEmbeddingTests(TestCase):
    """The product map is embedded in a <script> tag, so how it is escaped matters.

    The sales app renders its equivalent map through ``json_script`` and that
    map already carries a product *name*. This one carried only numbers, but it
    was rendered with ``|safe``, so the day anybody added a name to it the page
    would have become injectable. These pin both halves: the browser contract
    the inline script depends on, and the escaping.
    """

    @classmethod
    def setUpTestData(cls):
        cls.unit = UnitOfMeasure.objects.get(code="EA")
        Product.objects.create(
            sku="SKU-EMBED",
            # If this name ever reaches the payload, json_script is what stops
            # it closing the tag. Harmless while the map is numeric-only.
            name="Widget </script><script>alert(1)</script>",
            unit=cls.unit,
            purchase_price=Decimal("10.00"),
        )

    def setUp(self):
        self.buyer = User.objects.create_user(
            username="embed-buyer", email="embed@example.com", password="testpass-12345"
        )
        self.buyer.groups.add(Group.objects.get(name=PURCHASING))
        self.client.force_login(self.buyer)

    def test_the_inline_script_can_still_find_its_data(self):
        """json_script emits the id itself; the JS looks the element up by it."""
        response = self.client.get(reverse("purchases:po_create"))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn('id="po-products-data"', body)
        self.assertIn('type="application/json"', body)

    def test_no_unescaped_closing_script_tag_reaches_the_page(self):
        response = self.client.get(reverse("purchases:po_create"))
        body = response.content.decode()
        payload_start = body.index('id="po-products-data"')
        payload = body[payload_start : body.index("</script>", payload_start)]
        self.assertNotIn("<script", payload)
