# Working together on this repo

Four people, one Django project, eight days. These rules exist because of one
thing: the ways four people accidentally block each other are predictable, and
almost all of them are cheap to prevent and expensive to fix.

Read this once at the start. It takes five minutes.

---

## 1. App ownership

| Member | Owns |
|---|---|
| 1 | `core`, `accounts`, `parties`, `catalog`, plus `config/` and shared templates |
| 2 | `purchases`, `inventory` |
| 3 | `sales` |
| 4 | `payments`, `ledger`, `reports` |

You can **read** and **import from** any app. You only **change** your own.

Need something changed in someone else's app? Ask them. It takes two minutes and
saves a merge conflict in a file neither of you can safely resolve.

`config/settings.py` and `INSTALLED_APPS` belong to Member 1. If you need
something added there — a new setting, a context processor — ask. This is the
single most conflict-prone file in any shared Django project.

---

## 2. Migrations

This is the rule that will actually save you time. Read it properly.

**Only run `makemigrations` for apps you own.**

```bash
python manage.py makemigrations catalog     # good, if you own catalog
python manage.py makemigrations             # dangerous — touches everything
```

Running it bare will generate migrations for other people's apps if their models
have drifted, and you'll commit files they were about to create themselves.

**Always pull before you generate.**

```bash
git pull
python manage.py makemigrations <your_app>
python manage.py migrate
```

**Commit the migration with the model change, in the same commit.** A model
change without its migration breaks everyone's `migrate` on the next pull.

**Never edit a migration that is already committed.** Once it's pushed, someone
has applied it. Changing it means their database and yours silently disagree.
Add a new migration instead.

**Never delete migration files** to "clean up". The numbers are a history, not
a tidy sequence.

### If you get conflicting migration numbers

Two people created `0005_...` in the same app. Django will refuse to run.

```bash
python manage.py makemigrations --merge
```

That generates a merge migration that reconciles both branches. Commit it. If it
doesn't work, ask Member 1 — do not hand-edit migration history.

### A note that makes this easier than it sounds

The schema is already complete. Most of the week involves **no new migrations at
all** — you are building views, forms and templates against models that exist.
The realistic exceptions are custom permissions (Member 1, Day 1) and small
additions as gaps appear. If you find yourself writing a lot of migrations,
check first whether the model you want already exists.

---

## 3. Branching

```bash
git checkout -b m1/login-and-nav      # <member>/<short-description>
# ... work ...
git add -A && git commit -m "Add login, logout and base template"
git push -u origin m1/login-and-nav
```

- Branch per piece of work, prefixed with your member number.
- **Merge to `main` at least once a day.** Long-lived branches are how an
  eight-day project produces a three-day merge on day seven.
- Never commit directly to `main` after Day 1.
- Pull `main` into your branch before you open a merge request.

`main` must always run. If you push something that breaks `migrate` or
`runserver` for everyone, fixing it is your immediate next task.

---

## 4. Where code goes

The BRD is explicit about this (§11.3, NFR-014), and it matters most for the
posting rules.

```
models.py       data and constraints only
services.py     business logic: totals, posting, allocation, eligibility
forms.py        validation and cleaning
views.py        HTTP: permissions, calling services, choosing templates
templates/      presentation only
```

**No business logic in views or templates.** A view should read like: check
permission, validate the form, call a service, render. If a view is doing
arithmetic on money, it belongs in a service.

**All money is `Decimal`.** Never `float`, anywhere, for any reason (BR-001).

**Posting goes through Member 4's posting service.** Do not write journal
entries directly from your app — the service handles account mapping,
idempotency and the balance rules.

**Wrap anything that touches money or stock in `transaction.atomic()`** (BR-005).
Either all of it happens or none of it does.

---

## 4b. Guarding your views (read this before writing a view)

Every action permission lives in `apps/core/permissions.py` as a constant.
Import the constant — never type the string.

```python
from apps.core.mixins import ActionPermissionMixin
from apps.core.permissions import POST_SALES_INVOICE

class SalesInvoicePostView(ActionPermissionMixin, View):
    required_permission = POST_SALES_INVOICE
```

For a function view: `@require_action(POST_SALES_INVOICE)`.

**Hiding a button is not security.** BRD §4.1 is explicit, and ACC-004's
acceptance test is "a user lacking a permission receives a denial even when
calling the URL directly". So:

```django
{% if perms.core.post_sales_invoice %}<button>Post</button>{% endif %}
```

is presentation. The view must *also* refuse. If only one of the two exists,
make it the view.

Need a permission that doesn't exist yet? Add it to `ACTION_PERMISSIONS` in
`apps/core/permissions.py` and tell Member 1 — it needs a migration, and that
file is Member 1's.

## 4d. Building a list screen (copy `apps/parties/views.py`)

Do not write a table by hand. Subclass `FilteredListView` and declare what your
list holds — you get search, filters, sortable columns, pagination, an empty
state and CSV export, all behaving the same as every other module (UX-002).

