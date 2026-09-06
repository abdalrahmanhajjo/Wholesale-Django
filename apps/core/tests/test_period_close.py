"""Closing and reopening a fiscal period (CFG-009, ACC-008, BR-020).

The rules under test are the ones that stop a close from meaning nothing:
periods close in order, they do not close over broken arithmetic, and they do
not reopen into a hole.
"""

from datetime import date

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from apps.core import period_close
from apps.core.models import FiscalPeriod, FiscalYear, PeriodStatus


class PeriodFixture:
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            id=950_001,
            username="m7-closer",
            email="m7-closer@example.com",
            password="x-test-password",
        )
        # Deliberately earlier than the seeded 2026 calendar. These tests are
        # about the ordering among *these three* periods, and a fixture dated
        # after the seed would sit behind two years of open seeded periods -
        # so every close would be blocked, correctly, for a reason that has
        # nothing to do with what is being tested.
        cls.year = FiscalYear.objects.create(
            code="M7-FY94", start_date=date(1994, 1, 1), end_date=date(1994, 12, 31)
        )
        cls.january = cls._period(1, "M7 January", date(1994, 1, 1), date(1994, 1, 31))
        cls.february = cls._period(2, "M7 February", date(1994, 2, 1), date(1994, 2, 28))
        cls.march = cls._period(3, "M7 March", date(1994, 3, 1), date(1994, 3, 31))

    @classmethod
    def _period(cls, no, name, start, end):
        return FiscalPeriod.objects.create(
            fiscal_year=cls.year, period_no=no, name=name, start_date=start, end_date=end
        )


class ClosingTests(PeriodFixture, TestCase):
    def test_a_clean_period_closes_and_records_who_and_why(self):
        closed = period_close.close_period(
            self.january, user=self.user, reason="Reviewed by the accountant."
        )
        self.assertEqual(closed.status, PeriodStatus.CLOSED)
        self.assertEqual(closed.closed_by, self.user)
        self.assertIsNotNone(closed.closed_at)
        self.assertEqual(closed.close_reason, "Reviewed by the accountant.")

    def test_closing_out_of_order_is_refused(self):
        """March cannot close while January is open: its opening balances could move."""
        with self.assertRaises(ValidationError) as caught:
            period_close.close_period(self.march, user=self.user, reason="Month end.")
        self.assertIn("still open", " ".join(caught.exception.messages))

    def test_closing_in_order_works(self):
        for period in (self.january, self.february, self.march):
            period_close.close_period(period, user=self.user, reason="Month end.")
        self.march.refresh_from_db()
        self.assertEqual(self.march.status, PeriodStatus.CLOSED)

    def test_a_reason_is_required(self):
        """It is the only record of who signed the period off."""
        for blank in ("", "   "):
            with self.subTest(reason=repr(blank)):
                with self.assertRaises(ValidationError):
                    period_close.close_period(self.january, user=self.user, reason=blank)

    def test_a_period_cannot_be_closed_twice(self):
        period_close.close_period(self.january, user=self.user, reason="Month end.")
        with self.assertRaises(ValidationError):
            period_close.close_period(self.january, user=self.user, reason="Again.")

    def test_a_locked_period_cannot_be_closed(self):
        self.january.status = PeriodStatus.LOCKED
        self.january.closed_by = self.user
        self.january.save(update_fields=["status", "closed_by"])
        with self.assertRaises(ValidationError):
            period_close.close_period(self.january, user=self.user, reason="Month end.")


