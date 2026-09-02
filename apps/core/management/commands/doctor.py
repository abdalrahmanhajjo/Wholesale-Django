"""
`python manage.py doctor` — checks that a developer's machine is set up correctly.

The point is that setup problems diagnose themselves. A teammate who sees
"pg_trgm extension missing — your database was created before migration
core.0001_extensions" fixes it in a minute; the same person seeing a 40-line
Django traceback messages whoever set the project up.

Run it after following the Setup section of README.md. Exit status is 0 when
everything passes and 1 otherwise, so CI can use it too.
"""

import sys

import django
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
DIM = "\033[2m"
RESET = "\033[0m"

MIN_PYTHON = (3, 11)
MIN_POSTGRES = 14
REQUIRED_EXTENSIONS = ["btree_gist", "pg_trgm"]
EXPECTED_TRIGGERS = 9
EXPECTED_VIEWS = 10
EXPECTED_FUNCTIONS = 4
SUPPORTED_DJANGO_PREFIX = "5.2"


class Command(BaseCommand):
    help = "Check that this machine is correctly set up to run the project."

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.failures = 0
        self.warnings = 0

    # -- output helpers ----------------------------------------------------
    def ok(self, label, detail=""):
        self.stdout.write(
            f"  {GREEN}PASS{RESET}  {label}" + (f"  {DIM}{detail}{RESET}" if detail else "")
        )

    def fail(self, label, fix):
        self.failures += 1
        self.stdout.write(f"  {RED}FAIL{RESET}  {label}")
        self.stdout.write(f"        {DIM}fix: {fix}{RESET}")

    def warn(self, label, note):
        self.warnings += 1
        self.stdout.write(f"  {YELLOW}WARN{RESET}  {label}")
        self.stdout.write(f"        {DIM}{note}{RESET}")

    def section(self, title):
        self.stdout.write(f"\n{title}")

    # -- checks ------------------------------------------------------------
    def handle(self, *args, **options):
        self.stdout.write("\nWholesale Accounting & BMS — setup check")

        self.check_python()
        self.check_django()
        self.check_environment()
        self.check_database()
        self.check_extensions()
        self.check_migrations()
        self.check_schema_objects()
        self.check_seed_data()

        self.stdout.write("")
        if self.failures:
            self.stdout.write(
                f"{RED}{self.failures} check(s) failed.{RESET} "
                "Work through the fixes above, then run this again."
            )
            sys.exit(1)
        if self.warnings:
            self.stdout.write(
                f"{GREEN}All checks passed{RESET} with {self.warnings} warning(s). "
                "You are ready to work."
            )
        else:
            self.stdout.write(f"{GREEN}All checks passed. You are ready to work.{RESET}")

    def check_python(self):
        self.section("Runtime")
        version = sys.version_info
        label = f"Python {version.major}.{version.minor}.{version.micro}"
        if version[:2] >= MIN_PYTHON:
            self.ok(label)
        else:
            self.fail(
                f"{label} is too old",
                f"install Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer and rebuild your virtualenv",
            )

    def check_django(self):
        version = django.get_version()
        if version.startswith(SUPPORTED_DJANGO_PREFIX):
            self.ok(f"Django {version}")
        else:
            self.warn(
                f"Django {version} — the project is pinned to {SUPPORTED_DJANGO_PREFIX}",
                "run: pip install -r requirements.txt",
            )

    def check_environment(self):
        self.section("Configuration")
        env_file = settings.BASE_DIR / ".env"
        if env_file.exists():
            self.ok(".env present")
        else:
            self.warn(
                ".env not found — using process environment only",
                "fine in production; locally run: cp .env.example .env",
            )

        if settings.DEBUG:
            self.ok("DEBUG is on", "development mode")
        else:
            self.ok("DEBUG is off", "production hardening active")
            if "dev-only" in settings.SECRET_KEY:
                self.fail(
                    "DEBUG is off but SECRET_KEY is still the development key",
                    "set DJANGO_SECRET_KEY in the environment",
                )

    def check_database(self):
        self.section("Database")
        try:
            with connection.cursor() as cur:
                cur.execute("SELECT version()")
                banner = cur.fetchone()[0]
        except Exception as exc:  # noqa: BLE001 — we want any failure reported plainly
            self.fail(
                f"cannot connect to PostgreSQL — {str(exc).splitlines()[0][:90]}",
                "check PGHOST/PGPORT/PGUSER/PGPASSWORD in .env, and that PostgreSQL is running",
            )
            return

        major = connection.pg_version // 10000
        db = settings.DATABASES["default"]["NAME"]
        if major >= MIN_POSTGRES:
            self.ok(f"PostgreSQL {major} reachable", f"database '{db}'")
        else:
            self.fail(
                f"PostgreSQL {major} is too old",
                f"the schema needs PostgreSQL {MIN_POSTGRES} or newer "
                "(exclusion constraints and plpgsql triggers)",
            )
        self.stdout.write(f"        {DIM}{banner[:70]}{RESET}")

    def check_extensions(self):
        if not self._db_reachable():
            return
        with connection.cursor() as cur:
            cur.execute("SELECT extname FROM pg_extension")
            installed = {row[0] for row in cur.fetchall()}
        for ext in REQUIRED_EXTENSIONS:
            if ext in installed:
                self.ok(f"extension {ext}")
            else:
                self.fail(
                    f"extension {ext} is missing",
                    "run: python manage.py migrate   (it is created by core.0001_extensions)",
                )

    def check_migrations(self):
        if not self._db_reachable():
            return
        self.section("Migrations")
        try:
            executor = MigrationExecutor(connection)
            plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
        except Exception as exc:  # noqa: BLE001
            self.fail(
                f"migration state could not be read — {str(exc).splitlines()[0][:80]}",
                "if this is a fresh clone, run: python manage.py migrate",
            )
            return

        if plan:
            names = ", ".join(f"{m.app_label}.{m.name}" for m, _ in plan[:4])
            more = f" (+{len(plan) - 4} more)" if len(plan) > 4 else ""
            self.fail(
                f"{len(plan)} migration(s) not applied: {names}{more}",
                "run: python manage.py migrate",
            )
        else:
            applied = len(executor.loader.applied_migrations)
            self.ok("all migrations applied", f"{applied} in total")

    def check_schema_objects(self):
        if not self._db_reachable():
            return
        self.section("Schema objects")
        with connection.cursor() as cur:
            cur.execute(
                "SELECT "
                "(SELECT count(*) FROM pg_trigger t "
                " JOIN pg_class c ON c.oid = t.tgrelid "
                " JOIN pg_namespace n ON n.oid = c.relnamespace "
                " WHERE NOT t.tgisinternal AND n.nspname = 'public' "
                " AND t.tgname LIKE 'trg\\_%'), "
                "(SELECT count(*) FROM information_schema.views "
                " WHERE table_schema = 'public'), "
                "(SELECT count(*) FROM pg_proc p "
                " JOIN pg_namespace n ON n.oid = p.pronamespace "
                " WHERE n.nspname = 'public' AND p.proname LIKE 'fn\\_%')"
            )
            triggers, views, functions = cur.fetchone()

        self._expect(triggers, EXPECTED_TRIGGERS, "posting-guard triggers")
        self._expect(views, EXPECTED_VIEWS, "reporting views")
        self._expect(functions, EXPECTED_FUNCTIONS, "reporting functions")

    def check_seed_data(self):
        if not self._db_reachable():
            return
        self.section("Reference data")
        from apps.core.models import Currency, FiscalPeriod
        from apps.ledger.models import Account, AccountMapping, MappingKey

        accounts = Account.objects.count()
        if accounts >= 55:
            self.ok("chart of accounts seeded", f"{accounts} accounts")
        else:
            self.fail(
                f"only {accounts} accounts found",
                "run: python manage.py migrate   (seeded by core.0004_seed_reference_data)",
            )

        mappings, expected = AccountMapping.objects.count(), len(MappingKey.choices)
        if mappings == expected:
            self.ok("account mappings complete (CFG-007)", f"{mappings}/{expected}")
        else:
            self.fail(
                f"account mappings incomplete: {mappings}/{expected}",
                "posting will refuse to run until every mapping key resolves",
            )

        base = Currency.objects.filter(is_base=True).first()
        if base:
            self.ok("base currency set (BR-002)", base.code)
        else:
            self.fail("no base currency", "run: python manage.py migrate")

        # ACC-003: empty role groups mean every permission check fails silently.
        from django.contrib.auth.models import Group
        from django.db.models import Count

        groups = list(Group.objects.annotate(permission_count=Count("permissions")))
        empty = [group.name for group in groups if group.permission_count == 0]
        if groups and not empty:
            self.ok("role groups populated (ACC-003)", f"{len(groups)} roles")
        elif empty:
            self.fail(
                f"role group(s) with no permissions: {', '.join(empty)}",
                "run: python manage.py migrate   (assigned by core.0006_seed_role_permissions)",
            )

        open_periods = FiscalPeriod.objects.filter(status="OPEN").count()
        if open_periods:
            self.ok("fiscal calendar present", f"{open_periods} open period(s)")
        else:
            self.fail(
                "no open fiscal period",
                "nothing can be posted without one (BR-020)",
            )

    # -- internals ---------------------------------------------------------
    def _expect(self, actual, expected, label):
        if actual == expected:
            self.ok(label, f"{actual}")
        elif actual < expected:
            self.fail(
                f"{label}: found {actual}, expected {expected}",
                "run: python manage.py migrate",
            )
        else:
            self.warn(
                f"{label}: found {actual}, expected {expected}",
                "extra objects exist — fine if someone added them deliberately",
            )

    def _db_reachable(self):
        try:
            with connection.cursor() as cur:
                cur.execute("SELECT 1")
            return True
        except Exception:  # noqa: BLE001
            return False
