"""Guards against N+1 regressions on the document detail screens.

These assert a *property* rather than a magic number: rendering a document with
five lines must not cost more queries than rendering one with a single line.
An absolute count would break every time an unrelated context lookup is added,
and would tell nobody what the rule actually is.

This matters more here than in most projects. The production database is a
remote Supabase region, and a measured round trip is roughly 300ms, so one
query per line turns a twenty-line invoice into six seconds of page load.
"""

from decimal import Decimal

from django.contrib.auth.models import Permission
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.accounts.models import User
from apps.sales.tests import factories as f


class DetailViewQueryCountTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="query-count-auditor",
            email="query-count@example.com",
            password="x-test-password",
        )
        for codename in ("view_salesorder", "view_salesinvoice"):
            cls.user.user_permissions.add(Permission.objects.get(codename=codename))
        cls.customer = f.make_customer(code="C-QUERY")
        cls.warehouse = f.make_warehouse(code="WH-QUERY")

    def setUp(self):
        self.client.force_login(self.user)
        # SESSION_ENGINE is cached_db, so the very first authenticated request
        # of a test reads the session from the database and later ones read it
        # from the local-memory cache. Measuring without burning that off makes
        # the first render look one query more expensive than every render
        # after it, which has nothing to do with what is being tested here.
        self._warm_up()

    def _warm_up(self):
        warm = self._order_with(1)
        self.client.get(reverse("sales:so_detail", args=[warm.pk]))

    def _order_with(self, line_count):
        order = f.make_order(customer=self.customer, warehouse=self.warehouse)
        for index in range(line_count):
            # A distinct product per line: reusing one would let Django's
            # per-query cache hide the very N+1 this is meant to catch.
            f.make_line(
                order,
                product=f.make_product(sku=f"P-QUERY-{order.pk}-{index}"),
                qty=Decimal("1"),
                line_no=index + 1,
            )
        return order

    def _queries_rendering(self, order):
        url = reverse("sales:so_detail", args=[order.pk])
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return len(captured)

    def test_order_detail_does_not_query_per_line(self):
        one_line = self._queries_rendering(self._order_with(1))
        five_lines = self._queries_rendering(self._order_with(5))

        self.assertLessEqual(
            five_lines,
            one_line,
            "Rendering a five-line order cost "
            f"{five_lines - one_line} more queries than a one-line order, so "
            "something in so_detail.html is being fetched per line. Check the "
            "prefetch_related on SalesOrderDetailView.get_queryset().",
        )
