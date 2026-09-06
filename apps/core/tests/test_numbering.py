"""Tests for the document numbering service (CFG-008, NFR-008)."""

from datetime import date

from django.test import TestCase

from apps.core import numbering
from apps.core.models import DocumentSequence, DocumentType, SequenceReset


class NumberingTests(TestCase):
    """
    Every test builds its own sequence on series "TEST" rather than using the
    seeded DEFAULT one, so a change to the seed data can never break these
    tests, and the tests can never be read as documentation of the seed.
    """

    def make_sequence(self, **overrides):
        values = {
            "document_type": DocumentType.SALES_INVOICE,
            "series": "TEST",
            "prefix": "T-",
            "padding": 4,
            "next_number": 1,
            "period_key": "",
            "reset_policy": SequenceReset.YEARLY,
            "is_active": True,
        }
        values.update(overrides)
        return DocumentSequence.objects.create(**values)

    def allocate(self, on_date=date(2026, 5, 1)):
        return numbering.next_number(DocumentType.SALES_INVOICE, on_date, series="TEST")

    # -- allocation ---------------------------------------------------------

    def test_allocates_numbers_in_order(self):
        sequence = self.make_sequence()

        self.assertEqual(self.allocate(), "T-0001")
        self.assertEqual(self.allocate(), "T-0002")

        sequence.refresh_from_db()
        self.assertEqual(sequence.next_number, 3)

    def test_formats_with_prefix_padding_and_suffix(self):
        self.make_sequence(prefix="INV-", suffix="/26", padding=6, next_number=42)

        self.assertEqual(self.allocate(), "INV-000042/26")

    # -- period reset -------------------------------------------------------

    def test_unused_sequence_keeps_its_counter(self):
        """
        Regression: a blank period_key means "never used", not "a different
        period". Resetting here would re-issue numbers already in use.
        """
        self.make_sequence(next_number=8, period_key="")

        self.assertEqual(self.allocate(), "T-0008")

    def test_records_the_period_on_first_use(self):
        sequence = self.make_sequence(period_key="")

        self.allocate(date(2026, 5, 1))

        sequence.refresh_from_db()
        self.assertEqual(sequence.period_key, "2026")

    def test_restarts_at_one_in_a_new_year(self):
        sequence = self.make_sequence(next_number=42, period_key="2026")

        self.assertEqual(self.allocate(date(2027, 1, 2)), "T-0001")

        sequence.refresh_from_db()
        self.assertEqual(sequence.period_key, "2027")

    def test_does_not_restart_within_the_same_year(self):
        self.make_sequence(next_number=42, period_key="2026")

        self.assertEqual(self.allocate(date(2026, 12, 31)), "T-0042")

    def test_never_policy_ignores_the_year(self):
        self.make_sequence(next_number=42, period_key="", reset_policy=SequenceReset.NEVER)

        self.assertEqual(self.allocate(date(2026, 5, 1)), "T-0042")
        self.assertEqual(self.allocate(date(2027, 5, 1)), "T-0043")

    def test_monthly_policy_restarts_each_month(self):
        self.make_sequence(
            next_number=9, period_key="2026-05", reset_policy=SequenceReset.MONTHLY
        )

        self.assertEqual(self.allocate(date(2026, 6, 1)), "T-0001")

    # -- misconfiguration ---------------------------------------------------

    def test_missing_sequence_raises(self):
        with self.assertRaises(numbering.SequenceNotConfigured):
            numbering.next_number(DocumentType.SALES_INVOICE, series="NOPE")

    def test_inactive_sequence_raises(self):
        self.make_sequence(is_active=False)

        with self.assertRaises(numbering.SequenceNotConfigured):
            self.allocate()
