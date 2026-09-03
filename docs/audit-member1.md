# Project Audit — Member 1 pass

**Scope:** whole repository, read-only, plus three implemented fixes confined to
`apps/parties`. **Branch:** `Customer_Management`. **Date:** 2 September 2026.

---

## 1. Executive summary

This codebase is in far better shape than a typical eight-day team build. The
security posture, CI pipeline and database-level rule enforcement are genuinely
strong — stronger than many shipped products. A prior audit
(`docs/performance-security-quality-audit.md`) already covered request
performance, transaction boundaries and Supabase hardening; this pass verified
those claims rather than repeating them, and looked for what was missed.

**Nothing critical was found.** No injection risk, no authentication bypass, no
data-corruption path, no secret in version control. The real risks are not in
the code: they are **process** risks — a divergent `main`/`dev`, a shared
database used for development, and roughly 40% of the planned functionality not
yet built.

**This project is not production-ready, and cannot be**, for reasons that have
nothing to do with quality: the product is incomplete. See §9.

---

## 2. What was verified as already correct

Credit where due — these were checked and found sound:

| Area | Evidence |
|---|---|
| Secret handling | `SECRET_KEY` required when `DEBUG=0`, refuses to boot without it (`config/settings.py:79`) |
| `.env` protection | gitignored (`.gitignore:4-5`); no credentials in tracked files |
| Production hardening | SSL redirect, secure cookies, HSTS, nosniff, referrer policy — automatic when `DEBUG` is off (`settings.py:312-332`) |
| Session security | `HTTPONLY` + `SAMESITE=Lax`, 12-hour expiry |
| SQL injection | No `.raw()`, no `RawSQL`, no `.extra()` anywhere in `apps/` |
| XSS | No `mark_safe` in application code; Django autoescaping intact |
| CSRF | `csrf_exempt` appears nowhere; every form template carries `{% csrf_token %}` |
| Authorization | Server-side via `ActionPermissionMixin`; 28 action permissions; read-only account flag enforced on every unsafe method |
| Business rules | 9 triggers + 168 CHECK constraints in PostgreSQL — rules hold regardless of application code |
| ORM discipline | 50 `select_related`/`prefetch_related` calls across 20 list views |
| Test suite | 341 tests across 30 files |
| CI | ruff format + check, `manage.py check`, `makemigrations --check`, `check --deploy --fail-level WARNING`, `npm audit`, CSS build verified against the committed file, full suite on PostgreSQL 16 |

That CI configuration deserves particular note: `git diff --exit-code -- static/css/app.css` after a rebuild means a stale compiled stylesheet **fails the build**, which is the correct answer to the class of bug that cost an afternoon this week.

---

## 3. Findings

Severity follows the requested scale. File references are to the audited branch.

### HIGH — process, not code

**H1. `main` and `dev` have diverged and neither contains the other's work.**
Member 2's purchases and inventory (4 commits) exist only on `main`; Member 1's
settings screens and Member 3's sales work only on `dev`. Nobody is developing
against a tree that contains the whole product, and the reconciliation cost
grows daily.
*Risk:* a large, conflict-heavy merge late in the schedule — the highest-risk
moment to attempt one.
*Fix:* agree one integration branch today; merge the other into it while the
divergence is four commits rather than forty.
*Status:* **not fixed — team decision, outside one member's authority.**

**H2. Development runs against a single shared Supabase database.**
All four developers share one database, and `manage.py test` creates and drops
`test_postgres` **on that same server**. Two concurrent test runs collide; one
developer's run deletes another's database mid-suite (observed twice this week).
*Risk:* corrupted test runs, and a real possibility of a mistaken migration or
bulk operation affecting everyone at once.
*Fix:* per-developer local PostgreSQL for tests, or a per-developer test
database name (`database_config["TEST"] = {"NAME": f"test_{PGUSER}"}`).
*Status:* **not fixed — changes how the whole team runs tests; needs agreement.**
*Secondary cost:* the suite takes **309 seconds** against Supabase
(`ap-northeast-2`) versus a few seconds locally. Every developer pays that on
every run.

