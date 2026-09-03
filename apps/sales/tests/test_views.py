"""
Sales-order view tests (SAL-004, ACC-004, ACC-005).

ACC-004's evidence is that a user lacking a permission is denied even when
calling the URL directly. So these call the submit/approve/reject URLs via the
test client as users with and without the right permissions.

Run:  python manage.py test apps.sales.tests.test_views
"""

from decimal import Decimal

from django.contrib.auth.models import Group, Permission
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.core.models import DocumentStatus
from apps.sales import services
from apps.sales.models import SalesOrder
from apps.sales.tests import factories as f


def make_user(username, group_name=None, permissions=()):
    user = User.objects.create_user(
        username=username, email=f"{username}@example.com", password="x-1234567"
    )
    if group_name:
        user.groups.add(Group.objects.get(name=group_name))
    for codename in permissions:
        user.user_permissions.add(Permission.objects.get(codename=codename))
    user.save()
    return user


class ApproveRejectPermissionTests(TestCase):
    """ACC-004: approval is gated server-side on APPROVE_SALES_ORDER."""

    def setUp(self):
        self.approver = make_user(
            "approver", permissions=["approve_sales_order", "view_salesorder"]
        )
        self.salesperson = make_user("salesperson", permissions=["view_salesorder"])
        self.order = f.make_order()
        services.submit_order(self.order, self.salesperson)

    def test_approver_with_permission_is_allowed(self):
        self.client.force_login(self.approver)
        url = reverse("sales:so_approve", args=[self.order.pk])
        response = self.client.post(url, {"confirm": "yes", "reason": "OK"})
        self.assertIn(response.status_code, (302, 200))
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, DocumentStatus.APPROVED)

    def test_user_without_permission_gets_403(self):
        # The Sales role does not approve by default (BRD 4.1).
        self.client.force_login(self.salesperson)
        url = reverse("sales:so_approve", args=[self.order.pk])
        response = self.client.post(url, {"confirm": "yes", "reason": "OK"})
        self.assertEqual(response.status_code, 403)
        self.order.refresh_from_db()
        self.assertNotEqual(self.order.status, DocumentStatus.APPROVED)

    def test_approve_requires_confirmation_and_reason(self):
        # ConfirmationRequiredMixin (ACC-008) refuses an empty/missing reason.
        self.client.force_login(self.approver)
        url = reverse("sales:so_approve", args=[self.order.pk])
        response = self.client.post(url, {"confirm": "yes", "reason": ""})
        self.assertEqual(response.status_code, 403)
        self.order.refresh_from_db()
        self.assertNotEqual(self.order.status, DocumentStatus.APPROVED)


class SalesOrderEditTests(TestCase):
    """Editing an order adds new lines without hitting so_line_unique_no."""

    def setUp(self):
        self.editor = make_user(
            "editor",
            permissions=["change_salesorder", "view_salesorder"],
        )
        self.customer = f.make_customer()
        self.warehouse = f.make_warehouse()
        self.product_a = f.make_product(sku="P-EDIT-1", price=Decimal("100"))
        self.product_b = f.make_product(sku="P-EDIT-2", price=Decimal("250"))
        self.order = f.make_order(customer=self.customer, warehouse=self.warehouse)
        f.make_line(
            self.order,
            product=self.product_a,
            qty=Decimal("10"),
            price=Decimal("100"),
            line_no=1,
        )

    def _post_data(self):
        from apps.sales.models import DiscountKind

        first = self.order.lines.get(line_no=1)
        return {
            # header
            "customer": self.customer.pk,
            "warehouse": self.warehouse.pk,
            "currency": self.customer.currency.pk,
            "exchange_rate": "1.0000",
            "document_date": self.order.document_date.isoformat(),
            "posting_date": self.order.posting_date.isoformat(),
            "document_discount_kind": DiscountKind.NONE,
            "document_discount_value": "0.00",
            # lines management
            "lines-TOTAL_FORMS": "2",
            "lines-INITIAL_FORMS": "1",
            "lines-MIN_NUM_FORMS": "0",
            "lines-MAX_NUM_FORMS": "1000",
            # existing line
            "lines-0-id": first.pk,
            "lines-0-line_no": "1",
            "lines-0-product": first.product_id,
            "lines-0-unit": first.unit_id,
            "lines-0-quantity": str(first.quantity),
            "lines-0-unit_price": str(first.unit_price),
            "lines-0-discount_percent": "0.0000",
            "lines-0-warehouse": "",
            "lines-0-DELETE": "",
            # new line
            "lines-1-line_no": "",
            "lines-1-product": self.product_b.pk,
            "lines-1-unit": self.product_b.unit_id,
            "lines-1-quantity": "4",
            "lines-1-unit_price": "250.00",
            "lines-1-discount_percent": "0.0000",
            "lines-1-warehouse": "",
            "lines-1-DELETE": "",
        }

    def test_add_line_on_edit_keeps_line_numbers_distinct(self):
        self.client.force_login(self.editor)
        url = reverse("sales:so_edit", args=[self.order.pk])
        response = self.client.post(url, self._post_data())
        self.assertEqual(response.status_code, 302)

        self.order.refresh_from_db()
        self.assertEqual(self.order.lines.count(), 2)
        new_line = self.order.lines.get(product=self.product_b)
        self.assertEqual(new_line.line_no, 2)
        existing = self.order.lines.get(product=self.product_a)
        self.assertEqual(existing.line_no, 1)

    def test_edit_message_counts_line_change(self):
        self.client.force_login(self.editor)
        url = reverse("sales:so_edit", args=[self.order.pk])
        response = self.client.post(url, self._post_data(), follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f"{self.order.number} updated")
        self.assertContains(response, "1 line")


