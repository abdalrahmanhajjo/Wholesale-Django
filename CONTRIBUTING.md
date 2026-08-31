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
