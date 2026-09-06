"""
Sign in, request an account, and reset a password.

Two properties matter more than the rest and are asserted first: a requested
account cannot sign in, and neither the sign-in form nor the reset form reveals
whether an account exists.

    python manage.py test apps.accounts
"""

import re

from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import User
from apps.core.tests.factories import make_user

LOCMEM = "django.core.mail.backends.locmem.EmailBackend"
#: Long enough to pass Django's validators; not a secret.
GOOD_PASSWORD = "correct-horse-battery-7"  # noqa: S105


class AccountRequestTests(TestCase):
    """A request creates an account that cannot be used."""

    def request_account(self, **overrides):
        data = {
            "username": "new-person",
            "full_name": "New Person",
            "email": "new.person@example.com",
            "job_title": "Accounts payable",
            "password1": GOOD_PASSWORD,
            "password2": GOOD_PASSWORD,
        }
        data.update(overrides)
        return self.client.post(reverse("signup"), data)

    def test_a_request_creates_an_account_that_cannot_sign_in(self):
        """The whole reason this page is safe to expose to anonymous visitors."""
        self.request_account()
        user = User.objects.get(username="new-person")
        self.assertFalse(user.is_active)
        self.assertFalse(self.client.login(username="new-person", password=GOOD_PASSWORD))

    def test_the_account_gets_no_role_and_no_permissions(self):
        self.request_account()
        user = User.objects.get(username="new-person")
        self.assertEqual(user.groups.count(), 0)
        self.assertEqual(user.user_permissions.count(), 0)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)

    def test_the_inactive_account_satisfies_the_model_constraint(self):
        """user_inactive_has_timestamp requires a deactivated_at on any inactive row."""
        self.request_account()
        user = User.objects.get(username="new-person")
        self.assertIsNotNone(user.deactivated_at)
        self.assertIn("approval", user.deactivated_reason.lower())

    def test_a_successful_request_redirects_to_its_own_page(self):
        """So a refresh cannot resubmit it."""
        self.assertRedirects(self.request_account(), reverse("signup_done"))

    def test_a_duplicate_email_is_refused_without_naming_the_holder(self):
        make_user("existing", email="taken@example.com")
        response = self.request_account(email="taken@example.com")
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("already exists", body)
        self.assertNotIn("existing", body)

    def test_email_matching_ignores_case(self):
        make_user("existing", email="taken@example.com")
        self.assertEqual(self.request_account(email="TAKEN@example.com").status_code, 200)
        self.assertFalse(User.objects.filter(username="new-person").exists())

    def test_a_taken_username_is_refused(self):
        make_user("new-person", email="other@example.com")
        self.assertEqual(self.request_account().status_code, 200)

    def test_mismatched_passwords_are_refused(self):
        response = self.request_account(password2="something-else-entirely")
        self.assertEqual(response.status_code, 200)
        self.assertIn("don&#x27;t match", response.content.decode())
        self.assertFalse(User.objects.filter(username="new-person").exists())

    def test_a_weak_password_is_refused_by_djangos_validators(self):
        response = self.request_account(password1="password", password2="password")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username="new-person").exists())

    def test_a_signed_in_user_is_sent_away(self):
        """
        fetch_redirect_response=False because the destination is not the point:
        this user has no role, so the dashboard would refuse them — which is
        correct, and nothing to do with whether the redirect happened.
        """
        self.client.force_login(make_user("already-in"))
        self.assertRedirects(
            self.client.get(reverse("signup")),
            reverse("dashboard"),
            fetch_redirect_response=False,
        )


@override_settings(EMAIL_BACKEND=LOCMEM)
class PasswordResetTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = make_user("reset-me", email="reset-me@example.com")

    def ask(self, email):
        mail.outbox = []
        return self.client.post(reverse("password_reset"), {"email": email})

    def link_from_last_mail(self):
        found = re.search(r"https?://[^/]+(/password-reset/[^\s]+)", mail.outbox[0].body)
        self.assertIsNotNone(found, "no reset link in the email")
        return found.group(1)

    def test_a_known_address_receives_a_link(self):
        self.ask("reset-me@example.com")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Ledgerwise", mail.outbox[0].subject)

    def test_an_unknown_address_looks_exactly_the_same(self):
        """
        Otherwise the form becomes a way to test which addresses have accounts.
        """
        known = self.ask("reset-me@example.com")
        sent_for_known = len(mail.outbox)
        unknown = self.ask("nobody@example.com")
        self.assertEqual(known.status_code, unknown.status_code)
        self.assertEqual(known["Location"], unknown["Location"])
        self.assertEqual(sent_for_known, 1)
        self.assertEqual(len(mail.outbox), 0)

    def test_the_link_opens_the_new_password_form(self):
        self.ask("reset-me@example.com")
        response = self.client.get(self.link_from_last_mail(), follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "new_password1")

    def test_the_link_can_be_used_once(self):
        self.ask("reset-me@example.com")
        link = self.link_from_last_mail()
        target = self.client.get(link, follow=True).redirect_chain[-1][0]
        self.client.post(
            target,
            {
                "new_password1": "a-much-longer-passphrase-42",
                "new_password2": "a-much-longer-passphrase-42",
            },
        )
        again = self.client.get(link, follow=True)
        self.assertContains(again, "expired")

    def test_the_new_password_works(self):
        self.ask("reset-me@example.com")
        target = self.client.get(self.link_from_last_mail(), follow=True).redirect_chain[-1][0]
        self.client.post(
            target,
            {
                "new_password1": "a-much-longer-passphrase-42",
                "new_password2": "a-much-longer-passphrase-42",
            },
        )
        self.assertTrue(
            self.client.login(username="reset-me", password="a-much-longer-passphrase-42")
        )


