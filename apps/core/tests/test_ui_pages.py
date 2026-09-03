"""
Every screen rendered end to end.

The accessibility pass promoted the record name on detail screens to <h1>
without noticing that base.html already renders one from `page_title`, so those
pages shipped with two. Nothing caught it: the template tests render partials in
isolation, and no test had ever asked a whole page what it looked like.

This walks the real URLs as a real user and asserts the handful of structural
promises the design system makes. It is deliberately shallow — it is here to
notice a page that broke, not to specify how a page looks.

    python manage.py test apps.core.tests.test_ui_pages
"""

import re

from django.test import TestCase

from apps.core.permissions import OWNER_ADMIN
from apps.core.tests.factories import make_user

#: Screens reachable without an existing record.
PAGES = [
    ("dashboard", "/"),
    ("customer list", "/parties/customers/"),
    ("customer create", "/parties/customers/new/"),
    ("vendor list", "/parties/vendors/"),
    ("vendor create", "/parties/vendors/new/"),
    ("sales order list", "/sales/orders/"),
    ("sales order create", "/sales/orders/new/"),
    ("payment list", "/payments/"),
    ("payment create", "/payments/new/"),
    ("currency list", "/settings/currencies/"),
    ("currency create", "/settings/currencies/new/"),
    ("tax code list", "/settings/tax-codes/"),
    ("tax code create", "/settings/tax-codes/new/"),
    ("payment term list", "/settings/payment-terms/"),
    ("number series list", "/settings/number-series/"),
    ("fiscal period list", "/settings/fiscal-periods/"),
    ("company settings", "/settings/company/"),
]

#: Screens that are one level in, so must offer a way back.
NEEDS_BACK_LINK = {
    "/parties/customers/new/",
    "/parties/vendors/new/",
    "/sales/orders/new/",
    "/payments/new/",
    "/settings/currencies/new/",
    "/settings/tax-codes/new/",
    "/settings/company/",
}


class EveryPageRendersTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = make_user("ui-probe", OWNER_ADMIN)

    def setUp(self):
        self.client.force_login(self.user)

    def test_every_page_returns_200(self):
        for name, url in PAGES:
            with self.subTest(page=name):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_every_page_has_exactly_one_h1(self):
        """Two <h1>s is as broken an outline as none."""
        for name, url in PAGES:
            with self.subTest(page=name):
                body = self.client.get(url).content.decode()
                self.assertEqual(
                    len(re.findall(r"<h1[\s>]", body)),
                    1,
                    f"{name} should have exactly one <h1>",
                )

    def test_inner_pages_offer_a_way_back(self):
        for name, url in PAGES:
            if url not in NEEDS_BACK_LINK:
                continue
            with self.subTest(page=name):
                body = self.client.get(url).content.decode()
                self.assertIn('class="back-link"', body, f"{name} has no return arrow")

    def test_back_links_name_their_destination(self):
        """A list of links all reading “Back” tells a screen-reader user nothing."""
        for name, url in PAGES:
            if url not in NEEDS_BACK_LINK:
                continue
            with self.subTest(page=name):
                body = self.client.get(url).content.decode()
                labels = re.findall(r'class="back-link".*?<span>([^<]+)</span>', body, re.S)
                self.assertTrue(labels, f"{name} back link has no label")
                for label in labels:
                    self.assertGreater(
                        len(label.strip()),
                        len("Back"),
                        f"{name}: “{label.strip()}” is not a place",
                    )

    def test_no_page_leaks_template_source(self):
        """
        Both halves of this matter, and only the first was checked before.

        Django's `{# #}` comment is single-line. A multi-line one is not a
        comment at all: the first line is discarded and the rest is printed to
        the page. Four of them shipped that way, one of them into a table cell
        on the sales order form, and nothing noticed because the assertion
        only looked for `{{`.
        """
        for name, url in PAGES:
            with self.subTest(page=name):
                body = self.client.get(url).content.decode()
                self.assertNotIn("{{", body, f"{name} rendered a raw template variable")
                self.assertNotIn("{#", body, f"{name} rendered a raw template comment")
                self.assertNotIn("{%", body, f"{name} rendered a raw template tag")

    def test_shared_shell_is_present_everywhere(self):
        """The icon sprite and form layer are what the components depend on."""
        for name, url in PAGES:
            with self.subTest(page=name):
                body = self.client.get(url).content.decode()
                self.assertIn('id="i-arrow-left"', body)
                self.assertIn("js/forms.js", body)

    def test_form_pages_describe_their_fields_to_the_browser(self):
        """Placeholders and validation rules reach the page, not just the mixin."""
        body = self.client.get("/parties/customers/new/").content.decode()
        self.assertIn("data-rule=", body)
        self.assertIn('placeholder="name@company.com"', body)
        self.assertIn("data-combobox", body)
