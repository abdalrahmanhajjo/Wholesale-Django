"""
Sales-order service tests.

Covers the business logic in apps/sales/services.py:
  - number generation (NFR-008 concurrency-safe allocation)
  - line arithmetic  (SAL-002, BR-010, BR-011)  exclusive and inclusive tax
  - document-level discount allocation (SAL-003)
  - header totals and rounding (BR-022)
  - approval lifecycle (SAL-004) incl. resubmit from REJECTED

Run:  python manage.py test apps.sales.tests.test_services
"""

from decimal import Decimal

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from apps.core.models import AuditAction, AuditEvent, DocumentSequence, DocumentStatus
from apps.sales import services
from apps.sales.models import DiscountKind
from apps.sales.tests import factories as f

ZERO = Decimal("0")


class NumberingTests(TestCase):
    def test_allocate_next_numbers_sequentially(self):
        f.make_sequence(prefix="SO-", padding=5)
        n1 = services.allocate_so_number()
        n2 = services.allocate_so_number()
        self.assertEqual(n1, "SO-00001")
        self.assertEqual(n2, "SO-00002")
        # Sequence row advanced
        seq = DocumentSequence.objects.get(document_type="SO")
        self.assertEqual(Decimal(seq.next_number), Decimal(3))

    def test_allocate_raises_without_active_sequence(self):
        DocumentSequence.objects.filter(document_type="SO").update(is_active=False)
        with self.assertRaises(ValueError):
            services.allocate_so_number()


class LineArithmeticTests(TestCase):
    """SAL-002 / BR-010 / BR-011: gross -> net -> taxable -> tax -> total."""

    def setUp(self):
        self.order = f.make_order()
        self.tax = f.make_tax(rate=Decimal("11.0"))

    def test_exclusive_tax_exact(self):
        # qty 10 x price 20 = gross 200; 10% discount -> discount 20, net 180
        line = f.make_line(
            self.order,
            qty=Decimal("10"),
            price=Decimal("20"),
            discount=Decimal("10"),
            tax=self.tax,
        )
        services.calculate_line(line)

        self.assertEqual(line.gross_txn, Decimal("200.0000"))
        self.assertEqual(line.line_discount_txn, Decimal("20.0000"))
        self.assertEqual(line.net_txn, Decimal("180.0000"))
        # exclusive tax: taxable base = net = 180; tax = 180 * 11% = 19.8
        self.assertEqual(line.taxable_base_txn, Decimal("180.0000"))
        self.assertEqual(line.tax_txn, Decimal("19.8000"))
        self.assertEqual(line.total_txn, Decimal("199.8000"))

    def test_inclusive_tax_backs_out(self):
        # price includes tax: taxable base = net / (1 + rate/100)
        tax = f.make_tax(code="VAT-INC", rate=Decimal("11.0"), is_inclusive=True)
        line = f.make_line(self.order, qty=Decimal("1"), price=Decimal("111"), tax=tax)
        services.calculate_line(line)

        self.assertEqual(line.gross_txn, Decimal("111.0000"))
        self.assertEqual(line.net_txn, Decimal("111.0000"))  # price incl. tax
        taxable = (Decimal("111") / Decimal("1.11")).quantize(Decimal("0.0001"))
        self.assertEqual(line.taxable_base_txn, taxable)
        self.assertEqual(line.tax_txn, Decimal("11.0000"))
        self.assertEqual(line.total_txn, Decimal("111.0000"))

    def test_line_discount_within_gross(self):
        # A 100% discount leaves net zero, never negative.
        line = f.make_line(
            self.order,
            qty=Decimal("1"),
            price=Decimal("100"),
            discount=Decimal("100"),
        )
        services.calculate_line(line)
        self.assertEqual(line.line_discount_txn, line.gross_txn)
        self.assertEqual(line.net_txn, ZERO)

    def test_allocated_document_discount_does_not_make_net_negative(self):
        # Even if a line's allocated document discount exceeds its own gross,
        # net is clamped at zero (BR-010 / FTD-008).
        line = f.make_line(self.order, qty=Decimal("1"), price=Decimal("100"))
        line.allocated_document_discount_txn = Decimal("150.0000")  # > gross 100
        services.calculate_line(line)
        self.assertGreaterEqual(line.net_txn, ZERO)


