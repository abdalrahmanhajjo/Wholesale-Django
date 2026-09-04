"""Liveness and readiness endpoints for whatever is running in front of us.

Two endpoints rather than one, because they answer different questions and a
load balancer acts on them differently:

``/healthz``  Is this process alive? Touches nothing. If it cannot answer, the
              process is wedged and should be restarted.
``/readyz``   Can this process actually serve a request - meaning, can it reach
              the database? A "no" here should take the instance out of
              rotation, not kill it, because the usual cause is the database
              rather than the instance.

Collapsing them into one endpoint means a database blip triggers a restart
loop, which is the opposite of helpful. Splitting them costs one URL.

Both are deliberately unauthenticated: a load balancer has no session. They are
therefore written to say nothing an anonymous caller should not know - no
version, no hostname, no settings, and on failure no exception text. The log
gets the detail; the response gets a word.
"""

import logging

from django.db import connection
from django.http import HttpResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

logger = logging.getLogger(__name__)


@never_cache
@require_GET
def healthz(request):
    """Liveness. Deliberately does no work at all."""
    return HttpResponse("ok", content_type="text/plain")


@never_cache
@require_GET
def readyz(request):
    """Readiness: can this process reach its database?"""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:
        # Logged in full here; the caller is told only that we are not ready,
        # because this endpoint is reachable without a session.
        logger.exception("Readiness probe failed to reach the database")
        return HttpResponse("not ready", content_type="text/plain", status=503)
    return HttpResponse("ready", content_type="text/plain")
