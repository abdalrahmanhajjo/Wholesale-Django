"""
Tests for Day 7 sales credit-note services (RET-003, RET-004, SAL-007).

Run:  python manage.py test apps.sales.tests.test_credit_note --keepdb
"""

from decimal import Decimal

from django.test import TestCase

from apps.core.models import DocumentStatus
from apps.sales import services
from apps.sales.tests.factories import (
    ensure_account_mappings,
    ensure_open_period_for_today,
    make_company,
    make_customer,
    make_line,
    make_order,
    make_product,
    make_sequence,
    make_tax,
    make_user,
    make_warehouse,
    seed_stock,
)

ZERO = Decimal("0")


class CreditNoteServicesTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        make_sequence("SO", prefix="SO-")
        make_sequence("DN", prefix="DN-")
        make_sequence("SI", prefix="INV-")
        make_sequence("SR", prefix="SRT-")
        make_sequence("CN", prefix="CN-")
        make_sequence("JE", prefix="JV-")
        ensure_account_mappings()
        ensure_open_period_for_today()
        make_company()
        cls.warehouse = make_warehouse("WH-CN1")
        cls.customer = make_customer("CN-C1")
        cls.product_a = make_product(sku="CN-A", price=Decimal("100"))
        cls.product_b = make_product(sku="CN-B", price=Decimal("250"))
        cls.user = make_user("credit-note-user")
        seed_stock(
            cls.product_a,
            cls.warehouse,
            Decimal("100"),
            Decimal("50"),
            cls.user,
            "SEED-CN-A",
        )
        seed_stock(
            cls.product_b,
            cls.warehouse,
            Decimal("100"),
            Decimal("80"),
            cls.user,
            "SEED-CN-B",
        )

    def _make_posted_invoice(self, tax=None):
        """Approved order → posted delivery → posted invoice."""
        order = make_order(customer=self.customer, warehouse=self.warehouse)
        make_line(
            order,
            product=self.product_a,
            qty=Decimal("10"),
            price=Decimal("100"),
            line_no=1,
            tax=tax,
        )
        make_line(
            order,
            product=self.product_b,
            qty=Decimal("4"),
            price=Decimal("250"),
            line_no=2,
            tax=tax,
        )
        order.status = DocumentStatus.APPROVED
        order.approved_at = "2026-08-15T10:00:00Z"
        order.save(update_fields=["status", "approved_at"])

        note = services.draft_delivery_from_order(
            order=order,
            user=self.user,
            quantities={
                order.lines.get(line_no=1).pk: Decimal("10"),
                order.lines.get(line_no=2).pk: Decimal("4"),
            },
        )
        services.post_delivery(note, self.user)

        quantities = {
            dl.pk: dl.quantity
            for dl in note.lines.select_related("product")
            if services.remaining_to_invoice(dl) > ZERO
        }
        invoice = services.create_invoice_from_delivery(
            delivery=note, user=self.user, quantities=quantities
        )
        services.submit_invoice(invoice, self.user)
        services.post_invoice(invoice, self.user)
        return invoice

    # ------------------------------------------------------------------
    # draft_credit_note_from_invoice
    # ------------------------------------------------------------------
    def test_draft_creates_credit_note_with_lines(self):
        invoice = self._make_posted_invoice()
        il = invoice.lines.get(line_no=1)
        cn = services.draft_credit_note_from_invoice(
            invoice=invoice,
            user=self.user,
            quantities={il.pk: Decimal("3")},
            reason="Customer changed mind",
        )
        self.assertEqual(cn.status, DocumentStatus.DRAFT)
        self.assertEqual(cn.lines.count(), 1)
        line = cn.lines.first()
        self.assertEqual(line.quantity, Decimal("3"))
        self.assertEqual(line.invoice_line, il)
        self.assertEqual(line.unit_price, il.unit_price)
        self.assertEqual(cn.original_invoice, invoice)
        self.assertEqual(cn.customer, invoice.customer)
        self.assertEqual(cn.total_txn, Decimal("300.00"))

    def test_draft_does_not_touch_return_eligibility(self):
        invoice = self._make_posted_invoice()
        il = invoice.lines.get(line_no=1)
        services.draft_credit_note_from_invoice(
            invoice=invoice,
            user=self.user,
            quantities={il.pk: Decimal("3")},
            reason="Price dispute",
        )
        il.refresh_from_db()
        self.assertEqual(il.quantity_returned, ZERO)

    def test_draft_rejects_over_credit(self):
        invoice = self._make_posted_invoice()
        il = invoice.lines.get(line_no=1)
        with self.assertRaises(ValueError) as ctx:
            services.draft_credit_note_from_invoice(
                invoice=invoice,
                user=self.user,
                quantities={il.pk: Decimal("20")},
                reason="Too much",
            )
        self.assertIn("cannot credit more", str(ctx.exception))

    def test_draft_requires_reason(self):
        invoice = self._make_posted_invoice()
        il = invoice.lines.get(line_no=1)
        with self.assertRaises(ValueError) as ctx:
            services.draft_credit_note_from_invoice(
                invoice=invoice,
                user=self.user,
                quantities={il.pk: Decimal("2")},
                reason="   ",
            )
        self.assertIn("reason", str(ctx.exception))

    def test_draft_rejects_unposted_invoice(self):
        from apps.sales.models import SalesInvoice

        invoice = SalesInvoice.objects.create(
            number="INV-DRAFT-CN",
            document_date="2026-09-01",
            posting_date="2026-09-01",
            customer=self.customer,
            warehouse=self.warehouse,
            currency=self.customer.currency,
            exchange_rate=Decimal("1"),
            status=DocumentStatus.DRAFT,
        )
        with self.assertRaises(ValueError) as ctx:
            services.draft_credit_note_from_invoice(
                invoice=invoice,
                user=self.user,
                quantities={},
                reason="test",
            )
        self.assertIn("DRAFT", str(ctx.exception))

    # ------------------------------------------------------------------
    # submit / approve / reject
    # ------------------------------------------------------------------
    def test_submit_moves_to_submitted(self):
        invoice = self._make_posted_invoice()
        il = invoice.lines.get(line_no=1)
        cn = services.draft_credit_note_from_invoice(
            invoice=invoice,
            user=self.user,
            quantities={il.pk: Decimal("2")},
            reason="Defective",
        )
        services.submit_credit_note(cn, self.user)
        cn.refresh_from_db()
        self.assertEqual(cn.status, DocumentStatus.SUBMITTED)
        self.assertIsNotNone(cn.submitted_at)
        self.assertEqual(cn.submitted_by, self.user)

    def test_approve_moves_to_approved(self):
        invoice = self._make_posted_invoice()
        il = invoice.lines.get(line_no=1)
        cn = services.draft_credit_note_from_invoice(
            invoice=invoice,
            user=self.user,
            quantities={il.pk: Decimal("2")},
            reason="Defective",
        )
        services.submit_credit_note(cn, self.user)
        services.approve_credit_note(cn, self.user, reason="Approved")
        cn.refresh_from_db()
        self.assertEqual(cn.status, DocumentStatus.APPROVED)
        self.assertIsNotNone(cn.approved_at)
        self.assertEqual(cn.approval_reason, "Approved")

    def test_reject_moves_to_rejected(self):
        invoice = self._make_posted_invoice()
        il = invoice.lines.get(line_no=1)
        cn = services.draft_credit_note_from_invoice(
            invoice=invoice,
            user=self.user,
            quantities={il.pk: Decimal("2")},
            reason="Defective",
        )
        services.submit_credit_note(cn, self.user)
        services.reject_credit_note(cn, self.user, reason="Not valid")
        cn.refresh_from_db()
        self.assertEqual(cn.status, DocumentStatus.REJECTED)
        self.assertEqual(cn.approval_reason, "Not valid")

    def test_reject_requires_reason(self):
        invoice = self._make_posted_invoice()
        il = invoice.lines.get(line_no=1)
        cn = services.draft_credit_note_from_invoice(
            invoice=invoice,
            user=self.user,
            quantities={il.pk: Decimal("2")},
            reason="Defective",
        )
        services.submit_credit_note(cn, self.user)
        with self.assertRaises(ValueError):
            services.reject_credit_note(cn, self.user, reason="")

    # ------------------------------------------------------------------
    # journal / post
    # ------------------------------------------------------------------
    def test_journal_is_balanced(self):
        invoice = self._make_posted_invoice()
        il = invoice.lines.get(line_no=1)
        cn = services.draft_credit_note_from_invoice(
            invoice=invoice,
            user=self.user,
            quantities={il.pk: Decimal("5")},
            reason="Defective",
        )
        draft = services.build_sales_credit_note_journal(cn, user=self.user)
        total_debit = sum(ln.debit_base for ln in draft.lines)
        total_credit = sum(ln.credit_base for ln in draft.lines)
        self.assertEqual(total_debit, total_credit)
        self.assertGreater(total_debit, ZERO)

    def test_journal_credits_ar(self):
        invoice = self._make_posted_invoice()
        il = invoice.lines.get(line_no=1)
        cn = services.draft_credit_note_from_invoice(
            invoice=invoice,
            user=self.user,
            quantities={il.pk: Decimal("5")},
            reason="Defective",
        )
        draft = services.build_sales_credit_note_journal(cn, user=self.user)
        ar_lines = [ln for ln in draft.lines if ln.account.code == "1210"]
        self.assertEqual(len(ar_lines), 1)
        self.assertEqual(ar_lines[0].credit_base, Decimal("500.00"))

    def test_post_persists_journal(self):
        invoice = self._make_posted_invoice()
        il = invoice.lines.get(line_no=1)
        cn = services.draft_credit_note_from_invoice(
            invoice=invoice,
            user=self.user,
            quantities={il.pk: Decimal("5")},
            reason="Defective",
        )
        services.submit_credit_note(cn, self.user)
        posted = services.post_credit_note(cn, self.user)
        posted.refresh_from_db()
        self.assertEqual(posted.status, DocumentStatus.POSTED)
        self.assertIsNotNone(posted.journal_entry)
        self.assertIsNotNone(posted.posted_by)
        self.assertIsNotNone(posted.posted_at)

    def test_post_requires_submitted(self):
        invoice = self._make_posted_invoice()
        il = invoice.lines.get(line_no=1)
        cn = services.draft_credit_note_from_invoice(
            invoice=invoice,
            user=self.user,
            quantities={il.pk: Decimal("2")},
            reason="Defective",
        )
        with self.assertRaises(ValueError):
            services.post_credit_note(cn, self.user)

    def test_post_idempotency(self):
        invoice = self._make_posted_invoice()
        il = invoice.lines.get(line_no=1)
        cn = services.draft_credit_note_from_invoice(
            invoice=invoice,
            user=self.user,
            quantities={il.pk: Decimal("5")},
            reason="Defective",
        )
        services.submit_credit_note(cn, self.user)
        services.post_credit_note(cn, self.user)
        with self.assertRaises(ValueError):
            services.post_credit_note(cn, self.user)

    # ------------------------------------------------------------------
    # tax reversal
    # ------------------------------------------------------------------
    def test_journal_reverses_output_tax(self):
        invoice = self._make_posted_invoice(tax=make_tax())
        il = invoice.lines.get(line_no=1)
        cn = services.draft_credit_note_from_invoice(
            invoice=invoice,
            user=self.user,
            quantities={il.pk: Decimal("5")},
            reason="Defective",
        )
        draft = services.build_sales_credit_note_journal(cn, user=self.user)
        total_debit = sum(ln.debit_base for ln in draft.lines)
        total_credit = sum(ln.credit_base for ln in draft.lines)
        self.assertEqual(total_debit, total_credit)
        tax_lines = [ln for ln in draft.lines if ln.account.code == "2310"]
        self.assertEqual(len(tax_lines), 1)
        self.assertGreater(tax_lines[0].debit_base, ZERO)

    def test_post_with_output_tax(self):
        invoice = self._make_posted_invoice(tax=make_tax())
        il = invoice.lines.get(line_no=1)
        cn = services.draft_credit_note_from_invoice(
            invoice=invoice,
            user=self.user,
            quantities={il.pk: Decimal("5")},
            reason="Defective",
        )
        services.submit_credit_note(cn, self.user)
        posted = services.post_credit_note(cn, self.user)
        posted.refresh_from_db()
        self.assertEqual(posted.status, DocumentStatus.POSTED)
        self.assertIsNotNone(posted.journal_entry)