```python
from apps.core.list_views import BooleanFilter, ChoiceFilter, Column, FilteredListView
from apps.core.permissions import EXPORT_DATA

class PurchaseBillListView(FilteredListView):
    model = PurchaseBill
    permission_required = "purchases.view_purchasebill"
    page_title = "Purchase bills"
    create_url_name = "purchases:bill_create"
    export_permission = EXPORT_DATA
    export_filename = "purchase-bills"
    default_ordering = "-document_date"

    columns = [
        Column("number", "Number", sortable=True, link=True, css="font-mono text-xs"),
        Column("vendor", "Vendor", sortable=True),
        Column("document_date", "Date", sortable=True),
        Column("total_txn", "Total", align="right", money=True, sortable=True),
        Column("status", "Status", badge=True, align="center"),
    ]
    search_fields = ["number", "vendor__name", "vendor_invoice_number"]
    filters = [ChoiceFilter("status", "Status", DocumentStatus.choices)]

    def get_summary(self):
        return [("Open bills", ...), ("Overdue", ...)]
```

Your model needs `get_absolute_url()` for the row links. Give it a
`urls.py` with `app_name`, and add one `include()` to `config/urls.py` — ask
Member 1, that file is theirs.

Points worth knowing:

* **Sorting only works on columns marked `sortable=True.`** That is deliberate —
  accepting any `?sort=` value would let a visitor order by a related table.
* **Export re-runs the same queryset**, so UX-007's "exported rows match the
  on-screen filtered report" holds by construction, and it writes an audit event.
* **Filter state lives in the URL**, so a filtered list is shareable and the back
  button works.

## 4e. Recording audit events (ACC-005)

Every material change needs user, timestamp, action and before/after values.
Use `AuditedFormMixin` on your create and update views and it happens for you:

```python
from apps.parties.views import AuditedFormMixin   # or copy it into your app

class BillUpdateView(AuditedFormMixin, ActionPermissionMixin, UpdateView):
    ...
```

For posting, approving, reversing and closing, call the service directly:

```python
from apps.core import audit
from apps.core.models import AuditAction

audit.record_action(request, AuditAction.POST, invoice, reason=reason)
```

Do it **inside** the same `transaction.atomic()` as the change itself. If the
posting rolls back the audit event must roll back too — a log that records
things which did not happen is worse than no log.

## 4c. Templates and styling

Extend `templates/base.html`. It gives you the sidebar, header, message banner
and page-header block, and it carries the design tokens from the approved
prototype.

Use the component classes rather than re-deriving utility strings, so every
module looks like the same product:

| Class | For |
|---|---|
| `.btn-primary` `.btn-accent` `.btn-ghost` `.btn-danger` `.btn-sm` | buttons |
| `.card` `.card-pad` `.card-title` | panels |
| `.field` `.label` `.help` `.error-text` | form controls |
| `.badge-draft` `.badge-posted` `.badge-partial` `.badge-overdue` | statuses |
| `.eyebrow` | small uppercase section labels |

Blocks available: `title`, `header_title`, `page_title`, `page_subtitle`,
`breadcrumb`, `actions`, `content`, `extra_head`, `extra_scripts`.

Add a new component class to `base.html` rather than inventing a one-off — and
tell the team, since `base.html` is Member 1's file.

**Tailwind build:** templates load the committed `static/css/app.css`; the
browser never runs the Tailwind compiler. If you change templates,
`static/src/app.css`, or `tailwind.config.js`, run `npm run build:css` and
commit the generated stylesheet. Use `npm run watch:css` while iterating.

---

## 5. Before you commit

```bash
ruff format .
ruff check .
python manage.py doctor
```

Then check your diff for:

- No `.env`, no `__pycache__`, no `*.pyc`, no database dumps
- No secrets, passwords or API keys in code
- No `print()` left in — use the logger
- Model changes have their migration alongside

---

## 6. Daily rhythm

- **Morning:** `git pull`, `pip install -r requirements-dev.txt` (in case
  dependencies changed), `python manage.py migrate`, `python manage.py doctor`.
- **During the day:** commit small and often.
- **End of day:** merge to `main`, and say in the group chat what you finished
  and what you're blocked on.

If you're blocked for more than thirty minutes, say so. On an eight-day project
a silent blocked afternoon costs more than 6% of the schedule.

---

## 7. Sync points

- **End of Day 1** — Member 4 shares the posting-engine interface stub. Members
  2 and 3 need it by Day 4.
- **End of Day 2** — Member 1 confirms the base template and list-page pattern
  are ready to copy.
- **Day 4–5** — the first real postings. Everyone checks debit = credit
  end-to-end.
- **Day 7–8** — integration: one full cycle together (purchase → sale → payment
  → report).

---

## 8. When the database rejects your write

You will see errors like:

```
BR-006 violated: journal JV-00012 header totals (100.00 / 100.00)
do not match its lines (50.00 / 50.00)
```

That is a posting-guard trigger doing exactly what it was built to do. The
message names the BRD rule so you can look it up.

**The fix is almost always in your code.** Before proposing a schema change,
work out which rule you're breaking and why. If a constraint really is wrong,
bring it to Member 1 with the case that breaks it — but the constraints have
been verified against 55 test cases, so the odds favour the schema.