class ReopeningTests(PeriodFixture, TestCase):
    def setUp(self):
        for period in (self.january, self.february):
            period_close.close_period(period, user=self.user, reason="Month end.")

    def test_reopening_records_who_and_why(self):
        reopened = period_close.reopen_period(
            self.february, user=self.user, reason="A missed invoice has to go in."
        )
        self.assertEqual(reopened.status, PeriodStatus.OPEN)
        self.assertEqual(reopened.reopened_by, self.user)
        self.assertIsNotNone(reopened.reopened_at)
        self.assertEqual(reopened.reopen_reason, "A missed invoice has to go in.")
        # The close record survives; reopening adds to the history, it does not
        # erase it.
        self.assertIsNotNone(reopened.closed_at)

    def test_reopening_out_of_order_is_refused(self):
        """Reopening January while February is closed would move February's opening."""
        with self.assertRaises(ValidationError) as caught:
            period_close.reopen_period(self.january, user=self.user, reason="A correction.")
        self.assertIn("closed after this period", " ".join(caught.exception.messages))

    def test_reopening_in_reverse_order_works(self):
        period_close.reopen_period(self.february, user=self.user, reason="Correction.")
        period_close.reopen_period(self.january, user=self.user, reason="Correction.")
        self.january.refresh_from_db()
        self.assertEqual(self.january.status, PeriodStatus.OPEN)

    def test_a_locked_period_can_never_be_reopened(self):
        """That is the whole difference between CLOSED and LOCKED."""
        self.february.status = PeriodStatus.LOCKED
        self.february.save(update_fields=["status"])
        with self.assertRaises(ValidationError) as caught:
            period_close.reopen_period(self.february, user=self.user, reason="Please.")
        self.assertIn("permanently locked", " ".join(caught.exception.messages))

    def test_an_open_period_cannot_be_reopened(self):
        with self.assertRaises(ValidationError):
            period_close.reopen_period(self.march, user=self.user, reason="Why not.")

    def test_a_reason_is_required(self):
        with self.assertRaises(ValidationError):
            period_close.reopen_period(self.february, user=self.user, reason="  ")


class ChecklistTests(PeriodFixture, TestCase):
    def test_an_open_earlier_period_is_a_blocker_not_a_warning(self):
        report = period_close.checklist(self.march)
        earlier = next(c for c in report.checks if c.key == "earlier_periods")
        self.assertFalse(earlier.passed)
        self.assertEqual(earlier.severity, period_close.BLOCKER)
        self.assertFalse(report.can_close)

    def test_a_blocker_clears_once_the_earlier_period_closes(self):
        for period in (self.january, self.february):
            period_close.close_period(period, user=self.user, reason="Month end.")
        report = period_close.checklist(self.march)
        self.assertTrue(report.can_close, [c.detail for c in report.blockers])

    def test_warnings_do_not_prevent_closing(self):
        """A warning is a decision to be made, not an arithmetic error."""
        report = period_close.checklist(self.january)
        for check in report.checks:
            if check.severity == period_close.WARNING and not check.passed:
                self.assertNotIn(check, report.blockers)
        self.assertTrue(report.can_close)

    def test_every_check_explains_itself(self):
        """A checklist item nobody can act on is decoration."""
        for check in period_close.checklist(self.march).checks:
            with self.subTest(check=check.key):
                self.assertTrue(check.title)
                self.assertTrue(check.detail, f"{check.key} gave no explanation")


class DocumentRegistryTests(TestCase):
    def test_every_registered_source_actually_exists(self):
        """A typo here would silently stop checking a whole document type."""
        from django.apps import apps as django_apps

        for app_label, model_name, date_field, label, _url in period_close.UNPOSTED_SOURCES:
            with self.subTest(model=f"{app_label}.{model_name}"):
                model = django_apps.get_model(app_label, model_name)
                fields = {f.name for f in model._meta.get_fields() if hasattr(f, "attname")}
                self.assertIn(date_field, fields)
                self.assertIn("status", fields)
                self.assertTrue(label)

    def test_registered_urls_resolve(self):
        for *_, url_name in period_close.UNPOSTED_SOURCES:
            if url_name:
                with self.subTest(url=url_name):
                    self.assertTrue(reverse(url_name))

    def test_orders_are_deliberately_excluded(self):
        """Orders carry a journal_entry field but never post; they are commitments."""
        registered = {f"{app}.{model}" for app, model, *_ in period_close.UNPOSTED_SOURCES}
        self.assertNotIn("sales.SalesOrder", registered)
        self.assertNotIn("purchases.PurchaseOrder", registered)


