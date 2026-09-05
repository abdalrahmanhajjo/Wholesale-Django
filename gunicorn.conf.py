"""Gunicorn configuration.

The numbers here are driven by one measured fact about this deployment: the
database is remote and a query round trip is roughly 300ms (see the performance
notes in README). That makes almost every request I/O-bound rather than
CPU-bound, which changes what the right worker model is.

Sync workers would spend that 300ms asleep holding a whole process. Threads let
one worker keep serving while another request waits on the database, so the
worker count stays low - and the worker count is what consumes the connection
budget on the Supabase pooler, which allows 60 connections in total.

    connections = workers x threads (worst case, with CONN_MAX_AGE > 0)

so the defaults below (2 x 4 = 8) leave plenty of headroom, and anyone raising
them should check that arithmetic against the pooler rather than against CPU
count, which is the usual advice and the wrong advice here.
"""

import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"

workers = int(os.environ.get("GUNICORN_WORKERS", "2"))
threads = int(os.environ.get("GUNICORN_THREADS", "4"))
worker_class = "gthread"

# Longer than a slow query but shorter than a hung one. The database's own
# statement_timeout is 2 minutes, so a worker killed at 60s means the request
# was stuck on something other than a single query.
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "60"))
graceful_timeout = 30

# Beyond the idle timeout of most load balancers, so the connection is closed
# by us rather than dropped mid-response by them.
keepalive = 65

# Recycle workers periodically: a slow leak in a long-running process is very
# hard to find and very cheap to survive. The jitter stops every worker
# restarting at once and emptying the pool.
max_requests = int(os.environ.get("GUNICORN_MAX_REQUESTS", "1000"))
max_requests_jitter = 100

# Logs to stdout/stderr so the platform collects them; nothing writes to disk.
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")

# Trust the proxy's X-Forwarded-* only when explicitly told to; see
# DJANGO_BEHIND_HTTPS_PROXY in settings.py for the Django half of this.
forwarded_allow_ips = os.environ.get("GUNICORN_FORWARDED_ALLOW_IPS", "127.0.0.1")