class AuthPageRenderTests(TestCase):
    """Every page outside the application renders and is structurally sound."""

    PAGES = [
        ("sign in", "login"),
        ("request account", "signup"),
        ("request sent", "signup_done"),
        ("reset: ask", "password_reset"),
        ("reset: sent", "password_reset_done"),
        ("reset: complete", "password_reset_complete"),
    ]

    def test_every_page_renders_with_one_heading(self):
        for name, route in self.PAGES:
            with self.subTest(page=name):
                response = self.client.get(reverse(route))
                self.assertEqual(response.status_code, 200)
                body = response.content.decode()
                self.assertEqual(len(re.findall(r"<h1[\s>]", body)), 1)

    def test_no_page_leaks_template_source(self):
        for name, route in self.PAGES:
            with self.subTest(page=name):
                body = self.client.get(reverse(route)).content.decode()
                for delimiter in ("{{", "{%", "{#"):
                    self.assertNotIn(delimiter, body, f"{name} leaked {delimiter}")

    def test_the_sign_in_failure_does_not_say_whether_the_user_exists(self):
        """
        Compares the two responses rather than matching a phrase.

        The property is that a real username and an invented one are
        indistinguishable — that is what stops the form being used to discover
        who has an account. Asserting on a phrase would keep passing if the two
        messages diverged, as long as both still contained it.
        """
        make_user("real-person")

        def failure_text(username):
            response = self.client.post(
                reverse("login"), {"username": username, "password": "wrong-password"}
            )
            self.assertEqual(response.status_code, 200)
            body = response.content.decode()
            alert = re.search(r'role="alert".*?</div>', body, re.S)
            self.assertIsNotNone(alert, "no failure message shown")
            # Collapse the wrapping so the comparison is about words, not layout.
            return " ".join(re.sub(r"<[^>]+>", " ", alert.group(0)).split())

        real = failure_text("real-person")
        invented = failure_text("no-such-person")
        self.assertEqual(real, invented)
        self.assertIn("Sign-in failed", real)
        self.assertNotIn("real-person", real)

    def test_each_page_links_onward_so_none_is_a_dead_end(self):
        for route, expected in [
            ("login", "password_reset"),
            ("login", "signup"),
            ("signup", "login"),
            ("password_reset", "login"),
        ]:
            with self.subTest(page=route, links_to=expected):
                body = self.client.get(reverse(route)).content.decode()
                self.assertIn(f'href="{reverse(expected)}"', body)


class DocumentHeadTests(TestCase):
    """Every auth page declares its charset and viewport, inside the head.

    These are asserted here rather than left to an editor's HTML linter. The
    linter reads the *template*, where a `{% comment %}` block sits above
    `<html>`; an HTML parser treats that as body text, opens an implied
    `<body>`, and then reports the metas that follow as being in the wrong
    place. It cannot reach the right answer, because the file it is reading is
    not the document that gets served.

    The rule it is trying to enforce is a real one, so it is checked against
    what the browser actually receives.
    """

    #: Every page built on registration/_auth_base.html.
    PAGES = (
        "login",
        "signup",
        "signup_done",
        "password_reset",
        "password_reset_done",
        "password_reset_complete",
    )

    def head_of(self, name):
        response = self.client.get(reverse(name))
        self.assertEqual(response.status_code, 200, f"{name} did not render")
        html = response.content.decode()
        start, end = html.find("<head"), html.find("</head>")
        self.assertNotEqual(start, -1, f"{name} rendered no <head>")
        self.assertGreater(end, start, f"{name} rendered no closing </head>")
        return html, html[start:end]

    def test_the_charset_is_declared_inside_the_head(self):
        for name in self.PAGES:
            with self.subTest(page=name):
                _, head = self.head_of(name)
                self.assertIn('<meta charset="utf-8">', head)

    def test_the_viewport_is_declared_inside_the_head(self):
        for name in self.PAGES:
            with self.subTest(page=name):
                _, head = self.head_of(name)
                self.assertIn('name="viewport"', head)

    def test_neither_meta_falls_into_the_body(self):
        """The specific claim the linter makes, tested against real output."""
        for name in self.PAGES:
            with self.subTest(page=name):
                html, _ = self.head_of(name)
                body_at = html.find("<body")
                self.assertGreater(body_at, 0, f"{name} rendered no <body>")
                for meta in ("<meta charset", 'name="viewport"'):
                    at = html.find(meta)
                    self.assertGreater(at, 0, f"{name} is missing {meta}")
                    self.assertLess(at, body_at, f"{name}: {meta} landed after <body>")

    def test_the_doctype_comes_first(self):
        """A Django comment above it renders to nothing, so it must still lead."""
        for name in self.PAGES:
            with self.subTest(page=name):
                html = self.client.get(reverse(name)).content.decode()
                self.assertTrue(
                    html.lstrip().lower().startswith("<!doctype html>"),
                    f"{name} does not begin with a doctype",
                )
