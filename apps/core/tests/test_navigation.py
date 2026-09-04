"""The sidebar's structure, permissions and active state (UX-002).

The navigation moved out of base.html and into apps/core/navigation.py, which
makes it testable for the first time. These assert the properties the markup
used to imply and nobody could check: every destination resolves, a row is
hidden when its permission is missing, and exactly one row is marked current.
"""

import re

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import NoReverseMatch, reverse

from apps.core.navigation import SECTIONS

LABEL = re.compile(r'<span class="nav-label">([^<]+)</span>')
HREF = re.compile(r'<a href="([^"]+)"[^>]*class="nav-link')


def all_items():
    for section in SECTIONS:
        yield from section.items
        for group in section.subgroups:
            yield from group.items


class StructureTests(TestCase):
    def test_every_destination_resolves(self):
        """A sidebar that links to a name Django cannot reverse is a 500."""
        for item in all_items():
            with self.subTest(item=item.label):
                try:
                    self.assertTrue(reverse(item.url_name))
                except NoReverseMatch as exc:  # pragma: no cover - a real break
                    self.fail(f"{item.label} -> {item.url_name}: {exc}")

    def test_no_row_is_a_dead_link(self):
        for item in all_items():
            with self.subTest(item=item.label):
                self.assertTrue(item.url_name)
                self.assertNotIn("#", item.url_name)

    def test_every_row_can_become_active(self):
        """A row with no match rule can never highlight, so it would look broken."""
        for item in all_items():
            with self.subTest(item=item.label):
                self.assertTrue(
                    item.exact or item.prefix or item.path,
                    f"{item.label} has no rule that would ever mark it current",
                )

    def test_every_row_names_an_icon(self):
        for item in all_items():
            with self.subTest(item=item.label):
                self.assertTrue(item.icon)

    def test_labels_are_unique(self):
        """Two rows reading the same is a menu nobody can describe out loud."""
        labels = [item.label for item in all_items()]
        self.assertEqual(sorted(labels), sorted(set(labels)))

    def test_section_keys_are_unique(self):
        """The key is what remembers a collapsed section; a clash conflates two."""
        keys = [section.key for section in SECTIONS]
        self.assertEqual(sorted(keys), sorted(set(keys)))


class RenderingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.full = User.objects.create_user(
            id=960_001,
            username="nav-full",
            email="nav-full@example.com",
            password="x-nav-password",
            is_staff=True,
        )
        cls.full.user_permissions.set(Permission.objects.all())
        cls.thin = User.objects.create_user(
            id=960_002,
            username="nav-thin",
            email="nav-thin@example.com",
            password="x-nav-password",
        )
        cls.thin.user_permissions.add(Permission.objects.get(codename="view_company"))

    def nav_of(self, user, path="/settings/company/"):
        self.client.force_login(user)
        html = self.client.get(path).content.decode()
        start = html.find('aria-label="Primary navigation"')
        self.assertNotEqual(start, -1, "the sidebar did not render")
        return html[start : html.find("</nav>", start)]

    def test_a_permissioned_row_is_hidden_without_the_permission(self):
        thin = LABEL.findall(self.nav_of(self.thin))
        full = LABEL.findall(self.nav_of(self.full))
        self.assertIn("Sales orders", full)
        self.assertNotIn("Sales orders", thin)
        self.assertNotIn("General ledger", thin)

    def test_a_section_with_nothing_left_in_it_disappears(self):
        """An empty header labels nothing and reads as a broken menu."""
        nav = self.nav_of(self.thin)
        self.assertNotIn(">Sales<", nav)
        self.assertNotIn(">Relationships<", nav)

    def test_exactly_one_row_is_marked_current(self):
        for path, expected in (
            ("/settings/company/", "Company"),
            ("/parties/customers/", "Customers"),
            ("/reports/trial-balance/", "Trial balance"),
            ("/inventory/stock/ledger/", "Stock ledger"),
        ):
            with self.subTest(path=path):
                nav = self.nav_of(self.full, path)
                active = re.findall(r'nav-link-active[^>]*>.*?nav-label">([^<]+)<', nav, re.S)
                self.assertEqual(active, [expected])
                self.assertEqual(nav.count('aria-current="page"'), 1)

    def test_the_dashboard_is_not_permanently_highlighted(self):
        nav = self.nav_of(self.full, "/parties/customers/")
        active = re.findall(r'nav-link-active[^>]*>.*?nav-label">([^<]+)<', nav, re.S)
        self.assertNotIn("Dashboard", active)

    def test_the_section_holding_the_current_page_starts_open(self):
        """A collapsed section that hides where you are is worse than no memory."""
        nav = self.nav_of(self.full, "/reports/trial-balance/")
        finance = nav[nav.find('data-nav-section="finance"') :]
        self.assertTrue(finance[: finance.find(">")].strip().endswith("open"))

    def test_every_rendered_href_is_a_real_path(self):
        nav = self.nav_of(self.full)
        hrefs = HREF.findall(nav)
        self.assertGreater(len(hrefs), 20)
        for href in hrefs:
            with self.subTest(href=href):
                self.assertTrue(href.startswith("/"), f"{href} is not a path")
                self.assertNotEqual(href, "#")

    def test_the_company_badge_uses_real_data(self):
        """It was the literal 'AW' - right for one install, wrong for the rest."""
        self.client.force_login(self.full)
        html = self.client.get("/settings/company/").content.decode()
        self.assertNotIn(">AW</span>", html)