**H3. No `.gitattributes`; line endings churn on every commit.**
`core.autocrlf` is unset for at least one developer. A recent working tree
showed **96 modified files of which 4 contained real changes** — 19,000 lines of
CRLF/LF noise.
*Risk:* unreviewable diffs; noise-vs-signal collisions during merges; the exact
condition that turns a small merge into an unrelated-histories rescue.
*Fix:* commit a `.gitattributes` containing `* text=auto eol=lf`.
*Status:* **not fixed — one-line repo-wide change, propose to the team.**

### MEDIUM — code quality, fixed in this pass

**M1. Duplicated widget-styling loops bypassing the shared form layer.**
`AddressForm` and `ContactForm` (`apps/parties/forms.py`) hand-rolled CSS class
assignment instead of using `UIFormMixin`, which every other form in the project
uses.
*Risk:* those two forms silently miss the shared validation attributes,
placeholders, searchable comboboxes and accessibility work; the duplication
drifts from the original as the design changes.
*Fix:* inherit `UIFormMixin`, delete both loops.
*Status:* **FIXED.** ~20 lines removed.

**M2. `get_required_permissions()` triplicated, and querying twice per request.**
The same three-line override appeared in `AddressUpdateView`,
`ContactUpdateView` and `PartyChildDeleteView`, each loading the row with
`get_object_or_404` — then the handler loaded it again.
*Risk:* two identical queries per edit/delete request; three copies of a
security-relevant rule to keep in step.
*Fix:* one `OwnerAwarePermissionMixin` with a cached `get_child_object()`.
*Status:* **FIXED.** Query count per delete drops from 2 to 1; the rule now
lives in one place.

**M3. N+1 on the party detail pages.**
`customer_detail.html` and `vendor_detail.html` iterate `customer.addresses.all`
and `customer.contacts.all`; neither detail view prefetched them.
*Risk:* two extra queries per page view — small individually, and the pattern is
the one that scales badly once other modules copy it.
*Fix:* `.prefetch_related("addresses", "contacts")` on both detail querysets.
*Status:* **FIXED.**

### MEDIUM — reported, not fixed

**M4. `BOTH` address type overlaps `BILLING` and `SHIPPING`.**
The constraint allows one default *per type*, so a party can simultaneously have
a default `SHIPPING` address and a default `BILLING AND SHIPPING` address.
"Which address does a delivery use?" then has two answers.
*Risk:* delivery and invoicing code (Members 2 and 3, not yet written against
this) picks arbitrarily; inconsistent documents.
*Fix:* either treat `BOTH` as satisfying the other two in the constraint, or drop
`BOTH` and require separate rows.
*Status:* **not fixed — schema change affecting other members' unwritten code.
Raise before they build against it.**

**M5. Fiscal period rows render dead "View" links.**
`FiscalPeriod` has no `get_absolute_url`, and `core/list_base.html` renders the
link unconditionally, producing `<a href="">`.
*Risk:* cosmetic, plus a mild accessibility problem — a link that goes nowhere.
*Fix:* give the model a URL, or make the column conditional in the shared
template. The template fix helps every future read-only list.
*Status:* **not fixed — touches the shared list template used by all members.**

**M6. Company logo cannot be uploaded.**
`CompanyForm` excludes `logo`; the form has no `enctype="multipart/form-data"`.
`Pillow` is nonetheless a hard runtime dependency for the field.
*Risk:* a documented feature (company branding on documents) is unreachable.
*Status:* **not fixed — small, deliberate deferral.**

### LOW

**L1.** Vendors reuse `parties/customer_form.html`, switched by a `party_kind`
context flag. Works; the filename now misleads. Rename to `party_form.html`.

**L2.** `docs/settings-screens.md` documents six screens; two more (chart of
accounts, account mappings) shipped afterwards and are not listed.

**L3.** No `Dockerfile`, `Procfile`, gunicorn config or WSGI server dependency.
Deployment is undefined — see §9.

---

## 4. Database and ORM

- **Connection handling** is correct: `CONN_MAX_AGE` configurable (default 60s),
  `ATOMIC_REQUESTS=False` (deliberate — posting services own their own
  transactions), `sslmode` derived from `DATABASE_URL` and defaulting to
  `require`.
- **Supabase session pooler** on port 5432 is the right choice for Django's
  persistent connections; the transaction pooler (6543) would break
  `select_for_update` semantics across statements.
