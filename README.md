# Wholesale Accounting & Business Management System

A full-stack Django application for a wholesale business: purchasing, inventory,
sales, payments, and an accrual-basis double-entry general ledger.

Built from `Wholesale_Accounting_BRD_Django.docx` (BRD v1.0, 28 August 2026).
Django 5.1 · PostgreSQL 16 · Django Templates (no separate frontend).

---

## Setup

Ten minutes from clone to a running site. If any step fails, `manage.py doctor`
will tell you why.

### 1. Prerequisites

- **Python 3.11+**
- **PostgreSQL 13+** (16 recommended) running locally

Everyone runs their own local database. Migrations are shared through git; data
is not. Nobody can break anybody else's data.

### 2. Clone and install

```bash
git clone <repo-url> wams
cd wams

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -r requirements-dev.txt
```

### 3. Configure

```bash
cp .env.example .env
```

Open `.env` and set `PGUSER` / `PGPASSWORD` to match your local PostgreSQL.
Everything else can stay as it is for development.

### 4. Create the database

```bash
createdb wams
```

If `createdb` isn't on your PATH, this works too:

```bash
psql -U postgres -c "CREATE DATABASE wams;"
```

### 5. Build the schema

```bash
python manage.py migrate
```

This creates 67 tables, 9 posting-guard triggers, 10 reporting views and 4
reporting functions, then seeds the reference data: chart of accounts, account
mappings, tax codes, currencies, the 2026–2027 fiscal calendar, money accounts,
payment methods, number series and the seven role groups.

### 6. Check your setup

```bash
python manage.py doctor
```

Every line should say PASS. If something fails, the output tells you the fix.

### 7. Create yourself a login and run it

```bash
python manage.py createsuperuser
python manage.py runserver
```

Then open http://127.0.0.1:8000/admin/.

---

## What already exists

The database schema is **complete and verified** — this is not a starting point
to be filled in, it is a foundation to build screens on.

| Area | Status |
|---|---|
| All models across 10 apps | Built, migrated, verified |
| Reference data | Seeded (chart of accounts, tax, currencies, calendar, roles) |
| Accounting rules | Enforced in the database (see below) |
| Reporting layer | 10 views + 4 functions |
| Views, templates, forms | **Not built** — this is the work |

Before designing a model, check whether it already exists. It probably does.

### The rules the database enforces

Nine triggers and 168 CHECK constraints make certain things impossible, whatever
the application code does. You will meet these as errors — that is intentional.

- A journal entry's stored totals must equal the sum of its lines, and debits
  must equal credits (BR-006). Checked at COMMIT, so you may write the header
  before the lines.
- No posting into a closed fiscal period, or with a date outside its period (BR-020).
- No posting to a non-postable parent account or a deactivated account (GL-010).
- An AR or AP line must carry a party (GL-011).
- Posted journals and journal lines are never edited or deleted — reverse instead (BR-004).
- Stock cannot go negative unless policy allows it (BR-017).
- An invoice's open balance must equal total − allocated − credited (BR-009).
- Allocations cannot exceed the payment or the document (BR-008).

If you hit one of these, the fix is almost always in your code, not the schema.
Bring it to Member 1 before changing a constraint.

### Verifying the schema

```bash
# needs a fresh database — it writes posted journals, which are immutable
dropdb wams && createdb wams && python manage.py migrate
python verify_schema.py
```

55 checks. All should pass.

---

## Project layout

```
config/                 settings, root URLs, WSGI/ASGI
apps/
  core/                 company, currency, FX, tax, payment terms, fiscal
                        calendar, numbering, audit trail, shared base models
  accounts/             User, roles
  ledger/               chart of accounts, mappings, journals, posting guards
  parties/              customers, vendors, addresses, contacts
  catalog/              products, categories, units, price lists
  inventory/            warehouses, stock movements, receipts, deliveries
  sales/                orders, invoices, returns, credit notes
  purchases/            orders, bills, returns, debit notes
  payments/             money accounts, payments, allocations, refunds
  reports/              no models — reporting views and functions
verify_schema.py        constraint verification harness
schema_reference.sql    pg_dump of the built schema, for reference
```

## Who owns what

| Member | Apps | Focus |
|---|---|---|
| 1 | core, accounts, parties, catalog | Foundation, identity, master data, shared UI |
| 2 | purchases, inventory | Purchasing and stock |
| 3 | sales | Sales cycle and customer returns |
| 4 | payments, ledger, reports | Posting engine, money, financial statements |

**You run `makemigrations` only for your own apps.** See CONTRIBUTING.md.

## Useful commands

```bash
python manage.py doctor              # check your setup
python manage.py migrate             # apply schema changes
python manage.py runserver           # start the dev server
python manage.py shell               # Django shell
python verify_schema.py              # verify the accounting rules (fresh DB)
ruff format . && ruff check .        # format and lint before committing
```

## Reporting layer

Available to query directly — Member 4 builds screens on top of these.

**Views:** `v_general_ledger`, `v_control_account_balance`, `v_sales_invoice_open`,
`v_purchase_bill_open`, `v_customer_unapplied_credit`, `v_vendor_unapplied_credit`,
`v_inventory_valuation`, `v_tax_transaction`, `v_subledger_reconciliation`,
`v_money_account_activity`

**Functions:** `fn_trial_balance(from, to)`, `fn_ar_ageing(as_of)`,
`fn_ap_ageing(as_of)`, `fn_stock_card(product, warehouse, from, to)`

## Pending accountant sign-off

These are placeholders (BRD §14.4) and will change. Don't hard-code them.

- Base currency **USD** (OD-02) — cannot change after the first posted transaction
- Standard tax rate **11%** (OD-01)
- Chart of accounts codes and names
- Exempt and zero-rated codes flagged non-recoverable

---

## Troubleshooting

**`connection refused` on migrate** — PostgreSQL isn't running, or `PGPORT` in
`.env` is wrong. macOS: `brew services start postgresql`. Linux:
`sudo systemctl start postgresql`.

**`password authentication failed`** — `PGUSER` / `PGPASSWORD` in `.env` don't
match your local PostgreSQL user.

**`permission denied to create extension`** — your PostgreSQL user isn't a
superuser. Either grant it, or create the extensions once by hand:
`psql -d wams -c "CREATE EXTENSION btree_gist; CREATE EXTENSION pg_trgm;"`

**`InconsistentMigrationHistory`** — your database was created before a
migration was reordered. Fastest fix on a dev machine:
`dropdb wams && createdb wams && python manage.py migrate`.

**Conflicting migration numbers after a pull** — see CONTRIBUTING.md; usually
`python manage.py makemigrations --merge`.

**A constraint or trigger rejected my write** — read the error message; it names
the BRD rule. That is the schema doing its job.
