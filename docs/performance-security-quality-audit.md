# Performance, Security, and Quality Audit

- Date: 2 September 2026
- Branch: `m4/prototype-django-ui`
- Scope: Django templates, request/query behavior, sales and payment workflows,
  authorization, PostgreSQL/Supabase posture, dependencies, CI, and deployment
  configuration.

## Executive outcome

The main delay was not expensive Python or a missing PostgreSQL index. The
application process was reaching a Supabase session pooler in Seoul from
Beirut, so every ORM round trip paid inter-region network latency. Several
pages multiplied that cost with avoidable queries, browser-side Tailwind
compilation, repeated session lookups, and full-page search requests while the
user was still typing.

This branch removes those multipliers, corrects authorization and accounting
workflow defects found during the audit, and adds repeatable quality gates. It
does not claim that geography can be optimized in code: consistently meeting a
sub-two-second uncached response target requires deploying the Django workers
near `ap-northeast-2`, or moving the database nearer the application and users.

## Measurements

Measurements used Django's test client, `CaptureQueriesContext`, a logged-in
superuser, and the real hosted database. Time is server-side wall time; database
time is the sum reported by Django's query instrumentation. Browser rendering
and internet variation are not included.

| Page | Original wall time | Original queries | Original DB time |
|---|---:|---:|---:|
| Dashboard | 4.62 s | 15 | 4.52 s |
| Payments | 1.41 s | 5 | — |
| Customers | 1.89 s | 6 | — |
| Vendors | 1.79 s | 6 | — |

After consolidating dashboard queries and adding its short-lived snapshot
cache, an intermediate measurement was 3.25 seconds and 11 queries cold, then
0.95 seconds and 3 queries warm. Final figures should always be remeasured from
the deployment region because local-to-Seoul latency varies more than the ORM
execution time.

The database contains only a small development data set. `pg_stat_statements`
and catalog inspection did not show a table-scan or missing-index bottleneck at
the present volume.

## Changes made

### Request and frontend performance

- Replaced the Tailwind Play CDN and Google Fonts requests with a committed,
  minified stylesheet built from `static/src/app.css` and
  `tailwind.config.js`. No page now downloads a compiler or depends on a font
  provider at runtime.
- Added reproducible `npm ci`, `build:css`, and `watch:css` commands. CI rebuilds
  the stylesheet and fails if the committed output is stale.
- Changed sessions to Django's `cached_db` backend. The database remains the
  durable source while repeat requests in the same worker avoid another remote
  session read.
- Added a 30-second dashboard snapshot cache and consolidated account and
  payment aggregates. Recent payments share the same snapshot.
- Added a bounded persistent database connection age, connection timeout, and
  application name. Per-request connection health checks are opt-in because
  they add a remote round trip.
- Removed full-page list reloads on every 400 ms typing pause. Search now runs
  explicitly on Enter or **Apply**, preventing request storms and interrupted
  typing over a high-latency connection.
- Added `select_related()` and `prefetch_related()` to customer, vendor, sales,
  and payment detail/list paths where templates traverse related objects.
- Made every list summary use the already filtered queryset, so displayed
  totals and exported/filter results have the same scope.
- Documented `runserver --nothreading` for local development against a remote
  database. Django documents that its threaded development server creates a
  thread per request and negates persistent connection reuse. Production must
  use a real WSGI/ASGI server.

### Correctness and transaction boundaries

- Sales-order creation now validates the complete line formset before
  allocating a number or writing a header. Invalid lines cannot leave an orphan
  order or consume a sequence number.
- Sales recalculation now snapshots the selected tax configuration, applies
  percentage or fixed document discounts proportionally, calculates base
  currency mirrors, bulk-updates all calculated lines once, and saves header
  totals once.
- Approval, rejection, and submission lock the current row with
  `select_for_update()`, run atomically, require approval/rejection reasons, and
  attribute audit events to the real actor.
- Posted/non-editable sales orders and non-draft payments cannot be reopened by
  entering an edit URL directly.
- Payment number allocation and fiscal-period validation remain in one atomic
  unit; failures roll back the sequence. Cross-table payment rules run without
  repeating ModelForm and PostgreSQL constraint queries.
- Sales line form markup no longer emits duplicate hidden fields. A bounded
  add-line control supports up to ten lines, while all values remain validated
  and recalculated by Django on submit.
- The posting engine now logs expected domain rejections as structured warnings
  and reserves exception tracebacks for unexpected failures.

### Authorization and auditability

- All shared list GET requests now enforce their declared Django model
  permission. Previously, safe methods bypassed the check.
- Corrected party and sales view attributes that used `permission_required`
  instead of the project's `required_permission`; the former was ignored by the
  custom mixin.
- The dashboard now requires authentication and `core.view_company`; an
  authenticated account without a business role receives 403.
- Create/export controls and completed navigation links are rendered only when
  the user has the corresponding permission. Server-side checks remain
  authoritative.
- CSV exports neutralize values that spreadsheet applications could interpret
  as formulas while preserving numeric values as numbers.
- Customer activation/deactivation requires a reason and records it with the
  actor in the audit trail.