- **Indexes**: foreign keys are indexed by Django automatically; the models add
  targeted composite indexes (`ix_audit_target`, `ix_audit_user_time`,
  `ix_account_selectable`, `ix_fx_lookup`). No obviously missing index was found
  in the audited apps.
- **Query counts** were not measured under load. Django Debug Toolbar is not
  installed; adding it in development would make N+1 regressions visible rather
  than inferred. Recommended, not implemented.

---

## 5. Testing

341 tests, and the important ones test the right things — `DirectUrlAccessTests`
calls views *through their URLs* rather than inspecting `has_perm()`, which is
the difference between testing authorisation and testing a data structure.

Gaps:

- **Vendor address/contact rules have no tests** (the customer equivalents do).
- **No test asserts query counts.** `assertNumQueries` around the list and detail
  views would turn N+1 regressions into build failures — cheap and high value
  given how much the shared list component is reused.
- **The three fixes in this pass are covered indirectly** by the existing suite;
  M2 in particular should get an explicit test that a Sales user (holding
  `change_customer` but not `change_vendor`) is refused on a vendor address.

---

## 6. Production readiness

| Dimension | Score | Reasoning |
|---|---|---|
| Architecture | **8/10** | Clean app separation, a genuinely reusable list/form/audit layer, business rules in the database. Loses points for the shared list template's assumptions and some duplication across members' apps. |
| Security | **8/10** | Nothing exploitable found. Loses points only for unverified Supabase RLS posture and the absence of error monitoring. |
| Performance | **6/10** | ORM discipline is good; no load testing, no query-count assertions, no profiling in development, and the remote-database latency is untested under concurrency. |
| Testing | **7/10** | 341 tests with meaningful assertions; gaps in the newest code and no performance regression tests. |
| Deployment | **2/10** | No container, no WSGI server, no process manager, no health check, no error monitoring, no runbook. Nothing has ever been deployed. |

---

## 7. Is it production-ready?

**No — and the blocker is completeness, not quality.**

Required before any production conversation:

1. **Finish the product.** Day 6 (product catalog) is unbuilt, and no product
   catalog means no purchase or sales lines. Several members' days remain.
2. **Reconcile `main` and `dev`.** Nobody has run the whole system together.
3. **Define deployment.** A WSGI server (gunicorn/uvicorn), static file serving
   (whitenoise or a CDN), a health-check endpoint, error monitoring, and a
   migration runbook — none currently exist.
4. **Accountant sign-off** on the placeholder base currency, the 11% standard
   rate and the chart-of-accounts codes (BRD §14.4). Shipping an accounting
   system on placeholder tax data is a business risk, not a technical one.
5. **A real environment.** Development shares one Supabase database with no
   staging tier.

What is *not* blocking: the code itself. The application layer is sound, the
rules are enforced where they cannot be bypassed, and the tests are honest.

---

## 8. Changes made in this pass

| File | Change | Lines |
|---|---|---|
| `apps/parties/forms.py` | `AddressForm`/`ContactForm` inherit `UIFormMixin`; two duplicated styling loops removed | −20 |
| `apps/parties/views.py` | `OwnerAwarePermissionMixin` replaces three copies of the permission override; object cached | −6 / +16 |
| `apps/parties/views.py` | `prefetch_related("addresses", "contacts")` on both party detail views | +6 |

All three are confined to Member 1's own app. No teammate's file was touched.

**Verification required before committing:**

```bash
python manage.py check
python manage.py test apps.parties
ruff format . && ruff check .
```

The forms change affects rendering, so also click through: add and edit a
customer address, and confirm the fields still carry the project's styling and
the searchable dropdowns behave.

---

## 9. Recommended next actions, in order

1. Run the verification above; revert any of the three fixes that surprises you.
2. Raise H1 (branch divergence) with the team **today**.
3. Propose `.gitattributes` (H3) — one line, ends a recurring cost for everyone.
4. Set up a local test database (H2) — saves five minutes per test run.
5. Finish Day 6, the product catalog.
6. Add `assertNumQueries` to the list and detail tests.
7. Raise M4 (address type overlap) with Members 2 and 3 before they build
   delivery and invoicing against it.