class DocumentDiscountAllocationTests(TestCase):
    """SAL-003: header discount spread across lines by gross proportion."""

    def setUp(self):
        self.order = f.make_order()

    def test_proportional_split(self):
        # Two lines: gross 100 and 300 -> total 400. Allocate $40 header discount.
        a = f.make_product("P-A", price=Decimal("100"))
        b = f.make_product("P-B", price=Decimal("100"))
        f.make_line(self.order, product=a, qty=Decimal("1"), price=Decimal("100"), line_no=1)
        f.make_line(self.order, product=b, qty=Decimal("3"), price=Decimal("100"), line_no=2)

        self.order.document_discount_kind = DiscountKind.AMOUNT
        self.order.document_discount_value = Decimal("40")
        self.order.save()
        self.order.document_discount_txn = Decimal("40.0000")
        self.order.save()

        services.allocate_document_discount(self.order)
        for ln in self.order.lines.all():
            ln.save()

        la, lb = self.order.lines.order_by("line_no")
        # A gets 25% (100/400) of 40 = 10; B gets 30
        self.assertEqual(la.allocated_document_discount_txn, Decimal("10.0000"))
        self.assertEqual(lb.allocated_document_discount_txn, Decimal("30.0000"))
        # Allocations reconcile to the header discount
        total = la.allocated_document_discount_txn + lb.allocated_document_discount_txn
        self.assertEqual(total, Decimal("40.0000"))

    def test_zero_discount_clears_allocations(self):
        line = f.make_line(self.order, qty=Decimal("1"), price=Decimal("100"))
        line.allocated_document_discount_txn = Decimal("15.0000")
        line.save()

        self.order.document_discount_txn = ZERO
        services.allocate_document_discount(self.order)
        for ln in self.order.lines.all():
            ln.save()
        line.refresh_from_db()
        self.assertEqual(line.allocated_document_discount_txn, ZERO)

    def test_percentage_discount_is_derived_from_entered_header_value(self):
        f.make_line(self.order, qty=Decimal("2"), price=Decimal("100"))
        self.order.document_discount_kind = DiscountKind.PERCENT
        self.order.document_discount_value = Decimal("10")
        self.order.save(update_fields=["document_discount_kind", "document_discount_value"])

        services.recalculate_order(self.order)

        self.order.refresh_from_db()
        line = self.order.lines.get()
        self.assertEqual(self.order.document_discount_txn, Decimal("20.0000"))
        self.assertEqual(line.allocated_document_discount_txn, Decimal("20.0000"))
        self.assertEqual(self.order.total_txn, Decimal("180.0000"))

    def test_recalculation_bulk_updates_all_lines_once(self):
        for line_no in range(1, 4):
            product = f.make_product(f"P-BULK-{line_no}")
            f.make_line(self.order, product=product, line_no=line_no)

        with CaptureQueriesContext(connection) as captured:
            services.recalculate_order(self.order)

        line_updates = [
            query["sql"]
            for query in captured.captured_queries
            if query["sql"].lstrip().upper().startswith('UPDATE "SALES_ORDER_LINE"')
        ]
        self.assertEqual(len(line_updates), 1)


class RecalculateTotalsTests(TestCase):
    """BR-022: header totals roll up from lines and reconcile to the header."""

    def test_totals_roll_up(self):
        order = f.make_order()
        tax = f.make_tax(rate=Decimal("11.0"))
        line = f.make_line(order, qty=Decimal("2"), price=Decimal("100"), tax=tax, line_no=1)
        services.calculate_line(line)
        line.save()
        services.calculate_totals(order)

        # gross 200, tax 22, total 222
        self.assertEqual(order.subtotal_txn, Decimal("200.0000"))
        self.assertEqual(order.tax_txn, Decimal("22.0000"))
        self.assertEqual(order.total_txn, Decimal("222.0000"))
        self.assertEqual(order.total_base, Decimal("222.0000"))
        self.assertEqual(order.open_txn, Decimal("222.0000"))

    def test_recalculation_snapshots_selected_tax_code(self):
        order = f.make_order()
        tax = f.make_tax(code="VAT-SNAPSHOT", rate=Decimal("11.0"))
        line = f.make_line(order, qty=Decimal("1"), price=Decimal("100"))
        line.tax_code = tax
        line.tax_rate_percent = ZERO
        line.save(update_fields=["tax_code", "tax_rate_percent"])

        services.recalculate_order(order)

        line.refresh_from_db()
        self.assertEqual(line.tax_rate_percent, Decimal("11.0000"))
        self.assertEqual(line.tax_txn, Decimal("11.0000"))


class ApprovalWorkflowTests(TestCase):
    """SAL-004 lifecycle: submit, approve, reject, and resubmit from rejected."""

    def setUp(self):
        from apps.accounts.models import User

        self.user = User.objects.create_user(
            username="sales-user", email="s@example.com", password="x-1234567"
        )

    def test_draft_can_submit(self):
        order = f.make_order()
        services.submit_order(order, self.user)
        self.assertEqual(order.status, DocumentStatus.SUBMITTED)
        self.assertIsNotNone(order.submitted_at)

    def test_cannot_submit_non_draft(self):
        order = f.make_order()
        order.status = DocumentStatus.APPROVED
        order.save()
        with self.assertRaises(ValueError):
            services.submit_order(order, self.user)

    def test_submitted_can_approve(self):
        order = f.make_order()
        services.submit_order(order, self.user)
        services.approve_order(order, self.user, reason="Looks good")
        self.assertEqual(order.status, DocumentStatus.APPROVED)
        self.assertEqual(order.approved_by, self.user)
        self.assertEqual(order.approval_reason, "Looks good")
        event = AuditEvent.objects.get(object_id=order.pk, action=AuditAction.APPROVE)
        self.assertEqual(event.user, self.user)

    def test_approve_requires_reason(self):
        order = f.make_order()
        services.submit_order(order, self.user)
        with self.assertRaises(ValueError):
            services.approve_order(order, self.user, reason="")
        self.assertEqual(order.status, DocumentStatus.SUBMITTED)

    def test_reject_requires_reason(self):
        order = f.make_order()
        services.submit_order(order, self.user)
        with self.assertRaises(ValueError):
            services.reject_order(order, self.user, reason="")
        self.assertEqual(order.status, DocumentStatus.SUBMITTED)  # unchanged

    def test_rejected_can_resubmit(self):
        order = f.make_order()
        services.submit_order(order, self.user)
        services.reject_order(order, self.user, reason="Fix the price")
        self.assertEqual(order.status, DocumentStatus.REJECTED)
        # Editable + resubmittable (EDITABLE_STATES)
        services.submit_order(order, self.user)
        self.assertEqual(order.status, DocumentStatus.SUBMITTED)

    def test_cannot_approve_unsubmitted(self):
        order = f.make_order()
        with self.assertRaises(ValueError):
            services.approve_order(order, self.user, reason="x")