- Added direct-URL permission, read-only, CSRF, immutable-state, sequence
  rollback, actor-attribution, and vendor-detail regression tests.

### Supabase/PostgreSQL hardening

The read-only baseline audit found row-level security enabled on 0 of 67 public
tables and broad privileges for the Supabase `anon` and `authenticated` roles
across 77 public objects. That is unsafe for a Django-owned data model exposed
through Supabase's public schema.

Migration `core.0007_harden_public_schema`:

- enables RLS on every current ordinary/partitioned public table except the
  extension-owned `spatial_ref_sys` table;
- revokes table, sequence, and function privileges independently from whichever
  of `anon` and `authenticated` exist; and
- revokes the same default privileges for future objects created by the
  migration role.

This application connects through Django using the table owner, so the ORM
continues to work. The migration intentionally has no automatic reverse: a
future Supabase Data API integration must add explicit least-privilege grants
and reviewed RLS policies in a new migration, rather than restoring blanket
access.

### Runtime, deployment, and maintenance

- Upgraded from the unsupported Django 5.1 line to Django 5.2 LTS and aligned
  the project with Django's supported PostgreSQL 14+ floor.
- Moved Pillow to runtime dependencies because `Company.logo` uses
  `ImageField`, and upgraded it to the supported 12.3 line.
- Added secure-by-default startup (`DEBUG` defaults off), HTTPS redirect,
  secure/HttpOnly/SameSite cookies, HSTS controls, proxy-header opt-in, referrer
  policy, frame denial, and manifest-based static files in production.
- Added GitHub Actions checks for Python 3.12/PostgreSQL 16, deterministic CSS,
  Ruff formatting/lint/security rules, migration drift, Django deployment
  checks, and the full test suite.
- Added weekly Dependabot checks for Python, npm, and GitHub Actions and verified
  the current npm dependency tree has no reported vulnerabilities.
- Improved `manage.py doctor` to support Django 5.2/PostgreSQL 14+, avoid
  Supabase-internal trigger false positives, and eliminate group-permission
  N+1 queries.

## Quality gates

Run these before merging or deploying:

```bash
python -m pip install -r requirements-dev.txt
npm ci
npm run build:css
ruff format --check .
ruff check .
python manage.py check
python manage.py makemigrations --check --dry-run
DJANGO_DEBUG=0 DJANGO_SECRET_KEY='<real-secret>' \
  DJANGO_ALLOWED_HOSTS='app.example.com' \
  python manage.py check --deploy --fail-level WARNING
python manage.py test --noinput
python manage.py doctor
```

`pip check`, npm's vulnerability audit, Django's system check, template
compilation, Ruff, the production deployment check, and CSS reproducibility are
part of this branch's verification evidence.

## Deployment sequence

1. Back up the database and verify restore/PITR policy in Supabase.
2. Install Python dependencies and run `npm ci && npm run build:css`.
3. Set a real `DJANGO_SECRET_KEY`, allowed hosts, CSRF trusted origins, timezone,
   and HTTPS proxy flags in the deployment environment.
4. Use the Supabase **session pooler** for persistent Django WSGI workers on
   IPv4 networks; do not silently substitute the transaction-pooler endpoint.
5. Run `python manage.py migrate` and `python manage.py doctor` once as a release
   task.
6. Run `python manage.py collectstatic --noinput` and serve `STATIC_ROOT` from
   the platform or web server.
7. Start production WSGI workers near the database and monitor response time,
   error rate, database saturation, and slow queries.

## Remaining production decisions

These require infrastructure or product decisions and are deliberately not
guessed in application code:

- Place Django and PostgreSQL in the same or nearby region. This is the highest
  impact remaining performance action.
- Replace process-local cache with Redis when multiple workers/instances need a
  shared cache and immediate invalidation behavior.
- Replace the current PostgreSQL owner credential with a dedicated,
  least-privilege application role and design how that role interacts with RLS.
- Confirm automated backups/PITR and perform a restore drill; code inspection
  cannot verify operational recoverability.
- Select the production WSGI/ASGI server, static/media storage, TLS proxy, login
  rate limiting, error tracking, and metrics platform.
- Add load tests with realistic order, journal, and audit volumes before setting
  capacity limits. The current development data set is too small to prove
  large-volume index behavior.
- Purchasing, inventory, accounting, and report screens shown as disabled in
  navigation remain separate feature work; this audit does not represent them
  as implemented.

## Reference documentation

- [Django 5.2 database connections](https://docs.djangoproject.com/en/5.2/ref/databases/#persistent-connections)
- [Django 5.2 performance guidance](https://docs.djangoproject.com/en/5.2/topics/performance/)
- [Django deployment checklist](https://docs.djangoproject.com/en/5.2/howto/deployment/checklist/)
- [Supabase PostgreSQL connection guidance](https://supabase.com/docs/guides/database/connecting-to-postgres)
- [Supabase query optimization](https://supabase.com/docs/guides/database/query-optimization)
- [Supabase `pg_stat_statements`](https://supabase.com/docs/guides/database/extensions/pg_stat_statements)
