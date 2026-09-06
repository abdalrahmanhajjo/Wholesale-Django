"""
Authorisation tests (ACC-001, ACC-003, ACC-004, ACC-006).

ACC-004's acceptance evidence is specific: "A user lacking a permission receives
a denial even when calling the URL directly." So these tests call views through
the URL, not by inspecting group membership — a test that only checks
`user.has_perm()` would pass even if every view forgot to check.

    python manage.py test apps.core
"""

from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied
from django.test import Client, RequestFactory, SimpleTestCase, TestCase
from django.urls import reverse
from django.views import View

from apps.core.mixins import ActionPermissionMixin, require_action
from apps.core.permissions import (
    ACCOUNTANT,
    AUDITOR,
    CASHIER,
    CLOSE_PERIOD,
    OWNER_ADMIN,
    POST_PAYMENT,
    POST_SALES_INVOICE,
    SALES,
)
from apps.core.tests.factories import make_user


class RoleMatrixTests(TestCase):
    """The seeded groups grant what BRD §4.1 says they grant."""

    def test_groups_exist_and_are_populated(self):
        for name in [OWNER_ADMIN, ACCOUNTANT, SALES, CASHIER, AUDITOR]:
            group = Group.objects.get(name=name)
            self.assertGreater(
                group.permissions.count(), 0, f"{name} has no permissions — role seed failed"
            )

    def test_accountant_can_post_and_close(self):
        user = make_user("acc", ACCOUNTANT)
        self.assertTrue(user.has_perm(POST_SALES_INVOICE))
        self.assertTrue(user.has_perm(CLOSE_PERIOD))

    def test_sales_cannot_post_by_default(self):
        # BRD 4.1: "Create/edit drafts; submit; limited post if granted."
        user = make_user("sales", SALES)
        self.assertFalse(user.has_perm(POST_SALES_INVOICE))
        self.assertTrue(user.has_perm("sales.add_salesorder"))

    def test_cashier_can_take_money_but_not_configure(self):
        user = make_user("cash", CASHIER)
        self.assertTrue(user.has_perm(POST_PAYMENT))
        self.assertFalse(user.has_perm(CLOSE_PERIOD))
        self.assertFalse(user.has_perm("core.manage_configuration"))

    def test_auditor_sees_everything_and_changes_nothing(self):
        user = make_user("audit", AUDITOR)
        self.assertTrue(user.has_perm("sales.view_salesinvoice"))
        self.assertFalse(user.has_perm("sales.add_salesinvoice"))
        self.assertFalse(user.has_perm("sales.change_salesinvoice"))

    def test_owner_admin_has_everything(self):
        user = make_user("owner", OWNER_ADMIN)
        for perm in [POST_SALES_INVOICE, POST_PAYMENT, CLOSE_PERIOD]:
            self.assertTrue(user.has_perm(perm), perm)


class DirectUrlAccessTests(TestCase):
    """ACC-004: the denial must come from the view, not from a hidden button."""

    def setUp(self):
        self.factory = RequestFactory()

        class PostInvoiceView(ActionPermissionMixin, View):
            required_permission = POST_SALES_INVOICE

            def post(self, request):
                from django.http import HttpResponse

                return HttpResponse("posted")

        self.view = PostInvoiceView.as_view()

    def test_user_without_permission_is_denied(self):
        request = self.factory.post("/post/")
        request.user = make_user("nobody")
        with self.assertRaises(PermissionDenied):
            self.view(request)

    def test_user_with_permission_is_allowed(self):
        request = self.factory.post("/post/")
        request.user = make_user("acc2", ACCOUNTANT)
        response = self.view(request)
        self.assertEqual(response.status_code, 200)

    def test_read_only_account_is_blocked_on_write(self):
        """ACC-006: an auditor may hold permissions and still may not mutate."""
        request = self.factory.post("/post/")
        user = make_user("ro", ACCOUNTANT, is_read_only=True)
        request.user = user
        with self.assertRaises(PermissionDenied):
            self.view(request)

    def test_decorator_matches_the_mixin(self):
        @require_action(POST_SALES_INVOICE)
        def a_view(request):
            from django.http import HttpResponse

            return HttpResponse("ok")

        request = self.factory.post("/x/")
        request.user = make_user("nobody2")
        with self.assertRaises(PermissionDenied):
            a_view(request)


class AuthenticationTests(TestCase):
    """ACC-001, ACC-002."""

    def test_home_shows_landing_page_to_anonymous_user(self):
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "core/landing.html")

    def test_protected_page_redirects_anonymous_user_to_login(self):
        response = self.client.get(reverse("core:currency_list"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_valid_user_can_sign_in_and_reach_the_dashboard(self):
        make_user("hassan", OWNER_ADMIN)
        signed_in = self.client.login(username="hassan", password="testpass-12345")
        self.assertTrue(signed_in)
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)

    def test_authenticated_user_without_business_role_cannot_see_dashboard(self):
        make_user("unassigned")
        self.client.login(username="unassigned", password="testpass-12345")
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 403)

    def test_inactive_user_cannot_sign_in(self):
        user = make_user("gone")
        user.is_active = False
        user.deactivated_at = "2026-08-01T00:00:00Z"
        user.save()
        self.assertFalse(self.client.login(username="gone", password="testpass-12345"))

    def test_login_page_renders(self):
        response = self.client.get(reverse("login"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sign in")
        self.assertContains(response, 'href="/static/css/app.css"')
        self.assertNotContains(response, "cdn.tailwindcss.com")
        self.assertNotContains(response, "fonts.googleapis.com")

    def test_login_post_without_csrf_token_is_rejected(self):
        csrf_client = Client(enforce_csrf_checks=True)
        response = csrf_client.post(reverse("login"), {"username": "any", "password": "any"})
        self.assertEqual(response.status_code, 403)


class PermissionAttributeSpellingTests(SimpleTestCase):
    """
    A view on ActionPermissionMixin must declare `required_permission`.

    Django's own PermissionRequiredMixin calls the attribute
    `permission_required`, and this project's mixin does not read that name.
    Writing it is not an error and produces no warning — the permission is
    simply never checked, and every signed-in user reaches the screen. It has
    happened twice: on the settings screens, and again on the sales order and
    invoice lists.

    This walks the source rather than the URLs so a view is covered the moment
    it is written, before anyone remembers to add a test for it.
    """

    MIXINS = {
        "ActionPermissionMixin",
        "FilteredListView",
        "PostingPermissionMixin",
        "ConfirmationRequiredMixin",
    }

    def test_no_view_uses_djangos_attribute_name(self):
        import ast
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[3] / "apps"
        offenders = []
        for path in sorted(root.rglob("*.py")):
            if "migrations" in str(path):
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                if not isinstance(node, ast.ClassDef):
                    continue
                bases = {b.id for b in node.bases if isinstance(b, ast.Name)}
                if not bases & self.MIXINS:
                    continue
                for stmt in node.body:
                    if isinstance(stmt, ast.Assign) and any(
                        isinstance(t, ast.Name) and t.id == "permission_required"
                        for t in stmt.targets
                    ):
                        offenders.append(f"{path.name}:{stmt.lineno} {node.name}")
        self.assertEqual(
            offenders,
            [],
            "these declare permission_required, which this mixin ignores — "
            "use required_permission: " + ", ".join(offenders),
        )
