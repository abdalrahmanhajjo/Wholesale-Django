"""
Tests for Day 3 delivery services (SAL-005, INV-007).

Remaining-to-deliver, draft creation, posting, order-flip logic,
over-delivery guard, and the no-op stock-movement seam.
"""

from decimal import Decimal

from django.test import TestCase

from apps.core.audit import AuditEvent
from apps.core.models import DocumentStatus
from apps.inventory.models import StockMovement
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


class DeliveryServicesTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        make_sequence("DN", prefix="DN-")
        ensure_account_mappings()
        ensure_open_period_for_today()
        make_company()
        cls.warehouse = make_warehouse("WH-001")
        cls.customer = make_customer("DEL-C1")
        cls.product_a = make_product(sku="DEL-A", price=Decimal("100"))
        cls.product_b = make_product(sku="DEL-B", price=Decimal("250"))
        cls.user = make_user("delivery-user")
        seed_stock(
            cls.product_a, cls.warehouse, Decimal("100"), Decimal("50"), cls.user, "SEED-DEL-A"
        )
        seed_stock(
            cls.product_b, cls.warehouse, Decimal("100"), Decimal("80"), cls.user, "SEED-DEL-B"
        )

    def _make_approved_order(self, **kw):
        order = make_order(customer=self.customer, warehouse=self.warehouse, **kw)
        make_line(
            order, product=self.product_a, qty=Decimal("10"), price=Decimal("100"), line_no=1
        )
        make_line(
            order, product=self.product_b, qty=Decimal("4"), price=Decimal("250"), line_no=2
        )
        order.status = DocumentStatus.SUBMITTED
        order.save(update_fields=["status"])
        order.status = DocumentStatus.APPROVED
        order.approved_at = "2026-08-15T10:00:00Z"
        order.save(update_fields=["status", "approved_at"])
        return order

    def _post(self, note, user):
        """Convenience wrapper tested individually below."""
        return services.post_delivery(note, user)

    # ------------------------------------------------------------------
    # remaining_to_deliver
    # ------------------------------------------------------------------
    def test_remaining_before_delivery(self):
        order = self._make_approved_order()
        ln = order.lines.get(line_no=1)
        self.assertEqual(services.remaining_to_deliver(ln), Decimal("10"))

    def test_remaining_after_partial_delivery(self):
        order = self._make_approved_order()
        ln = order.lines.get(line_no=1)
        ln.quantity_delivered = Decimal("3")
        ln.save(update_fields=["quantity_delivered"])
        self.assertEqual(services.remaining_to_deliver(ln), Decimal("7"))

    def test_remaining_zero_when_fully_delivered(self):
        order = self._make_approved_order()
        ln = order.lines.get(line_no=1)
        ln.quantity_delivered = ln.quantity
        ln.save(update_fields=["quantity_delivered"])
        self.assertEqual(services.remaining_to_deliver(ln), ZERO)

    # ------------------------------------------------------------------
    # build_delivery_lines
    # ------------------------------------------------------------------
    def test_build_delivery_lines_excludes_fully_delivered(self):
        order = self._make_approved_order()
        ln1 = order.lines.get(line_no=1)
        ln1.quantity_delivered = ln1.quantity
        ln1.save(update_fields=["quantity_delivered"])

        lines = services.build_delivery_lines(order)
        pks = [ln.pk for ln, _remaining in lines]
        self.assertNotIn(ln1.pk, pks)
        self.assertEqual(len(lines), 1)

    def test_build_delivery_lines_returns_remaining(self):
        order = self._make_approved_order()
        ln = order.lines.get(line_no=2)
        ln.quantity_delivered = Decimal("1")
        ln.save(update_fields=["quantity_delivered"])

        lines = services.build_delivery_lines(order)
        # ln2 should be in the result with remaining=3
        ln2_rem = [rem for ln, rem in lines if ln.line_no == 2][0]
        self.assertEqual(ln2_rem, Decimal("3"))

    # ------------------------------------------------------------------
    # draft_delivery_from_order
    # ------------------------------------------------------------------
    def test_draft_creates_note_and_lines(self):
        order = self._make_approved_order()
        user = self.user  # use any existing user-like object
        note = services.draft_delivery_from_order(
            order=order,
            user=user,
            quantities={order.lines.get(line_no=1).pk: Decimal("5")},
        )
        self.assertEqual(note.status, DocumentStatus.DRAFT)
        self.assertEqual(note.lines.count(), 1)
        self.assertTrue(note.number.startswith("DN-"))
        self.assertEqual(note.customer, self.customer)
        self.assertEqual(note.sales_order, order)

    def test_draft_overrides_warehouse_and_date(self):
        order = self._make_approved_order()
        wh2 = make_warehouse("WH-002")
        note = services.draft_delivery_from_order(
            order=order,
            user=self.user,
            quantities={order.lines.get(line_no=1).pk: Decimal("2")},
            warehouse=wh2,
            document_date="2026-09-01",
        )
        self.assertEqual(note.warehouse, wh2)
        self.assertEqual(str(note.document_date), "2026-09-01")

    def test_draft_rejects_non_approved_order(self):
        order = make_order(customer=self.customer, warehouse=self.warehouse)
        make_line(order, qty=Decimal("5"))
        order.status = DocumentStatus.DRAFT
        order.save(update_fields=["status"])
        with self.assertRaises(ValueError) as ctx:
            services.draft_delivery_from_order(
                order=order,
                user=self.user,
                quantities={},
            )
        self.assertIn("APPROVED", str(ctx.exception))

    def test_draft_allows_partial_order_follow_up(self):
        order = self._make_approved_order()
        ln1 = order.lines.get(line_no=1)
        first = services.draft_delivery_from_order(
            order=order, user=self.user, quantities={ln1.pk: Decimal("6")}
        )
        services.post_delivery(first, self.user)
        order.refresh_from_db()
        self.assertEqual(order.status, DocumentStatus.PARTIAL)
        # A follow-up delivery on the now-PARTIAL order must be allowed.
        second = services.draft_delivery_from_order(
            order=order, user=self.user, quantities={ln1.pk: Decimal("4")}
        )
        self.assertEqual(second.sales_order, order)
        self.assertEqual(second.sales_order_id, order.pk)

    def test_draft_rejects_over_delivery(self):
        order = self._make_approved_order()
        ln = order.lines.get(line_no=1)
        with self.assertRaises(ValueError) as ctx:
            services.draft_delivery_from_order(
                order=order,
                user=self.user,
                quantities={ln.pk: ln.quantity + 1},
            )
        self.assertIn("over-delivery", str(ctx.exception).lower())

    def test_draft_rejects_empty_quantities(self):
        order = self._make_approved_order()
        with self.assertRaises(ValueError) as ctx:
            services.draft_delivery_from_order(order=order, user=self.user, quantities={})
        self.assertIn("No quantities", str(ctx.exception))

    def test_draft_records_audit_create(self):
        order = self._make_approved_order()
        ln = order.lines.get(line_no=1)
        before = AuditEvent.objects.count()
        services.draft_delivery_from_order(
            order=order, user=self.user, quantities={ln.pk: Decimal("4")}
        )
        self.assertGreater(AuditEvent.objects.count(), before)

    # ------------------------------------------------------------------
    # post_delivery
    # ------------------------------------------------------------------
    def test_post_sets_posted_status(self):
        order = self._make_approved_order()
        ln = order.lines.get(line_no=1)
        note = services.draft_delivery_from_order(
            order=order, user=self.user, quantities={ln.pk: Decimal("5")}
        )
        services.post_delivery(note, self.user)
        note.refresh_from_db()
        self.assertEqual(note.status, DocumentStatus.POSTED)
        self.assertIsNotNone(note.posted_at)
        self.assertEqual(note.posted_by, self.user)

    def test_post_increments_quantity_delivered(self):
        order = self._make_approved_order()
        ln1 = order.lines.get(line_no=1)
        note = services.draft_delivery_from_order(
            order=order, user=self.user, quantities={ln1.pk: Decimal("3")}
        )
        services.post_delivery(note, self.user)
        ln1.refresh_from_db()
        self.assertEqual(ln1.quantity_delivered, Decimal("3"))

    def test_post_partial_delivery_flips_order_to_partial(self):
        order = self._make_approved_order()
        ln1 = order.lines.get(line_no=1)
        note = services.draft_delivery_from_order(
            order=order, user=self.user, quantities={ln1.pk: Decimal("3")}
        )
        services.post_delivery(note, self.user)
        order.refresh_from_db()
        self.assertEqual(order.status, DocumentStatus.PARTIAL)

    def test_post_full_delivery_flips_order_to_completed(self):
        order = self._make_approved_order()
        lines_data = {}
        for ln in order.lines.all():
            lines_data[ln.pk] = ln.quantity
        note = services.draft_delivery_from_order(
            order=order, user=self.user, quantities=lines_data
        )
        services.post_delivery(note, self.user)
        order.refresh_from_db()
        self.assertEqual(order.status, DocumentStatus.COMPLETED)

    def test_post_rejects_already_posted(self):
        order = self._make_approved_order()
        ln = order.lines.get(line_no=1)
        note = services.draft_delivery_from_order(
            order=order, user=self.user, quantities={ln.pk: Decimal("5")}
        )
        services.post_delivery(note, self.user)
        with self.assertRaises(ValueError) as ctx:
            services.post_delivery(note, self.user)
        self.assertIn("POSTED", str(ctx.exception))

    def test_post_rejects_over_delivery_at_post_time(self):
        order = self._make_approved_order()
        ln1 = order.lines.get(line_no=1)
        ln2 = order.lines.get(line_no=2)
        # Draft a note for the full remaining of ln1 BEFORE it is consumed.
        note2 = services.draft_delivery_from_order(
            order=order,
            user=self.user,
            quantities={ln1.pk: Decimal("10")},
        )
        # Meanwhile another delivery ships ln1 entirely (order -> PARTIAL).
        note1 = services.draft_delivery_from_order(
            order=order,
            user=self.user,
            quantities={ln1.pk: Decimal("10"), ln2.pk: Decimal("4")},
        )
        services.post_delivery(note1, self.user)
        # note2 now has no remaining quantity left on ln1 at post time.
        with self.assertRaises(ValueError) as ctx:
            services.post_delivery(note2, self.user)
        self.assertIn("can deliver at most", str(ctx.exception))

    def test_post_records_audit_event(self):
        order = self._make_approved_order()
        ln = order.lines.get(line_no=1)
        note = services.draft_delivery_from_order(
            order=order, user=self.user, quantities={ln.pk: Decimal("5")}
        )
        before = AuditEvent.objects.count()
        services.post_delivery(note, self.user)
        self.assertGreater(AuditEvent.objects.count(), before)

    # ------------------------------------------------------------------
    # idempotency: posting twice is blocked
    # ------------------------------------------------------------------
    def test_post_idempotency_guard(self):
        order = self._make_approved_order()
        ln = order.lines.get(line_no=1)
        note = services.draft_delivery_from_order(
            order=order, user=self.user, quantities={ln.pk: Decimal("5")}
        )
        services.post_delivery(note, self.user)
        note.refresh_from_db()
        self.assertEqual(note.status, DocumentStatus.POSTED)
        with self.assertRaises(ValueError):
            services.post_delivery(note, self.user)

    # ------------------------------------------------------------------
    # stock movement: posting writes through Member 2's costing engine
    # ------------------------------------------------------------------
    def test_post_writes_stock_movement(self):
        order = self._make_approved_order()
        ln = order.lines.get(line_no=1)
        note = services.draft_delivery_from_order(
            order=order, user=self.user, quantities={ln.pk: Decimal("5")}
        )
        before = StockMovement.objects.count()
        services.post_delivery(note, self.user)
        self.assertEqual(StockMovement.objects.count(), before + 1)
        movement = StockMovement.objects.get(
            source_doc_number=note.number, product=self.product_a
        )
        self.assertEqual(movement.movement_type, "DELIVERY")
        self.assertEqual(movement.quantity, Decimal("5"))

    # ------------------------------------------------------------------
    # partial delivery: two notes on same order
    # ------------------------------------------------------------------
    def test_two_partials_then_full_delivery(self):
        order = self._make_approved_order()
        ln1 = order.lines.get(line_no=1)
        ln2 = order.lines.get(line_no=2)

        # First partial
        n1 = services.draft_delivery_from_order(
            order=order,
            user=self.user,
            quantities={ln1.pk: Decimal("6")},
        )
        services.post_delivery(n1, self.user)
        order.refresh_from_db()
        self.assertEqual(order.status, DocumentStatus.PARTIAL)

        # Second partial
        n2 = services.draft_delivery_from_order(
            order=order,
            user=self.user,
            quantities={ln1.pk: Decimal("4"), ln2.pk: Decimal("4")},
        )
        services.post_delivery(n2, self.user)
        order.refresh_from_db()
        self.assertEqual(order.status, DocumentStatus.COMPLETED)

        ln1.refresh_from_db()
        ln2.refresh_from_db()
        self.assertEqual(ln1.quantity_delivered, Decimal("10"))
        self.assertEqual(ln2.quantity_delivered, Decimal("4"))