class PermissionTests(PeriodFixture, TestCase):
    def setUp(self):
        self.viewer = get_user_model().objects.create_user(
            id=950_010,
            username="m7-viewer",
            email="m7-viewer@example.com",
            password="x-test-password",
        )
        self.viewer.user_permissions.add(Permission.objects.get(codename="view_fiscalperiod"))

    def test_the_checklist_is_visible_without_the_power_to_close(self):
        """Seeing why a period is not ready is not the same as signing it off."""
        self.client.force_login(self.viewer)
        response = self.client.get(reverse("core:period_close", args=[self.january.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["can_close"])

    def test_closing_without_the_permission_is_refused(self):
        self.client.force_login(self.viewer)
        response = self.client.post(
            reverse("core:period_close_confirm", args=[self.january.pk]),
            {"reason": "Trying anyway."},
        )
        self.assertEqual(response.status_code, 403)
        self.january.refresh_from_db()
        self.assertEqual(self.january.status, PeriodStatus.OPEN)

    def test_reopening_needs_its_own_permission(self):
        """Close and reopen are separate rights, not one 'manage periods' right."""
        period_close.close_period(self.january, user=self.user, reason="Month end.")
        closer = get_user_model().objects.create_user(
            id=950_011,
            username="m7-closer-only",
            email="m7-closer-only@example.com",
            password="x-test-password",
        )
        for codename in ("view_fiscalperiod", "close_period"):
            closer.user_permissions.add(Permission.objects.get(codename=codename))
        self.client.force_login(closer)
        response = self.client.post(
            reverse("core:period_reopen", args=[self.january.pk]),
            {"reason": "Undo it."},
        )
        self.assertEqual(response.status_code, 403)
        self.january.refresh_from_db()
        self.assertEqual(self.january.status, PeriodStatus.CLOSED)


class CloseWorkflowTests(PeriodFixture, TestCase):
    """The whole path through the screen, not just the service beneath it."""

    def setUp(self):
        for codename in ("view_fiscalperiod", "close_period", "reopen_period"):
            self.user.user_permissions.add(Permission.objects.get(codename=codename))
        self.client.force_login(self.user)

    def _checklist_warnings(self, period):
        return period_close.checklist(period).warnings

    def test_closing_through_the_screen_records_everything(self):
        warnings = self._checklist_warnings(self.january)
        payload = {"reason": "Reviewed with the accountant."}
        if warnings:
            payload["acknowledge"] = "on"

        response = self.client.post(
            reverse("core:period_close_confirm", args=[self.january.pk]), payload
        )
        self.assertEqual(response.status_code, 302)

        self.january.refresh_from_db()
        self.assertEqual(self.january.status, PeriodStatus.CLOSED)
        self.assertEqual(self.january.closed_by, self.user)
        self.assertEqual(self.january.close_reason, "Reviewed with the accountant.")

    def test_unacknowledged_warnings_stop_the_close(self):
        """The confirmation only appears when there is something to confirm."""
        if not self._checklist_warnings(self.january):
            self.skipTest("Nothing unresolved in this fixture, so nothing to acknowledge.")
        response = self.client.post(
            reverse("core:period_close_confirm", args=[self.january.pk]),
            {"reason": "Closing without looking."},
        )
        self.assertEqual(response.status_code, 302)
        self.january.refresh_from_db()
        self.assertEqual(self.january.status, PeriodStatus.OPEN)

    def test_closing_without_a_reason_is_stopped_by_the_form(self):
        response = self.client.post(
            reverse("core:period_close_confirm", args=[self.january.pk]), {"reason": ""}
        )
        self.assertEqual(response.status_code, 302)
        self.january.refresh_from_db()
        self.assertEqual(self.january.status, PeriodStatus.OPEN)

    def test_the_checklist_is_recomputed_when_the_button_is_pressed(self):
        """The page may have been open for an hour before anyone clicked.

        March looked closeable only because the screen was drawn before January
        and February were reopened; the action has to check again, not trust
        what was rendered.
        """
        for period in (self.january, self.february):
            period_close.close_period(period, user=self.user, reason="Month end.")
        self.assertTrue(period_close.checklist(self.march).can_close)

        period_close.reopen_period(self.february, user=self.user, reason="Correction.")

        response = self.client.post(
            reverse("core:period_close_confirm", args=[self.march.pk]),
            {"reason": "Month end.", "acknowledge": "on"},
        )
        self.assertEqual(response.status_code, 302)
        self.march.refresh_from_db()
        self.assertEqual(self.march.status, PeriodStatus.OPEN)
