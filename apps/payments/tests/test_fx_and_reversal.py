"""FX settlement, reversal, dependency checks, and the voucher (PAY-010, PAY-011).

Realised FX is checked in the ledger, not only on the allocation row: a stored
number that no journal agrees with would be worse than no number at all.
"""

from decimal import Decimal
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse as url_for

from apps.core.models import DocumentStatus
from apps.ledger.models import MappingKey
from apps.ledger.services.exceptions import PostingError
from apps.payments.allocation import AllocationLineInput, allocate_payment
from apps.payments.models import Allocation, PaymentDirection
from apps.payments.reversal import (
    live_allocation_batches,
    reverse_allocation_batch,
    reverse_payment,
)
from apps.payments.tests.test_allocation import RATE, TODAY, AllocationFixtureMixin


class FxFixtureMixin(AllocationFixtureMixin):
    """Adds the two FX accounts the reclassification needs when rates move."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from apps.ledger.models import AccountType, NormalBalance

        cls._map(MappingKey.FX_GAIN, "A4-FXG", AccountType.INCOME, NormalBalance.CREDIT)
        cls._map(MappingKey.FX_LOSS, "A4-FXL", AccountType.EXPENSE, NormalBalance.DEBIT)

    def lines_by_account(self, entry):
        return {line.account.code: line for line in entry.lines.select_related("account")}


# ---------------------------------------------------------------------------
# Realised FX
# ---------------------------------------------------------------------------


class RealisedFxTests(FxFixtureMixin, TestCase):
    def test_settling_a_lower_rated_invoice_realises_a_gain(self):
        # Invoice booked at 1.20; money arrived at 1.25. 100 is worth 5 more.
        invoice = self.make_invoice("INV-FX1", "100", rate=Decimal("1.20"))
        payment = self.make_payment("100")

        result = self.allocate(payment, [(invoice, "100")])

        row = result.allocations[0]
        self.assertEqual(row.fx_gain_loss_base, Decimal("5.0000"))
        self.assertEqual(row.settlement_rate, RATE)

        lines = self.lines_by_account(row.journal_entry)
        self.assertEqual(lines["A4-CADV"].debit_base, Decimal("125.0000"))
        self.assertEqual(lines["A4-AR"].credit_base, Decimal("120.0000"))
        self.assertEqual(lines["A4-FXG"].credit_base, Decimal("5.0000"))
        # The difference exists only between two rates; it has no txn amount.
        self.assertEqual(lines["A4-FXG"].credit_txn, Decimal("0"))
        self.assertEqual(row.journal_entry.total_debit_base, Decimal("125.0000"))

    def test_settling_a_higher_rated_invoice_realises_a_loss(self):
        invoice = self.make_invoice("INV-FX2", "100", rate=Decimal("1.30"))
        payment = self.make_payment("100")

        row = self.allocate(payment, [(invoice, "100")]).allocations[0]

        self.assertEqual(row.fx_gain_loss_base, Decimal("-5.0000"))
        lines = self.lines_by_account(row.journal_entry)
        self.assertEqual(lines["A4-CADV"].debit_base, Decimal("125.0000"))
        self.assertEqual(lines["A4-AR"].credit_base, Decimal("130.0000"))
        self.assertEqual(lines["A4-FXL"].debit_base, Decimal("5.0000"))

    def test_vendor_payment_at_a_higher_rate_is_a_loss(self):
        # Paying out more base currency than the payable was booked at.
        bill = self.make_bill("BILL-FX1", "100", rate=Decimal("1.20"))
        payment = self.make_payment("100", direction=PaymentDirection.PAYMENT)

        row = self.allocate(payment, [(bill, "100")]).allocations[0]

        self.assertEqual(row.fx_gain_loss_base, Decimal("-5.0000"))
        lines = self.lines_by_account(row.journal_entry)
        self.assertEqual(lines["A4-AP"].debit_base, Decimal("120.0000"))
        self.assertEqual(lines["A4-VADV"].credit_base, Decimal("125.0000"))
        self.assertEqual(lines["A4-FXL"].debit_base, Decimal("5.0000"))

    def test_vendor_payment_at_a_lower_rate_is_a_gain(self):
        bill = self.make_bill("BILL-FX2", "100", rate=Decimal("1.30"))
        payment = self.make_payment("100", direction=PaymentDirection.PAYMENT)

        row = self.allocate(payment, [(bill, "100")]).allocations[0]

        self.assertEqual(row.fx_gain_loss_base, Decimal("5.0000"))
        lines = self.lines_by_account(row.journal_entry)
        self.assertEqual(lines["A4-FXG"].credit_base, Decimal("5.0000"))

    def test_no_rate_movement_posts_no_fx_line(self):
        invoice = self.make_invoice("INV-FX3", "100")
        payment = self.make_payment("100")

        row = self.allocate(payment, [(invoice, "100")]).allocations[0]

        self.assertEqual(row.fx_gain_loss_base, Decimal("0.0000"))
        self.assertIsNone(row.fx_journal_entry_id)
        lines = self.lines_by_account(row.journal_entry)
        self.assertEqual(len(lines), 2)
        self.assertNotIn("A4-FXG", lines)
        self.assertNotIn("A4-FXL", lines)

    def test_a_batch_nets_gains_against_losses_in_one_journal(self):
        gain = self.make_invoice("INV-FX4", "100", rate=Decimal("1.20"))
        loss = self.make_invoice("INV-FX5", "100", rate=Decimal("1.30"))
        payment = self.make_payment("200")

        result = self.allocate(payment, [(gain, "100"), (loss, "100")])

        rows = {row.target.number: row for row in result.allocations}
        self.assertEqual(rows["INV-FX4"].fx_gain_loss_base, Decimal("5.0000"))
        self.assertEqual(rows["INV-FX5"].fx_gain_loss_base, Decimal("-5.0000"))
        # +5 and -5 cancel, so the journal needs no FX line at all.
        entry = result.allocations[0].journal_entry
        self.assertEqual(len(self.lines_by_account(entry)), 2)
        self.assertEqual(entry.total_debit_base, Decimal("250.0000"))

    def test_partial_settlement_realises_fx_only_on_what_was_settled(self):
        invoice = self.make_invoice("INV-FX6", "100", rate=Decimal("1.20"))
        payment = self.make_payment("100")

        row = self.allocate(payment, [(invoice, "40")]).allocations[0]

        # 40 at 1.25 is 50; at 1.20 it is 48.
        self.assertEqual(row.fx_gain_loss_base, Decimal("2.0000"))
        self.assertEqual(row.amount_base, Decimal("50.0000"))

    def test_the_fx_journal_is_the_allocation_journal(self):
        invoice = self.make_invoice("INV-FX7", "100", rate=Decimal("1.20"))
        payment = self.make_payment("100")

        row = self.allocate(payment, [(invoice, "100")]).allocations[0]

        self.assertEqual(row.fx_journal_entry_id, row.journal_entry_id)

    def test_a_different_currency_is_still_refused(self):
        invoice = self.make_invoice("INV-FX8", "100", currency=self.other_currency)
        payment = self.make_payment("100")
        with self.assertRaises(ValidationError) as ctx:
            self.allocate(payment, [(invoice, "10")])
        self.assertIn("different currency", str(ctx.exception))

    def test_missing_fx_mapping_is_reported_and_nothing_is_written(self):
        from apps.ledger.models import AccountMapping

        AccountMapping.objects.filter(key=MappingKey.FX_GAIN).delete()
        invoice = self.make_invoice("INV-FX9", "100", rate=Decimal("1.20"))
        payment = self.make_payment("100")

        with self.assertRaises((ValidationError, PostingError)):
            self.allocate(payment, [(invoice, "100")])
        invoice.refresh_from_db()
        self.assertEqual(invoice.allocated_txn, Decimal("0.0000"))
        self.assertEqual(Allocation.objects.count(), 0)


# ---------------------------------------------------------------------------
# Reversing an allocation
# ---------------------------------------------------------------------------


class AllocationReversalTests(FxFixtureMixin, TestCase):
    def _reverse(self, batch_key, reason="Applied to the wrong invoice"):
        return reverse_allocation_batch(
            batch_key, user=self.user, reason=reason, reversal_date=TODAY
        )

    def test_reversal_gives_the_invoice_its_balance_back(self):
        invoice = self.make_invoice("INV-R1", "100")
        payment = self.make_payment("100")
        key = uuid4()
        self.allocate(payment, [(invoice, "40")], key=key)

        self._reverse(key)

        invoice.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual(invoice.allocated_txn, Decimal("0.0000"))
        self.assertEqual(invoice.open_txn, Decimal("100.0000"))
        self.assertEqual(invoice.status, DocumentStatus.POSTED)
        self.assertEqual(payment.allocated_txn, Decimal("0.0000"))
        self.assertEqual(payment.unallocated_txn, Decimal("100.0000"))
        self.assertEqual(payment.status, DocumentStatus.POSTED)

    def test_reversal_posts_a_mirror_journal_that_points_back(self):
        invoice = self.make_invoice("INV-R2", "100")
        payment = self.make_payment("100")
        key = uuid4()
        original = self.allocate(payment, [(invoice, "40")], key=key).allocations[0]

        result = self._reverse(key)

        entry = result.journal_entry
        self.assertTrue(entry.is_reversal)
        self.assertEqual(entry.reverses_id, original.journal_entry_id)
        self.assertIn("wrong invoice", entry.reversal_reason)
        # Every side is swapped.
        before = self.lines_by_account(original.journal_entry)
        after = self.lines_by_account(entry)
        self.assertEqual(after["A4-CADV"].credit_base, before["A4-CADV"].debit_base)
        self.assertEqual(after["A4-AR"].debit_base, before["A4-AR"].credit_base)

    def test_reversing_an_fx_allocation_reverses_the_fx_too(self):
        invoice = self.make_invoice("INV-R3", "100", rate=Decimal("1.20"))
        payment = self.make_payment("100")
        key = uuid4()
        self.allocate(payment, [(invoice, "100")], key=key)

        result = self._reverse(key)

        after = self.lines_by_account(result.journal_entry)
        self.assertEqual(after["A4-FXG"].debit_base, Decimal("5.0000"))
        invoice.refresh_from_db()
        self.assertEqual(invoice.open_txn, Decimal("100.0000"))

    def test_reversal_marks_every_row_and_records_the_reason(self):
        first = self.make_invoice("INV-R4", "50")
        second = self.make_invoice("INV-R5", "50")
        payment = self.make_payment("100")
        key = uuid4()
        self.allocate(payment, [(first, "50"), (second, "50")], key=key)

        self._reverse(key, reason="Duplicate receipt")

        rows = Allocation.objects.filter(batch_key=key)
        self.assertEqual(rows.count(), 2)
        self.assertTrue(all(row.is_reversed for row in rows))
        self.assertTrue(all(row.reversal_reason == "Duplicate receipt" for row in rows))

    def test_a_batch_cannot_be_reversed_twice(self):
        invoice = self.make_invoice("INV-R6", "100")
        payment = self.make_payment("100")
        key = uuid4()
        self.allocate(payment, [(invoice, "40")], key=key)
        self._reverse(key)

        with self.assertRaises(ValidationError) as ctx:
            self._reverse(key)
        self.assertIn("already been reversed", str(ctx.exception))

    def test_an_unknown_batch_is_refused(self):
        with self.assertRaises(ValidationError):
            self._reverse(uuid4())

    def test_a_reason_is_required(self):
        invoice = self.make_invoice("INV-R7", "100")
        payment = self.make_payment("100")
        key = uuid4()
        self.allocate(payment, [(invoice, "40")], key=key)

        for bad in ("", "   "):
            with self.assertRaises(ValidationError) as ctx:
                self._reverse(key, reason=bad)
            self.assertIn("reason", str(ctx.exception).lower())

    def test_the_freed_advance_can_be_allocated_again(self):
        first = self.make_invoice("INV-R8", "100")
        second = self.make_invoice("INV-R9", "100")
        payment = self.make_payment("100")
        key = uuid4()
        self.allocate(payment, [(first, "100")], key=key)

        self._reverse(key)
        self.allocate(payment, [(second, "100")])

        first.refresh_from_db()
        second.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual(first.open_txn, Decimal("100.0000"))
        self.assertEqual(second.open_txn, Decimal("0.0000"))
        self.assertEqual(payment.unallocated_txn, Decimal("0.0000"))

    def test_reversing_a_credit_application_restores_credited_not_allocated(self):
        from apps.payments.allocation import allocate_sales_credit
        from apps.payments.tests.test_allocation import CreditAllocationTests

        invoice = self.make_invoice("INV-R10", "100")
        note = CreditAllocationTests._credit_note(self, "CN-R1", "60")
        key = uuid4()
        allocate_sales_credit(
            note,
            lines=[AllocationLineInput(target_id=invoice.pk, amount_txn=Decimal("60"))],
            allocation_date=TODAY,
            user=self.user,
            batch_key=key,
        )

        self._reverse(key)

        invoice.refresh_from_db()
        note.refresh_from_db()
        self.assertEqual(invoice.credited_txn, Decimal("0.0000"))
        self.assertEqual(invoice.open_txn, Decimal("100.0000"))
        self.assertEqual(note.open_txn, Decimal("60.0000"))


# ---------------------------------------------------------------------------
# Reversing a payment, and the dependency check
# ---------------------------------------------------------------------------


class PaymentReversalTests(FxFixtureMixin, TestCase):
    def _reverse(self, payment, reason="Received in error"):
        return reverse_payment(payment, user=self.user, reason=reason, reversal_date=TODAY)

    def test_an_unallocated_payment_reverses(self):
        payment = self.make_payment("100")

        result = self._reverse(payment)

        payment.refresh_from_db()
        self.assertTrue(payment.is_reversed)
        self.assertEqual(payment.status, DocumentStatus.REVERSED)
        self.assertEqual(payment.reversal_reason, "Received in error")
        self.assertEqual(payment.reversed_by_id, self.user.pk)
        self.assertIsNotNone(payment.reversed_at)
        self.assertEqual(payment.reversal_journal_id, result.journal_entry.pk)

    def test_the_reversing_journal_mirrors_the_posting_and_points_back(self):
        payment = self.make_payment("100")
        original = payment.journal_entry

        entry = self._reverse(payment).journal_entry

        self.assertTrue(entry.is_reversal)
        self.assertEqual(entry.reverses_id, original.pk)
        before = self.lines_by_account(original)
        after = self.lines_by_account(entry)
        self.assertEqual(after["A4-CASH"].credit_base, before["A4-CASH"].debit_base)
        self.assertEqual(after["A4-CADV"].debit_base, before["A4-CADV"].credit_base)

    def test_an_allocated_payment_is_refused_and_names_the_documents(self):
        invoice = self.make_invoice("INV-P1", "100")
        payment = self.make_payment("100")
        self.allocate(payment, [(invoice, "40")])

        with self.assertRaises(ValidationError) as ctx:
            self._reverse(payment)

        message = str(ctx.exception)
        self.assertIn("INV-P1", message)
        self.assertIn("Reverse the allocation first", message)
        payment.refresh_from_db()
        self.assertFalse(payment.is_reversed)

    def test_reversing_the_allocation_first_then_the_payment_works(self):
        invoice = self.make_invoice("INV-P2", "100")
        payment = self.make_payment("100")
        key = uuid4()
        self.allocate(payment, [(invoice, "40")], key=key)

        reverse_allocation_batch(
            key, user=self.user, reason="Wrong invoice", reversal_date=TODAY
        )
        self._reverse(payment)

        payment.refresh_from_db()
        invoice.refresh_from_db()
        self.assertTrue(payment.is_reversed)
        self.assertEqual(invoice.open_txn, Decimal("100.0000"))

    def test_a_reversed_allocation_no_longer_blocks_the_payment(self):
        invoice = self.make_invoice("INV-P3", "100")
        payment = self.make_payment("100")
        key = uuid4()
        self.allocate(payment, [(invoice, "40")], key=key)
        reverse_allocation_batch(
            key, user=self.user, reason="Wrong invoice", reversal_date=TODAY
        )
        self.assertEqual(live_allocation_batches(payment), [])

    def test_a_payment_cannot_be_reversed_twice(self):
        payment = self.make_payment("100")
        self._reverse(payment)
        with self.assertRaises(ValidationError) as ctx:
            self._reverse(payment)
        self.assertIn("already been reversed", str(ctx.exception))

    def test_a_draft_payment_cannot_be_reversed(self):
        payment = self.make_payment("100", post=False)
        with self.assertRaises(ValidationError) as ctx:
            self._reverse(payment)
        self.assertIn("not posted", str(ctx.exception))

    def test_a_reason_is_required(self):
        payment = self.make_payment("100")
        with self.assertRaises(ValidationError):
            self._reverse(payment, reason="  ")

    def test_a_reversed_payment_cannot_be_allocated(self):
        invoice = self.make_invoice("INV-P4", "100")
        payment = self.make_payment("100")
        self._reverse(payment)
        with self.assertRaises(ValidationError):
            allocate_payment(
                payment,
                lines=[AllocationLineInput(target_id=invoice.pk, amount_txn=Decimal("10"))],
                allocation_date=TODAY,
                user=self.user,
                batch_key=uuid4(),
            )

    def test_live_batches_lists_what_blocks_the_reversal(self):
        first = self.make_invoice("INV-P5", "50")
        second = self.make_invoice("INV-P6", "50")
        payment = self.make_payment("100")
        self.allocate(payment, [(first, "50"), (second, "50")])

        batches = live_allocation_batches(payment)

        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0]["amount_txn"], Decimal("100.0000"))
        self.assertEqual(sorted(batches[0]["documents"]), ["INV-P5", "INV-P6"])


# ---------------------------------------------------------------------------
# The screens
# ---------------------------------------------------------------------------


class ReversalAndVoucherViewTests(FxFixtureMixin, TestCase):
    def setUp(self):
        self.invoice = self.make_invoice("INV-V1", "100", rate=Decimal("1.20"))
        self.payment = self.make_payment("100")
        self.actor = get_user_model().objects.create_user(
            username="reverser", email="reverser@example.com"
        )
        self.client.force_login(self.actor)

    def _grant(self, *codenames):
        self.actor.user_permissions.add(*Permission.objects.filter(codename__in=codenames))
        self.actor = get_user_model().objects.get(pk=self.actor.pk)
        self.client.force_login(self.actor)

    def test_reverse_screen_requires_the_permission(self):
        url = url_for("payments:payment_reverse", args=[self.payment.pk])
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_reverse_screen_renders_and_reverses(self):
        self._grant("reverse_document", "view_payment")
        url = url_for("payments:payment_reverse", args=[self.payment.pk])
        self.assertEqual(self.client.get(url).status_code, 200)

        response = self.client.post(
            url, {"reason": "Duplicate receipt", "reversal_date": "2091-09-10"}
        )

        self.assertEqual(response.status_code, 302)
        self.payment.refresh_from_db()
        self.assertTrue(self.payment.is_reversed)

    def test_reverse_screen_reports_the_dependency_instead_of_reversing(self):
        self._grant("reverse_document", "view_payment", "allocate_payment")
        self.allocate(self.payment, [(self.invoice, "40")])
        url = url_for("payments:payment_reverse", args=[self.payment.pk])

        response = self.client.post(
            url, {"reason": "Duplicate", "reversal_date": "2091-09-10"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "INV-V1")
        self.payment.refresh_from_db()
        self.assertFalse(self.payment.is_reversed)

    def test_a_reason_is_required_by_the_form(self):
        self._grant("reverse_document", "view_payment")
        url = url_for("payments:payment_reverse", args=[self.payment.pk])
        response = self.client.post(url, {"reason": "", "reversal_date": "2091-09-10"})
        self.assertEqual(response.status_code, 200)
        self.payment.refresh_from_db()
        self.assertFalse(self.payment.is_reversed)

    def test_allocation_reverse_screen_un_applies_the_batch(self):
        self._grant("reverse_document", "view_payment", "allocate_payment")
        key = uuid4()
        self.allocate(self.payment, [(self.invoice, "40")], key=key)
        url = url_for("payments:allocation_reverse", args=[self.payment.pk, key])

        response = self.client.post(
            url, {"reason": "Wrong invoice", "reversal_date": "2091-09-10"}
        )

        self.assertEqual(response.status_code, 302)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.open_txn, Decimal("100.0000"))

    def test_voucher_requires_view_permission(self):
        url = url_for("payments:payment_voucher", args=[self.payment.pk])
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_voucher_shows_the_receipt_and_its_allocations(self):
        self._grant("view_payment", "allocate_payment")
        self.allocate(self.payment, [(self.invoice, "100")])
        url = url_for("payments:payment_voucher", args=[self.payment.pk])

        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "RECEIPT VOUCHER")
        self.assertContains(response, self.payment.number)
        self.assertContains(response, "INV-V1")
        self.assertContains(response, "5.0000")  # the realised gain

    def test_voucher_names_a_vendor_payment_correctly(self):
        self._grant("view_payment")
        payment = self.make_payment("50", direction=PaymentDirection.PAYMENT)
        url = url_for("payments:payment_voucher", args=[payment.pk])

        response = self.client.get(url)

        self.assertContains(response, "PAYMENT VOUCHER")
        self.assertContains(response, "unapplied advance")

    def test_a_reversed_voucher_says_so(self):
        self._grant("view_payment", "reverse_document")
        reverse_payment(
            self.payment, user=self.user, reason="Bounced cheque", reversal_date=TODAY
        )
        url = url_for("payments:payment_voucher", args=[self.payment.pk])

        response = self.client.get(url)

        self.assertContains(response, "REVERSED")
        self.assertContains(response, "Bounced cheque")
