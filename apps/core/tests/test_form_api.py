"""
The endpoints the form layer calls while someone is typing.

The security property matters more than the convenience: a search endpoint that
skipped the permission check would be a way to enumerate customers without being
allowed to see the customer list. Every suggester names the permission its list
screen requires, and these tests call the URLs as users who do and do not hold
it.

    python manage.py test apps.core.tests.test_form_api
"""

import json

from django.test import TestCase

from apps.core.permissions import OWNER_ADMIN
from apps.core.suggest import build_registry
from apps.core.tests.factories import make_user
from apps.parties.models import Customer


def body(response):
    return json.loads(response.content.decode())


class SuggestPermissionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = make_user("sug-admin", OWNER_ADMIN)
        cls.nobody = make_user("sug-nobody")

    def test_anonymous_is_sent_to_sign_in(self):
        response = self.client.get("/settings/suggest/customer/?q=a")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])

    def test_a_user_without_the_permission_is_refused(self):
        """Not an empty list — that would leak whether records exist."""
        self.client.force_login(self.nobody)
        self.assertEqual(self.client.get("/settings/suggest/customer/?q=a").status_code, 403)

    def test_every_registered_kind_names_a_permission_that_exists(self):
        for kind, suggester in build_registry().items():
            with self.subTest(kind=kind):
                app_label, codename = suggester.permission.split(".")
                self.assertTrue(
                    suggester.model._meta.app_label or app_label,
                    f"{kind} has no model",
                )
                self.assertTrue(codename, f"{kind} has an empty permission")

    def test_an_unknown_kind_is_a_404_not_a_500(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get("/settings/suggest/nonsense/?q=a").status_code, 404)

    def test_post_is_refused(self):
        """These are reads. Nothing here should accept a write."""
        self.client.force_login(self.admin)
        self.assertEqual(self.client.post("/settings/suggest/customer/").status_code, 405)


class SuggestResultTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = make_user("sug-user", OWNER_ADMIN)
        cls.match = Customer.objects.create(
            code="SUG-1", name="Mohammad Trading", currency_id="USD"
        )
        cls.other = Customer.objects.create(
            code="SUG-2", name="Zenith Supplies", currency_id="USD"
        )
        cls.retired = Customer.objects.create(
            code="SUG-3", name="Mohammad Retired", currency_id="USD", is_active=False
        )

    def setUp(self):
        self.client.force_login(self.user)

    def labels(self, term):
        data = body(self.client.get(f"/settings/suggest/customer/?q={term}"))
        return [row["label"] for row in data["results"]]

    def test_partial_text_matches_the_name(self):
        self.assertTrue(any("Mohammad Trading" in label for label in self.labels("moh")))

    def test_search_is_case_insensitive(self):
        self.assertTrue(any("Mohammad Trading" in label for label in self.labels("MOHAMMAD")))

    def test_the_code_is_searchable_too(self):
        self.assertTrue(any("Mohammad Trading" in label for label in self.labels("SUG-1")))

    def test_non_matching_records_stay_out(self):
        self.assertFalse(any("Zenith" in label for label in self.labels("moh")))

    def test_inactive_records_are_not_offered(self):
        """A retired customer cannot be chosen for a new document."""
        self.assertFalse(any("Retired" in label for label in self.labels("moh")))

    def test_each_result_carries_a_line_of_context(self):
        data = body(self.client.get("/settings/suggest/customer/?q=moh"))
        row = next(r for r in data["results"] if "Mohammad Trading" in r["label"])
        self.assertIn("SUG-1", row["detail"])
        self.assertIn("USD", row["detail"])

    def test_an_empty_query_still_offers_something(self):
        """A field is usable by someone who does not know what to type."""
        self.assertTrue(body(self.client.get("/settings/suggest/customer/?q="))["results"])


class PrefillTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = make_user("pre-user", OWNER_ADMIN)
        cls.customer = Customer.objects.create(
            code="PRE-1", name="Prefill Co", currency_id="USD"
        )
        cls.held = Customer.objects.create(
            code="PRE-2",
            name="Held Co",
            currency_id="USD",
            credit_hold=True,
            credit_hold_reason="Two invoices overdue",
        )

    def setUp(self):
        self.client.force_login(self.user)

    def prefill(self, customer):
        return body(self.client.get(f"/settings/suggest/customer/{customer.pk}/prefill/"))

    def test_the_customers_currency_comes_back(self):
        self.assertEqual(self.prefill(self.customer)["values"]["currency"], "USD")

    def test_a_credit_hold_is_surfaced_as_a_warning(self):
        """The user should learn this before filling in the rest of the order."""
        notices = self.prefill(self.held)["notices"]
        self.assertTrue(notices)
        self.assertEqual(notices[0]["level"], "warning")
        self.assertIn("credit hold", notices[0]["text"])
        self.assertIn("Two invoices overdue", notices[0]["text"])

    def test_permission_is_enforced_on_prefill_as_well(self):
        stranger = make_user("pre-nobody")
        self.client.force_login(stranger)
        response = self.client.get(f"/settings/suggest/customer/{self.customer.pk}/prefill/")
        self.assertEqual(response.status_code, 403)


class BusinessCheckTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = make_user("chk-user", OWNER_ADMIN)
        cls.existing = Customer.objects.create(
            code="CHK-1", name="Existing Trading", currency_id="USD"
        )

    def setUp(self):
        self.client.force_login(self.user)

    def check(self, **params):
        from urllib.parse import urlencode

        return body(self.client.get("/settings/check/?" + urlencode(params)))

    def test_a_taken_code_is_an_error_that_names_the_holder(self):
        result = self.check(rule="customer-code", value="CHK-1")
        self.assertEqual(result["level"], "error")
        self.assertIn("Existing Trading", result["text"])

    def test_case_does_not_make_a_code_different(self):
        self.assertEqual(self.check(rule="customer-code", value="chk-1")["level"], "error")

    def test_a_free_code_is_confirmed(self):
        self.assertEqual(self.check(rule="customer-code", value="CHK-FREE")["level"], "ok")

    def test_editing_a_record_does_not_flag_its_own_code(self):
        result = self.check(rule="customer-code", value="CHK-1", exclude=self.existing.pk)
        self.assertEqual(result["level"], "ok")

    def test_a_similar_name_warns_rather_than_blocks(self):
        """PTY-007: two real companies can trade under names that look alike."""
        result = self.check(rule="similar-customer-name", value="Existing Trad")
        self.assertEqual(result["level"], "warning")
        self.assertIn("CHK-1", result["text"])

    def test_a_short_name_is_not_judged(self):
        self.assertEqual(self.check(rule="similar-customer-name", value="Ex")["level"], "ok")

    def test_an_unknown_rule_is_a_404(self):
        self.assertEqual(self.client.get("/settings/check/?rule=made-up").status_code, 404)

    def test_a_check_still_requires_the_permission_to_see_the_data(self):
        self.client.force_login(make_user("chk-nobody"))
        self.assertEqual(
            self.client.get("/settings/check/?rule=customer-code&value=CHK-1").status_code, 403
        )


class MalformedParameterTests(TestCase):
    """Junk in a query parameter should be ignored, not raise.

    These parameters reach the ORM as `pk=` / `*_id=` lookups. Django raises
    ValueError on a non-numeric one rather than a validation error it would
    turn into a 400, so `?exclude=abc` used to produce a 500. There was never an
    injection here - the ORM refuses the value long before any SQL exists - but
    a 500 is the wrong answer and it fills the logs with noise.
    """

    @classmethod
    def setUpTestData(cls):
        cls.user = make_user("malformed-user", OWNER_ADMIN)

    def setUp(self):
        self.client.force_login(self.user)

    def test_a_non_numeric_exclude_is_ignored(self):
        for value in ("abc", "1 OR 1=1", "../../etc/passwd", "%00", "1;DROP TABLE"):
            with self.subTest(exclude=value):
                response = self.client.get(
                    "/settings/check/",
                    {"rule": "customer-code", "value": "ANY-CODE", "exclude": value},
                )
                self.assertEqual(response.status_code, 200, f"{value!r} was not handled")

    def test_a_non_numeric_stock_lookup_is_ignored(self):
        response = self.client.get(
            "/settings/check/",
            {"rule": "stock", "product": "abc", "warehouse": "def", "value": "1"},
        )
        self.assertEqual(response.status_code, 200)

    def test_a_valid_exclude_still_works(self):
        """The guard must not have turned a working parameter into a no-op."""
        response = self.client.get(
            "/settings/check/",
            {"rule": "customer-code", "value": "ANY-CODE", "exclude": "1"},
        )
        self.assertEqual(response.status_code, 200)

    def test_the_stock_check_answers_at_all(self):
        """It did not: it imported a model that does not exist.

        `stock_available` imported StockOnHand from apps.inventory.models,
        where the model is called StockBalance, so every call raised
        ImportError and returned a 500. Nothing covered it, and the malformed-
        parameter test above found it by accident. This one asks the question
        directly, so it cannot come back quietly.
        """
        from apps.inventory.models import StockBalance

        balance = StockBalance.objects.select_related("product", "warehouse").first()
        if balance is None:
            self.skipTest("No stock balances to ask about.")
        response = self.client.get(
            "/settings/check/",
            {
                "rule": "stock",
                "product": balance.product_id,
                "warehouse": balance.warehouse_id,
                "value": "1",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("level", json.loads(response.content))

    def test_a_non_numeric_prefill_id_is_a_404_not_a_500(self):
        response = self.client.get("/settings/suggest/customer/not-a-number/prefill/")
        self.assertEqual(response.status_code, 404)
