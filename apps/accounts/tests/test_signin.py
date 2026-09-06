"""Sign-in identifier, remember-me, and the password-change screens (ACC-001, ACC-002)."""

import re

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.forms import PENDING_APPROVAL

User = get_user_model()
PASSWORD = "correct-horse-battery-staple"  # noqa: S105 - test fixture


def make_user(username="signin-person", email="signin@example.com", **extra):
    # Created active, then deactivated if asked: user_inactive_has_timestamp
    # refuses an inactive row that does not say when it became one, and
    # create_user saves before we could set it.
    inactive = extra.pop("is_active", True) is False
    user = User.objects.create_user(username=username, email=email, **extra)
    user.set_password(PASSWORD)
    user.save()
    if inactive:
        deactivate(user, "Revoked")
    return user


def deactivate(user, reason):
    user.is_active = False
    user.deactivated_at = timezone.now()
    user.deactivated_reason = reason
    user.save(update_fields=["is_active", "deactivated_at", "deactivated_reason"])
    return user


def alert_text(response):
    """The failure message alone — the page also carries a fresh CSRF token."""
    body = response.content.decode()
    match = re.search(r'role="alert".*?</div>', body, re.S)
    if match is None:
        raise AssertionError("no failure message shown")
    return " ".join(re.sub(r"<[^>]+>", " ", match.group(0)).split())


class IdentifierTests(TestCase):
    """Either identifier works; neither reveals whether an account exists."""

    @classmethod
    def setUpTestData(cls):
        cls.user = make_user()

    def sign_in(self, identifier, password=PASSWORD):
        return self.client.post(
            reverse("login"), {"username": identifier, "password": password}
        )

    def test_the_username_still_works(self):
        self.assertEqual(self.sign_in("signin-person").status_code, 302)

    def test_the_email_works_too(self):
        self.assertEqual(self.sign_in("signin@example.com").status_code, 302)

    def test_the_email_is_matched_regardless_of_case(self):
        self.assertEqual(self.sign_in("SignIn@Example.COM").status_code, 302)

    def test_surrounding_space_is_ignored(self):
        self.assertEqual(self.sign_in("  signin-person  ").status_code, 302)

    def test_a_wrong_password_is_refused(self):
        self.assertEqual(self.sign_in("signin-person", "wrong").status_code, 200)

    def test_an_unknown_identifier_is_refused(self):
        self.assertEqual(self.sign_in("nobody@example.com").status_code, 200)

    def test_the_failure_is_identical_for_a_real_and_an_invented_account(self):
        """The security property, restated for the email identifier.

        Widening the lookup would be a way to discover who has an account if
        the two replies differed at all.
        """
        real = alert_text(self.sign_in("signin@example.com", "wrong"))
        invented = alert_text(self.sign_in("nobody@example.com", "wrong"))
        self.assertIn("Sign-in failed", real)
        self.assertEqual(real, invented)
        self.assertNotIn("signin@example.com", real)

    def test_an_inactive_account_cannot_sign_in(self):
        make_user(username="revoked", email="revoked@example.com", is_active=False)
        self.assertEqual(self.sign_in("revoked", PASSWORD).status_code, 200)

    def test_an_ambiguous_identifier_is_refused_rather_than_guessed(self):
        """One person's username is another's email address."""
        User.objects.filter(pk=self.user.pk).update(username="clash@example.com")
        make_user(username="other-person", email="clash@example.com")
        self.assertEqual(self.sign_in("clash@example.com").status_code, 200)


class PendingAccountTests(TestCase):
    def test_a_pending_account_is_told_it_is_pending(self):
        """Not disclosure: they filled the form that created it."""
        deactivate(
            make_user(username="pending", email="pending@example.com"), PENDING_APPROVAL
        )

        response = self.client.post(
            reverse("login"), {"username": "pending", "password": PASSWORD}
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "waiting for an administrator")

    def test_a_wrong_password_never_reveals_that_an_account_is_pending(self):
        """Otherwise the message becomes a way to enumerate pending accounts."""
        deactivate(make_user(username="quiet", email="quiet@example.com"), PENDING_APPROVAL)
        response = self.client.post(
            reverse("login"), {"username": "quiet@example.com", "password": "wrong"}
        )
        self.assertNotContains(response, "waiting for an administrator")
        self.assertContains(response, "Sign-in failed")

    def test_an_ordinary_deactivated_account_gets_the_generic_answer(self):
        deactivate(make_user(username="gone", email="gone@example.com"), "Left the company")

        response = self.client.post(
            reverse("login"), {"username": "gone", "password": PASSWORD}
        )

        self.assertNotContains(response, "waiting for an administrator")


class RememberMeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = make_user()

    def test_ticking_it_keeps_the_configured_session_length(self):
        self.client.post(
            reverse("login"),
            {"username": "signin-person", "password": PASSWORD, "remember_me": "on"},
        )
        self.assertEqual(self.client.session.get_expiry_age(), settings.SESSION_COOKIE_AGE)

    def test_leaving_it_off_ends_the_session_with_the_browser(self):
        self.client.post(reverse("login"), {"username": "signin-person", "password": PASSWORD})
        self.assertTrue(self.client.session.get_expire_at_browser_close())


class PasswordChangeTests(TestCase):
    """The route existed and rendered a template that had never been written."""

    def setUp(self):
        self.user = make_user()
        self.client.force_login(self.user)

    def test_the_form_renders_instead_of_erroring(self):
        response = self.client.get(reverse("password_change"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Current password")

    def test_it_requires_the_current_password(self):
        response = self.client.post(
            reverse("password_change"),
            {
                "old_password": "not-the-password",
                "new_password1": "another-long-passphrase-42",
                "new_password2": "another-long-passphrase-42",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(PASSWORD))

    def test_a_successful_change_lands_on_a_page_that_says_so(self):
        response = self.client.post(
            reverse("password_change"),
            {
                "old_password": PASSWORD,
                "new_password1": "another-long-passphrase-42",
                "new_password2": "another-long-passphrase-42",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Password changed")
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("another-long-passphrase-42"))

    def test_the_confirmation_page_is_not_public(self):
        self.client.logout()
        response = self.client.get(reverse("password_change_done"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])
