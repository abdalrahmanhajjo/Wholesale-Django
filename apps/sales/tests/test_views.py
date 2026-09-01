"""
Sales-order view tests (SAL-004, ACC-004, ACC-005).

ACC-004's evidence is that a user lacking a permission is denied even when
calling the URL directly. So these call the submit/approve/reject URLs via the
test client as users with and without the right permissions.

Run:  python manage.py test apps.sales.tests.test_views
"""

from django.contrib.auth.models import Group, Permission
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.core.models import DocumentStatus
from apps.core.permissions import APPROVE_SALES_ORDER, OWNER_ADMIN, SALES
from apps.sales import services
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
