# Sales Orders — Day 2 feature (SAL-001..SAL-004)

Sales-order **entry** and the **approval workflow**, delivered by Member 3.

Scope: the `SalesOrder` / `SalesOrderLine` screens — list, create, edit, detail,
and the submit → approve / reject lifecycle. Posting to the ledger is out of
scope for this day and flows through Member 4's posting engine later.

---

## What was built

| Layer | File | Purpose |
|---|---|---|
| Routes | `apps/sales/urls.py` | `app_name = "sales"`, wired into `config/urls.py` |
| Services | `apps/sales/services.py` | numbering, line math, discount allocation, totals, lifecycle |
| Forms | `apps/sales/forms.py` | `SalesOrderForm` + line inline formset |
| Views | `apps/sales/views.py` | list / create / edit / detail / submit / approve / reject |
| Templates | `templates/sales/so_form.html`, `so_detail.html` | entry + detail screens |
| Tests | `apps/sales/tests/` | `factories.py`, `test_services.py`, `test_views.py` |
| Admin | `apps/sales/admin.py` | `SalesOrderAdmin` (autocomplete on customer) |

All money is `Decimal` (BR-001) and every read/write path goes through a
service rather than the view (NFR-014). Views follow the `apps/parties/views.py`
worked example — `ActionPermissionMixin` for gating and `FilteredListView` for
the shared list pattern.

## Screens and routes

| URL | View | Requirement |
|---|---|---|
| `/sales/orders/` | `SalesOrderListView` | UX-002, UX-005 (search, filters, sort, export) |
| `/sales/orders/new/` | `SalesOrderCreateView` | SAL-001 entry |
| `/sales/orders/<pk>/` | `SalesOrderDetailView` | detail + audit history (ACC-005) |
| `/sales/orders/<pk>/edit/` | `SalesOrderUpdateView` | SAL-001 edit |
| `/sales/orders/<pk>/submit/` | `SalesOrderSubmitView` | SAL-004 DRAFT → SUBMITTED |
| `/sales/orders/<pk>/approve/` | `SalesOrderApproveView` | SAL-004, ACC-008 |
| `/sales/orders/<pk>/reject/` | `SalesOrderRejectView` | SAL-004, ACC-008 |

Permissions are enforced **server-side** on the views (ACC-004). Editing is
only allowed while the order is editable — DRAFT, SUBMITTED or REJECTED —
matching `EDITABLE_STATES`.

## Business rules implemented

### Numbering (`allocate_so_number`, NFR-008)
Concurrency-safe allocation using `SELECT ... FOR UPDATE` on the
`DocumentSequence` row for `document_type="SO"`, producing `SO-00001`, `SO-00002`,
... Raises `ValueError` if no active SO sequence is configured.

### Line arithmetic (`calculate_line`, BR-010 / BR-011 / FTD-006)

```
gross        = quantity × unit_price
line_discount= gross × discount_percent / 100   (clamped to gross)
net          = gross − line_discount − allocated_document_discount  (clamped ≥ 0)
taxable_base = net                              (exclusive tax)
             = net / (1 + rate/100)             (inclusive tax)
tax          = taxable_base × rate / 100
total        = taxable_base + tax
```

### Header discount allocation (`allocate_document_discount`, SAL-003 / BR-011)
A document-level discount is spread across lines **proportionally to each
line's gross**, written to `allocated_document_discount_txn`. The last line
absorbs any rounding remainder so the allocations reconcile to the header.

### Totals roll-up (`calculate_totals`, BR-022)
Subtotals, discounts, taxable base, tax and total are aggregated from the
lines; `total` is rounded to 2 dp with the difference stored in `rounding_txn`,
tolerated within the company's rounding allowance. Base-currency mirrors are
derived from the header `exchange_rate`. `recalculate_order` runs the full
pass — allocate → per-line calculate → totals — after any change to lines,
prices, quantities, discounts or the header discount.

### Approval lifecycle (`submit/approve/reject_order`, SAL-004, ACC-008)

```
DRAFT ──submit──▶ SUBMITTED ──approve──▶ APPROVED
   ▲                  │
   └────── edit ◀─REJECTED◀──reject─────┘
```

* Submit: DRAFT or REJECTED → SUBMITTED.
* Approve / reject: only from SUBMITTED, both require an explicit reason
  (ACC-008) and the `core.approve_sales_order` permission.
* Rejected orders stay editable and can be fixed and resubmitted.

Every state change is audited via `apps/core/audit.py` inside the same
`transaction.atomic()` block (ACC-005, BR-005).

## Entry forms

* `SalesOrderForm` — header fields; validates that a percentage document
  discount is between 0 and 100; limits customer / warehouse / payment-term
  dropdowns to active records.
* `SalesOrderLineForm` + `SalesOrderLineFormSet` — up to 10 lines, product
  chosen with price/tax auto-populated by the browser JS.
* `line_no` is **hidden and optional**: the view assigns `1, 2, 3...` at save
  time via the `_number_lines()` helper, so the user never types it.

## Tests

```bash
python manage.py test apps.sales.tests
```

Coverage:

* **Numbering** — sequential allocation and the sequence row advancing; error
  when no SO sequence exists.
* **Line arithmetic** — exclusive and inclusive tax, 100% discount leaving net
  zero, document-discount allocation never driving net negative.
* **Document discount** — proportional split reconciles to the header; a zero
  header discount clears line allocations.
* **Totals roll-up** — header totals reconcile to the lines.
* **Approval workflow** — submit, approve, reject, and resubmit from REJECTED.
* **Views** — approve / reject / submit are denied (403) at the URL for a user
  without the right permission (ACC-004); detail and list render.

> **Note on running tests with a shared Supabase DB.** The team DB is shared
> and the test user cannot always `CREATE DATABASE`, so `manage.py test` may
> fail to build the throwaway test database. Run with `--keepdb` to reuse the
> existing `test_*` database:
>
> ```bash
> python manage.py test apps.sales.tests --keepdb
> ```

## Open items / notes

* Posting an approved order to the ledger is **not** part of this day; it
  lands when Member 4's posting engine is wired in.
* The shared stylesheet is compiled at build time and committed; after changing
  template class names, run `npm run build:css` as documented in the README.
* Shared-file edits touched by this work were flagged to Member 1 per
  CONTRIBUTING.md: `templates/core/list_base.html` (live-search script) and
  `templates/_theme.html` (removed a developer comment).
