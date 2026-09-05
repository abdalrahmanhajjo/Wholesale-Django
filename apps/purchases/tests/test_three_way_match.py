"""
PurchaseOrderLine.match_status (PUR-003 / PUR-012 three-way match indicator).

Pure model-logic test — no DB needed, since match_status only reads the
in-memory quantity fields.
"""

from decimal import Decimal

from django.test import SimpleTestCase

from apps.purchases.models import PurchaseOrderLine, ThreeWayMatchStatus


def _line(quantity, received=0, billed=0, cancelled=0):
    return PurchaseOrderLine(
        quantity=Decimal(quantity),
        quantity_received=Decimal(received),
        quantity_billed=Decimal(billed),
        quantity_cancelled=Decimal(cancelled),
    )


class ThreeWayMatchStatusTests(SimpleTestCase):
    def test_nothing_received_or_billed_is_open(self):
        self.assertEqual(_line(10).match_status, ThreeWayMatchStatus.OPEN)

    def test_some_received_none_billed_is_partial(self):
        self.assertEqual(_line(10, received=5).match_status, ThreeWayMatchStatus.PARTIAL)

    def test_fully_received_but_not_billed_is_partial(self):
        self.assertEqual(_line(10, received=10).match_status, ThreeWayMatchStatus.PARTIAL)

    def test_fully_received_and_billed_is_matched(self):
        line = _line(10, received=10, billed=10)
        self.assertEqual(line.match_status, ThreeWayMatchStatus.MATCHED)
        self.assertEqual(line.get_match_status_display(), "Matched")

    def test_billed_more_than_received_is_over(self):
        self.assertEqual(
            _line(10, received=5, billed=8).match_status, ThreeWayMatchStatus.OVER
        )

    def test_fully_cancelled_line_is_cancelled_even_if_untouched(self):
        line = _line(10, cancelled=10)
        self.assertEqual(line.match_status, ThreeWayMatchStatus.CANCELLED)

    def test_partially_cancelled_remainder_still_tracked(self):
        # 10 ordered, 4 cancelled -> 6 still open; all 6 received and billed matches.
        line = _line(10, received=6, billed=6, cancelled=4)
        self.assertEqual(line.match_status, ThreeWayMatchStatus.MATCHED)
