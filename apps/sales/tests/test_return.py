"""
Tests for Day 6 sales-return services and views (RET-001..RET-009).

Run:  python manage.py test apps.sales.tests.test_return --keepdb
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
    make_user,
    make_warehouse,
    seed_stock,
)

ZERO = Decimal("0")


class ReturnServicesTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        make_sequence("SO", prefix="SO-")
        make_sequence("DN", prefix="DN-")
        make_sequence("SI", prefix="INV-")
        make_sequence("SR", prefix="SRT-")
        make_sequence("JE", prefix="JV-")
        ensure_account_mappings()
        ensure_open_period_for_today()
        make_company()
        cls.warehouse = make_warehouse("WH-RET1")
        cls.customer = make_customer("RET-C1")
        cls.product_a = make_product(sku="RET-A", price=Decimal("100"))
        cls.product_b = make_product(sku="RET-B", price=Decimal("250"))
        cls.user = make_user("return-user")
        seed_stock(
            cls.product_a,
            cls.warehouse,
            Decimal("100"),
            Decimal("50"),
            cls.user,
            "SEED-RET-A",
        )
        seed_stock(
            cls.product_b,
            cls.warehouse,
            Decimal("100"),
            Decimal("80"),
            cls.user,
            "SEED-RET-B",
        )

    def _make_posted_invoice(self):
        """Approved order → posted delivery → posted invoice."""
        order = make_order(customer=self.customer, warehouse=self.warehouse)
        make_line(
            order, product=self.product_a, qty=Decimal("10"), price=Decimal("100"), line_no=1
        )
        make_line(
            order, product=self.product_b, qty=Decimal("4"), price=Decimal("250"), line_no=2
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
    # remaining_to_return
    # ------------------------------------------------------------------
    def test_remaining_before_return(self):
        invoice = self._make_posted_invoice()
        il = invoice.lines.get(line_no=1)
        self.assertEqual(services.remaining_to_return(il), Decimal("10"))

    def test_remaining_after_partial_return(self):
        invoice = self._make_posted_invoice()
        il = invoice.lines.get(line_no=1)
        il.quantity_returned = Decimal("3")
        il.save(update_fields=["quantity_returned"])
        self.assertEqual(services.remaining_to_return(il), Decimal("7"))

    # ------------------------------------------------------------------
    # draft_return_from_invoice
    # ------------------------------------------------------------------
    def test_draft_creates_return_with_lines(self):
        invoice = self._make_posted_invoice()
        il = invoice.lines.get(line_no=1)
        ret = services.draft_return_from_invoice(
            invoice=invoice,
            user=self.user,
            quantities={il.pk: Decimal("3")},
            reason="Customer changed mind",
        )
        self.assertEqual(ret.status, DocumentStatus.DRAFT)
        self.assertEqual(ret.lines.count(), 1)
        self.assertEqual(ret.lines.first().quantity, Decimal("3"))
        self.assertEqual(ret.original_invoice, invoice)

    def test_draft_increments_quantity_returned(self):
        invoice = self._make_posted_invoice()
        il = invoice.lines.get(line_no=1)
        services.draft_return_from_invoice(
            invoice=invoice,
            user=self.user,
            quantities={il.pk: Decimal("3")},
            reason="Defective",
        )
        il.refresh_from_db()
        self.assertEqual(il.quantity_returned, Decimal("3"))

    def test_draft_rejects_over_return(self):
        invoice = self._make_posted_invoice()
        il = invoice.lines.get(line_no=1)
        with self.assertRaises(ValueError) as ctx:
            services.draft_return_from_invoice(
                invoice=invoice,
                user=self.user,
                quantities={il.pk: Decimal("20")},
                reason="Too many",
            )
        self.assertIn("can return at most", str(ctx.exception))

    def test_draft_rejects_unposted_invoice(self):
        order = make_order(customer=self.customer, warehouse=self.warehouse)
        make_line(order, product=self.product_a, qty=Decimal("5"), price=Decimal("100"))
        order.status = DocumentStatus.APPROVED
        order.save(update_fields=["status"])
        # A DRAFT invoice cannot be a return source (RET-001).
        from apps.sales.models import SalesInvoice

        invoice = SalesInvoice.objects.create(
            number="INV-DRAFT-1",
            document_date="2026-09-01",
            posting_date="2026-09-01",
            customer=self.customer,
            warehouse=self.warehouse,
            currency=order.currency,
            exchange_rate=Decimal("1"),
            status=DocumentStatus.DRAFT,
        )
        with self.assertRaises(ValueError) as ctx:
            services.draft_return_from_invoice(
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
        ret = services.draft_return_from_invoice(
            invoice=invoice,
            user=self.user,
            quantities={il.pk: Decimal("2")},
            reason="Defective",
        )
        services.submit_return(ret, self.user)
        ret.refresh_from_db()
        self.assertEqual(ret.status, DocumentStatus.SUBMITTED)
        self.assertIsNotNone(ret.submitted_at)
        self.assertEqual(ret.submitted_by, self.user)

    def test_approve_moves_to_approved(self):
        invoice = self._make_posted_invoice()
        il = invoice.lines.get(line_no=1)
        ret = services.draft_return_from_invoice(
            invoice=invoice,
            user=self.user,
            quantities={il.pk: Decimal("2")},
            reason="Defective",
        )
        services.submit_return(ret, self.user)
        services.approve_return(ret, self.user, reason="Approved")
        ret.refresh_from_db()
        self.assertEqual(ret.status, DocumentStatus.APPROVED)
        self.assertIsNotNone(ret.approved_at)
        self.assertEqual(ret.approval_reason, "Approved")

    def test_reject_moves_to_rejected(self):
        invoice = self._make_posted_invoice()
        il = invoice.lines.get(line_no=1)
        ret = services.draft_return_from_invoice(
            invoice=invoice,
            user=self.user,
            quantities={il.pk: Decimal("2")},
            reason="Defective",
        )
        services.submit_return(ret, self.user)
        services.reject_return(ret, self.user, reason="Not valid")
        ret.refresh_from_db()
        self.assertEqual(ret.status, DocumentStatus.REJECTED)
        self.assertEqual(ret.approval_reason, "Not valid")

    # ------------------------------------------------------------------
    # journal / post
    # ------------------------------------------------------------------
    def test_journal_is_balanced(self):
        invoice = self._make_posted_invoice()
        il = invoice.lines.get(line_no=1)
        ret = services.draft_return_from_invoice(
            invoice=invoice,
            user=self.user,
            quantities={il.pk: Decimal("5")},
            reason="Defective",
        )
        services.submit_return(ret, self.user)
        draft = services.build_sales_return_journal(ret, user=self.user)
        total_debit = sum(ln.debit_base for ln in draft.lines)
        total_credit = sum(ln.credit_base for ln in draft.lines)
        self.assertEqual(total_debit, total_credit)
        self.assertGreater(total_debit, ZERO)

    def test_post_persists_journal(self):
        invoice = self._make_posted_invoice()
        il = invoice.lines.get(line_no=1)
        ret = services.draft_return_from_invoice(
            invoice=invoice,
            user=self.user,
            quantities={il.pk: Decimal("5")},
            reason="Defective",
        )
        services.submit_return(ret, self.user)
        posted = services.post_return(ret, self.user)
        posted.refresh_from_db()
        self.assertEqual(posted.status, DocumentStatus.POSTED)
        self.assertIsNotNone(posted.journal_entry)
        self.assertIsNotNone(posted.posted_by)
        self.assertIsNotNone(posted.posted_at)

    def test_post_requires_submitted(self):
        invoice = self._make_posted_invoice()
        il = invoice.lines.get(line_no=1)
        ret = services.draft_return_from_invoice(
            invoice=invoice,
            user=self.user,
            quantities={il.pk: Decimal("2")},
            reason="Defective",
        )
        with self.assertRaises(ValueError):
            services.post_return(ret, self.user)

    def test_post_idempotency(self):
        invoice = self._make_posted_invoice()
        il = invoice.lines.get(line_no=1)
        ret = services.draft_return_from_invoice(
            invoice=invoice,
            user=self.user,
            quantities={il.pk: Decimal("5")},
            reason="Defective",
        )
        services.submit_return(ret, self.user)
        services.post_return(ret, self.user)
        with self.assertRaises(ValueError):
            services.post_return(ret, self.user)
