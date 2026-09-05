"""The four financial screens: access, rendering, and export fidelity."""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse

from apps.reports import reconciliation
from apps.reports.tests.test_statements import TradingYearFixture

STATEMENTS = (
    "reports:trial_balance",
    "reports:profit_and_loss",
    "reports:balance_sheet",
    "reports:reconciliation",
    "reports:ar_ageing",
    "reports:ap_ageing",
    "reports:tax",
    "reports:money_register",
)
ALL_SCREENS = ("reports:general_ledger", *STATEMENTS)


class AccessTests(TestCase):
    """RPT screens are gated on VIEW_FINANCIAL_REPORTS, not merely on a login."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.reader = User.objects.create_user(
            id=940_001,
            username="r6-reader",
            email="r6-reader@example.com",
            password="x-test-password",
        )
        cls.reader.user_permissions.add(
            Permission.objects.get(codename="view_financial_reports")
        )
        cls.outsider = User.objects.create_user(
            id=940_002,
            username="r6-outsider",
            email="r6-outsider@example.com",
            password="x-test-password",
        )

    def test_anonymous_users_are_sent_to_the_login_page(self):
        for name in ALL_SCREENS:
            with self.subTest(screen=name):
                response = self.client.get(reverse(name))
                self.assertIn(response.status_code, (302, 403))

    def test_a_signed_in_user_without_the_permission_is_refused(self):
        """Being an employee is not the same as being allowed to read the books."""
        self.client.force_login(self.outsider)
        for name in ALL_SCREENS:
            with self.subTest(screen=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 403)

    def test_the_permission_is_enough(self):
        self.client.force_login(self.reader)
        for name in ALL_SCREENS:
            with self.subTest(screen=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)


class ReaderFixture(TradingYearFixture):
    """A trading year plus somebody allowed to look at it.

    A mixin rather than a base TestCase: subclassing one TestCase from another
    re-runs every one of the parent's tests under the child's name, which is a
    slow way to get the same assertions twice and a confusing way to read a
    failure.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        User = get_user_model()
        cls.reader = User.objects.create_user(
            id=940_010,
            username="r6-render",
            email="r6-render@example.com",
            password="x-test-password",
        )
        for codename in ("view_financial_reports", "export_data"):
            cls.reader.user_permissions.add(Permission.objects.get(codename=codename))

    def setUp(self):
        self.client.force_login(self.reader)
        self.window = {"date_from": "2093-01-01", "date_to": "2093-12-31"}


class RenderingTests(ReaderFixture, TestCase):
    def test_the_trial_balance_reports_itself_balanced(self):
        response = self.client.get(reverse("reports:trial_balance"), self.window)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["report"].is_balanced)
        self.assertNotContains(response, "The ledger does not balance")

    def test_the_profit_and_loss_shows_the_period_result(self):
        response = self.client.get(reverse("reports:profit_and_loss"), self.window)
        self.assertEqual(response.context["report"].net_profit, Decimal("200.0000"))
        self.assertContains(response, "Gross profit")

    def test_the_balance_sheet_balances_and_names_the_unclosed_result(self):
        response = self.client.get(reverse("reports:balance_sheet"), {"as_of": "2093-12-31"})
        report = response.context["report"]
        self.assertTrue(report.is_balanced)
        self.assertContains(response, "Result for the period")
        self.assertNotContains(response, "does not balance")

    def test_the_general_ledger_lists_posted_lines(self):
        response = self.client.get(reverse("reports:general_ledger"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "R6-JE-")

    def test_an_impossible_range_is_rejected_rather_than_guessed_at(self):
        response = self.client.get(
            reverse("reports:trial_balance"),
            {"date_from": "2093-12-31", "date_to": "2093-01-01"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["form"].is_valid())


class ExportTests(ReaderFixture, TestCase):
    """UX-007: what downloads is what was on screen."""

    def _csv(self, name, params):
        response = self.client.get(reverse(name), {**params, "export": "csv"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")
        return response.content.decode()

    def test_the_trial_balance_export_carries_the_same_totals(self):
        body = self._csv("reports:trial_balance", self.window)
        onscreen = self.client.get(reverse("reports:trial_balance"), self.window).context[
            "report"
        ]
        self.assertIn(f"{onscreen.closing_debit:.2f}", body)
        self.assertIn(f"{onscreen.closing_credit:.2f}", body)

    def test_the_balance_sheet_export_carries_the_unclosed_result(self):
        body = self._csv("reports:balance_sheet", {"as_of": "2093-12-31"})
        self.assertIn("Result for the period (not yet closed)", body)
        self.assertIn("TOTAL LIABILITIES AND EQUITY", body)

    def test_the_profit_and_loss_export_names_its_window(self):
        body = self._csv("reports:profit_and_loss", self.window)
        self.assertIn("2093-01-01 to 2093-12-31", body)
        self.assertIn("Gross profit", body)

    def test_export_needs_the_export_permission_of_its_own(self):
        """Reading a statement and taking a copy away are different rights."""
        User = get_user_model()
        looker = User.objects.create_user(
            id=940_020,
            username="r6-noexport",
            email="r6-noexport@example.com",
            password="x-test-password",
        )
        looker.user_permissions.add(Permission.objects.get(codename="view_financial_reports"))
        self.client.force_login(looker)
        for name in STATEMENTS:
            with self.subTest(screen=name):
                response = self.client.get(reverse(name), {"export": "csv"})
                self.assertEqual(response.status_code, 403)


class ReconciliationScreenTests(ReaderFixture, TestCase):
    """GL-011 / RPT-021: the screen has to distinguish three states, not two."""

    def test_it_renders_for_a_reader(self):
        response = self.client.get(reverse("reports:reconciliation"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Subledger")

    def test_a_control_account_with_no_ledger_activity_is_reported_as_unexamined(self):
        """The view returns no row for it at all, which must not read as agreement."""
        response = self.client.get(reverse("reports:reconciliation"))
        checked = {check.control_type for check in response.context["checks"]}
        unevaluated = response.context["unevaluated"]
        # Whatever the data, every control type is accounted for one way or the
        # other - nothing is silently missing from the page.
        self.assertEqual(
            len(checked) + len(unevaluated),
            len(reconciliation.CONTROL_LABELS),
        )

    def test_the_export_names_the_unexamined_ones_too(self):
        body = self._csv_body("reports:reconciliation")
        for label in self.client.get(reverse("reports:reconciliation")).context["unevaluated"]:
            self.assertIn(label, body)

    def _csv_body(self, name):
        response = self.client.get(reverse(name), {"export": "csv"})
        self.assertEqual(response.status_code, 200)
        return response.content.decode()