class SubmitViewTests(TestCase):
    def setUp(self):
        self.editor = make_user(
            "editor",
            permissions=["change_salesorder", "view_salesorder"],
        )
        self.order = f.make_order()

    def test_submit_moves_order_to_submitted(self):
        self.client.force_login(self.editor)
        url = reverse("sales:so_submit", args=[self.order.pk])
        response = self.client.post(url)
        self.assertIn(response.status_code, (302, 200))
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, DocumentStatus.SUBMITTED)

    def test_user_without_change_permission_gets_403(self):
        nobody = make_user("nobody", permissions=["view_salesorder"])
        self.client.force_login(nobody)
        url = reverse("sales:so_submit", args=[self.order.pk])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 403)


class DetailViewTests(TestCase):
    def test_detail_renders_order(self):
        user = make_user("viewer", permissions=["view_salesorder"])
        order = f.make_order()
        self.client.force_login(user)
        url = reverse("sales:so_detail", args=[order.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, order.number)

    def test_user_without_view_permission_gets_403(self):
        user = make_user("detail-no-access")
        order = f.make_order()
        self.client.force_login(user)
        response = self.client.get(reverse("sales:so_detail", args=[order.pk]))
        self.assertEqual(response.status_code, 403)


class ListViewTests(TestCase):
    def test_list_renders_summary_tiles(self):
        user = make_user("viewer", permissions=["view_salesorder"])
        self.client.force_login(user)
        response = self.client.get(reverse("sales:so_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Draft")

    def test_anonymous_redirected_to_login(self):
        response = self.client.get(reverse("sales:so_list"))
        self.assertEqual(response.status_code, 302)

    def test_user_without_view_permission_gets_403(self):
        self.client.force_login(make_user("list-no-access"))
        response = self.client.get(reverse("sales:so_list"))
        self.assertEqual(response.status_code, 403)


class SalesOrderEntryTests(TestCase):
    def setUp(self):
        self.editor = make_user(
            "order-editor",
            permissions=["add_salesorder", "change_salesorder", "view_salesorder"],
        )
        self.client.force_login(self.editor)
        self.customer = f.make_customer()
        self.warehouse = f.make_warehouse()
        self.product = f.make_product()
        self.sequence = f.make_sequence()

    def _post_data(self, *, quantity="1"):
        today = timezone.localdate().isoformat()
        return {
            "customer": self.customer.pk,
            "warehouse": self.warehouse.pk,
            "document_date": today,
            "posting_date": today,
            "due_date": "",
            "currency": self.customer.currency_id,
            "exchange_rate": "1",
            "payment_term": "",
            "expected_date": "",
            "customer_reference": "TEST-PO",
            "billing_address_text": "",
            "shipping_address_text": "",
            "salesperson": "",
            "document_discount_kind": "NONE",
            "document_discount_value": "0",
            "notes": "",
            "internal_notes": "",
            "lines-TOTAL_FORMS": "1",
            "lines-INITIAL_FORMS": "0",
            "lines-MIN_NUM_FORMS": "0",
            "lines-MAX_NUM_FORMS": "1000",
            "lines-0-line_no": "",
            "lines-0-product": self.product.pk,
            "lines-0-description": "",
            "lines-0-unit": self.product.unit_id,
            "lines-0-quantity": quantity,
            "lines-0-unit_price": "100",
            "lines-0-discount_percent": "0",
            "lines-0-tax_code": "",
            "lines-0-warehouse": "",
        }

    def test_create_screen_includes_bounded_add_line_control(self):
        response = self.client.get(reverse("sales:so_create"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="add-line"')
        self.assertContains(response, "__prefix__")

    def test_more_than_ten_submitted_line_forms_is_rejected(self):
        data = self._post_data()
        data["lines-TOTAL_FORMS"] = "11"

        response = self.client.post(reverse("sales:so_create"), data)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "at most 10 forms")
        self.assertFalse(SalesOrder.objects.filter(customer_reference="TEST-PO").exists())

    def test_invalid_lines_do_not_save_header_or_consume_number(self):
        next_number = self.sequence.next_number
        response = self.client.post(reverse("sales:so_create"), self._post_data(quantity="-1"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["line_formset"].errors)
        self.assertFalse(SalesOrder.objects.filter(customer_reference="TEST-PO").exists())
        self.sequence.refresh_from_db()
        self.assertEqual(self.sequence.next_number, next_number)

    def test_non_editable_order_cannot_be_opened_in_update_view(self):
        order = f.make_order(status=DocumentStatus.APPROVED)
        response = self.client.get(reverse("sales:so_edit", args=[order.pk]))
        self.assertEqual(response.status_code, 404)
