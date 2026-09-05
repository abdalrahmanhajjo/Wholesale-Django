"""Every application route requires a session, except the ones that must not.

ACC-004's evidence is that calling a URL directly gets you nowhere without the
right session and permission. Individual views test that for themselves; this
one tests the *set* — it walks the real URLconf, so a view added next month is
covered the day it is routed, without anyone remembering to write a test.

The allowlist is the security-relevant part of this file. Anything in it is a
deliberate decision to expose a URL to the internet, and adding an entry should
be as hard to do by accident as possible.
"""

from django.test import TestCase
from django.urls import URLPattern, URLResolver, get_resolver

#: Routes that are anonymous on purpose, each with the reason.
PUBLIC_ROUTES = {
    "dashboard": "The public home page: a marketing landing for anonymous "
    "visitors, and the dashboard for signed-in users.",
    "login": "Somewhere to sign in from.",
    "logout": "Ends a session; harmless without one.",
    "password_reset": "Requested precisely because nobody can log in.",
    "password_reset_done": "Confirmation page for the above.",
    "password_reset_confirm": "Reached from an emailed single-use token.",
    "password_reset_complete": "Confirmation page for the above.",
    "signup": "ACC-003: creates an account that cannot sign in until approved.",
    "signup_done": "Confirmation page for the above.",
    # PAY-013. Stripe cannot hold a session or a CSRF token. Its authentication
    # is the webhook signature, checked in apps/payments/stripe_gateway.py.
    "payments:stripe_webhook": "Authenticated by Stripe signature instead.",
    # A load balancer has no session either. Both are written to disclose
    # nothing - see apps/core/health.py.
    "healthz": "Liveness probe; reports no state at all.",
    "readyz": "Readiness probe; reports reachable/not and nothing else.",
}

#: Namespaces this test does not own. Django's admin enforces its own staff
#: login on every view it registers.
SKIPPED_NAMESPACES = {"admin"}

#: Sample values by path converter, so a URL can be reversed without fixtures.
SAMPLES = {
    "int": "1",
    "str": "sample",
    "slug": "sample",
    "uuid": "3f1d5c2e-3a47-4f8b-9c21-0d8e7b4a5f30",
    "path": "sample",
}


def _iter_routes(patterns, prefix="", namespace=None):
    for entry in patterns:
        if isinstance(entry, URLResolver):
            child = entry.namespace or namespace
            if child in SKIPPED_NAMESPACES:
                continue
            yield from _iter_routes(entry.url_patterns, prefix + str(entry.pattern), child)
        elif isinstance(entry, URLPattern):
            name = entry.name
            if name is None:
                continue
            yield (f"{namespace}:{name}" if namespace else name), prefix + str(entry.pattern)


def _concrete_path(route: str) -> str | None:
    """Turn '<int:pk>/post/' into a requestable path, or None if it cannot be."""
    import re

    def swap(match):
        converter, _, _name = match.group(1).partition(":")
        if not _name:
            converter, _name = "str", converter
        return SAMPLES.get(converter, "1")

    if "(?P<" in route or "^" in route:
        return None  # a regex route; not worth guessing at
    return "/" + re.sub(r"<([^>]+)>", swap, route)


class EveryRouteRequiresASessionTests(TestCase):
    def test_no_application_route_serves_anonymous_users(self):
        offenders = []
        checked = 0
        for name, route in _iter_routes(get_resolver().url_patterns):
            if name in PUBLIC_ROUTES:
                continue
            path = _concrete_path(route)
            if path is None:
                continue
            checked += 1
            for method in ("get", "post"):
                response = getattr(self.client, method)(path)
                # 200 means content reached an anonymous caller. Anything else
                # - a redirect to login, 403, 404, 405 - kept them out.
                if response.status_code == 200:
                    offenders.append(f"{method.upper()} {path} ({name}) -> 200")

        self.assertGreater(checked, 40, "Route walk found suspiciously few routes.")
        self.assertEqual(
            offenders,
            [],
            "These routes served an anonymous request. Add a permission mixin, "
            "or add the route to PUBLIC_ROUTES with a reason:\n  " + "\n  ".join(offenders),
        )

    def test_the_public_allowlist_still_matches_reality(self):
        """A stale allowlist quietly grants an exemption nothing needs."""
        live = {name for name, _ in _iter_routes(get_resolver().url_patterns)}
        stale = sorted(set(PUBLIC_ROUTES) - live)
        self.assertEqual(
            stale, [], f"PUBLIC_ROUTES names routes that no longer exist: {stale}"
        )


class HealthEndpointTests(TestCase):
    """The probes are public, so what they say matters as much as that they answer."""

    def test_liveness_answers_without_a_session(self):
        response = self.client.get("/healthz/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"ok")

    def test_readiness_reports_the_database_is_reachable(self):
        response = self.client.get("/readyz/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"ready")

    def test_probes_disclose_nothing_beyond_a_single_word(self):
        """No version, no hostname, no settings - an anonymous caller learns nothing."""
        for path in ("/healthz/", "/readyz/"):
            body = self.client.get(path).content.decode()
            self.assertLess(len(body), 12, f"{path} returned more than a status word")

    def test_probes_are_not_cached(self):
        """A cached readiness answer is worse than none: it is confidently stale."""
        for path in ("/healthz/", "/readyz/"):
            cache_control = self.client.get(path).headers.get("Cache-Control", "")
            self.assertIn("no-cache", cache_control)

    def test_probes_reject_writes(self):
        self.assertEqual(self.client.post("/healthz/").status_code, 405)
        self.assertEqual(self.client.post("/readyz/").status_code, 405)
