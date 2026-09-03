# Sales Invoices — Day 4 + Day 5 feature (SAL-006..SAL-012)

Sales-invoice **drafting** from a posted delivery note, with the **submit →
post** lifecycle and the **journal draft** handed to the ledger posting engine.
Delivered by Member 3.

Scope: the `SalesInvoice` / `SalesInvoiceLine` screens — list, create (posted
delivery picker + remaining quantities), detail, submit, post, and print. The
**journal persistence** (writing `JournalEntry` / `JournalLine` rows, balance
and idempotency enforcement) lives in Member 4's `PostingEngine`, which the
sales module now binds directly, so posting works **end-to-end**.

---

## What was built

| Layer | File | Purpose |
|---|---|---|
| Routes | `apps/sales/urls.py` | invoice list / create / detail / submit / post / print |
| Services | `apps/sales/services.py` | SI numbering, remaining-to-invoice, draft from delivery, recalc, submit, journal builder, post |
| Forms | `apps/sales/forms.py` | `InvoiceSourceForm`, `DeliverySelectForm`, `SalesInvoiceForm`, `SalesInvoiceLineForm` + formset |
| Views | `apps/sales/views.py` | `SalesInvoiceListView`, `SalesInvoiceCreateView`, `SalesInvoiceDetailView`, `SalesInvoiceSubmitView`, `SalesInvoicePostView`, `SalesInvoicePrintView` |
| Templates | `templates/sales/invoice_form.html`, `invoice_detail.html`, `invoice_print.html` | picker + draft, detail, printable |
| Tests | `apps/sales/tests/test_invoice.py` | numbering, draft, submit, journal, posting contract, views |

The `SalesInvoice` / `SalesInvoiceLine` models already live in
`apps/sales/models.py` (Member 3 owns them). The journal **contract**
(`JournalDraft`, `PostingRequest`, `PostingError`) lives in
`apps/ledger/services` — Member 4 owns its implementation.

## Screens and routes

| URL | View | Requirement |
|---|---|---|
| `/sales/invoices/` | `SalesInvoiceListView` | list + search/filter/export (UX-002, UX-005) |
| `/sales/invoices/new/` | `SalesInvoiceCreateView` | pick a POSTED delivery → edit remaining quantities → draft |
| `/sales/invoices/<pk>/` | `SalesInvoiceDetailView` | detail + audit history (ACC-005) |
| `/sales/invoices/<pk>/submit/` | `SalesInvoiceSubmitView` | DRAFT → SUBMITTED (`change_salesinvoice`) |
| `/sales/invoices/<pk>/post/` | `SalesInvoicePostView` | SUBMITTED → POSTED via engine (`core.post_sales_invoice`) |
| `/sales/invoices/<pk>/print/` | `SalesInvoicePrintView` | printable copy (PTY-003), snapshots only |

Permissions are enforced server-side on each view (ACC-004):
`sales.view_salesinvoice`, `sales.add_salesinvoice`, `sales.change_salesinvoice`,
and `core.post_sales_invoice` (ACCOUNTANT by default in the role matrix).

## Business rules implemented

### Lifecycle

```text
POSTED delivery ─create─▶ DRAFT ─submit──▶ SUBMITTED ─post──▶ POSTED
(SAL-006)                (SAL-008)        (SAL-007)          (SAL-009)
```

Invoices past POSTED flip to PARTIAL/COMPLETED on settlement (payment) — handled
by the shared `_status_badge.html`, no change needed here.

### Numbering (`allocate_invoice_number`, NFR-008)
Concurrency-safe `SELECT ... FOR UPDATE` on the `DocumentSequence` row for
`document_type="SI"`, producing `INV-00001`, ... (seeded with `("SI", "INV-", 5)`).

### Remaining to invoice (`remaining_to_invoice`, SAL-006)
```
remaining = delivery_line.quantity − delivery_line.quantity_invoiced
```
The no-double-invoicing guard, used to build candidate rows, pre-fill
quantities, validate the form and service, and re-check at post time.

### Draft from a delivery (`create_invoice_from_delivery`)
* Only a **POSTED** delivery is a candidate; quantities beyond the remaining
  amount are rejected (double-invoicing blocked, SAL-006).
* Line values are copied from the originating `sales_order_line`; the customer,
  warehouse, dates, currency and rate come from the delivery / order
  (FTD-001/BR-013 snapshots).
* `customer_name_snapshot` / `customer_tax_id_snapshot` /
  `billing_address_text` / `shipping_address_text` are snapshotted (PTY-003).
* Recalculates all totals by reusing the Day-2 arithmetic unchanged
  (`recalculate_invoice` → `calculate_line` / `allocate_document_discount` /
  `calculate_totals` on the `DocumentLineBase` / `FinancialDocumentBase` shape).
* Created **DRAFT**; submit and post are separate, permission-gated steps.

### Journal builder (`build_sales_invoice_journal`, SAL-009 / CFG-007)
Builds an immutable `JournalDraft` — no database writes:

| Leg | Account | Amount |
|---|---|---|
| DR | `ACCOUNTS_RECEIVABLE` (1210), tagged with the customer | `total_base` |
| CR | `SALES_REVENUE` (4100) | `taxable_base_base` |
| CR | `OUTPUT_TAX` (2310) — only when `tax_base > 0` | `tax_base` |
| DR/CR | `ROUNDING_GAIN`/`ROUNDING_LOSS` — only when rounding ≠ 0 | keeps BR-006 balanced |
| DR / CR | `COGS` (5010) / `INVENTORY` (1310) — only for stocked lines with `delivery_line.unit_cost > 0` | `unit_cost × qty` |

Accounts are resolved through `AccountMapping` (`_resolve_account`), never
hard-coded ids; a missing mapping raises `PostingError` naming the key so the
post fails loudly instead of writing to the wrong account (CFG-007).

### Posting (`post_invoice`, SAL-009)
Inside one `transaction.atomic()`:
1. Validates the invoice is **SUBMITTED** (SAL-007).
2. Calls `posting_service.post(PostingRequest(…))`. The service binds the
   **real `PostingEngine`** (Member 4's Day-2 engine), which validates required
   account mappings, enforces a balanced journal, allocates a journal number,
   persists `JournalEntry` / `JournalLine` / `PostingLink`, and honors the
   idempotency key. A missing or invalid mapping raises `PostingError` and the
   whole post rolls back — never a silent no-op.
3. On success: flips to POSTED, attaches `journal_entry`, records
   `posted_at`/`posted_by`, bumps `DeliveryNoteLine.quantity_invoiced`
   (SAL-006), and records the POST audit event (BR-005, ACC-005).

#### Required mappings (`_posting_required_mappings`, Day 5)
The production engine (`posting.py _validate_draft`) rejects any mapping declared
in `required_mappings` whose account does **not** appear in the journal.
Since the invoice journal has **conditional** lines (output tax only when taxed,
rounding only when nonzero, COGS/inventory only for stocked lines with cost),
`required_mappings` must be derived from the same predicates as the builder.

`_posting_required_mappings(invoice)` returns a **tuple** of mapping keys that
mirrors exactly what `build_sales_invoice_journal` will emit:

| Condition | Keys added |
|---|---|
| Always | `ACCOUNTS_RECEIVABLE`, `SALES_REVENUE` |
| `invoice.tax_base > 0` | `OUTPUT_TAX` |
| `invoice.rounding_base != 0` | `ROUNDING_LOSS` or `ROUNDING_GAIN` |
| Any `delivery_line.unit_cost > 0` | `COGS`, `INVENTORY` |

This is called inside `post_invoice` and passed to `PostingRequest(required_mappings=…)`.
A mismatch between the builder and required_mappings causes the production engine to
reject the post before the builder runs (missing mapping) or after (unused mapping)
— both fail loudly by design (BR-005).

### Print (`invoice_print.html`, PTY-003)
Standalone HTML with print CSS; all values come from persisted snapshots, so a
reprint never changes.

## Tests

```bash
python manage.py test apps.sales.tests.test_invoice --keepdb
```

Coverage:

* **Numbering** — `INV-` format; missing SI sequence raises.
* **remaining_to_invoice / build_invoice_lines_from_delivery** — before,
  after a partial invoice, and excluding fully-invoiced lines.
* **create_invoice_from_delivery** — DRAFT invoice with number, customer,
  snapped values, line values copied; rejects unposted deliveries,
  over-invoicing, and empty quantities; records audit.
* **recalculate_invoice** — subtotal/total roll up correctly (2000.00 for
  10×100 + 4×250).
* **submit_invoice** — DRAFT → SUBMITTED; rejects other statuses.
* **CFG-007** — `_resolve_account` raises `PostingError` for a missing mapping.
* **Journal draft** — typed/balanced (DR = CR = `total_base`); AR + revenue
  legs; output-tax leg when taxed; COGS/INVENTORY legs when `unit_cost > 0`;
  zero-cost lines skipped.
* **Posting contract** — requires SUBMITTED; end-to-end post against the real
  engine persists a balanced `JournalEntry` (DR = CR), flips the invoice to
  POSTED, and records `posted_by`; a re-post of an already-POSTED invoice is
  rejected and the idempotency key dedupes; a missing account mapping rolls the
  whole post back leaving the invoice SUBMITTED with no journal and
  `quantity_invoiced` at zero (BR-005: nothing is half-posted).
* **Required mappings** (Day 5) — AR + revenue always present; OUTPUT_TAX
  excluded on tax-free invoices; COGS/INVENTORY excluded when no delivery line
  has `unit_cost > 0`; every declared key maps to an account actually present
  in the journal.
* **Views** — list, picker, create, over-invoice rejection, submit,
  permission denial on post without `core.post_sales_invoice`, graceful
  redirect when the engine is unavailable, detail, and print.

> **Note on `--keepdb`.** Same shared-DB constraint as Days 2–3 — the suite
> reuses the existing `test_postgres` database, so run with `--keepdb`. Tests
> seed the CFG-007 account mappings defensively and clear the `SI` sequence
> before asserting the missing-sequence error, so they are resilient to residue
> between runs.

## Hand-off notes

* The sales module now binds the **real `PostingEngine`** in
  `apps/sales/services.py` (`posting_service = PostingEngine()`). Nothing else
  in the module changes: the call site uses the real `PostingRequest` /
  `JournalDraft` contract and surfaces `PostingError`.
* The engine honors the SAL-009 idempotency key
  `sales-invoice:{invoice.pk}:post:v1` (GL-002) so a retry never double-posts.
* `required_mappings` is already supplied by `_posting_required_mappings()`.
  The engine validates that every declared key resolves to an active, postable
  account and that no declared key is absent from the journal
  (posting.py `_validate_draft` rule).
* **COGS/INVENTORY legs are now live.** The sales delivery-posting path
  (`post_delivery`) delegates the stock costing to Member 2's engine
  (`apps.inventory.services.post_delivery`), which costs each delivery line at
  the warehouse's weighted average (INV-005), sets `unit_cost`/`total_cost`,
  and posts the COGS-vs-Inventory journal. When an invoice is posted, the
  invoice journal includes COGS/Inventory legs for every costed line
  (SAL-010); lines with zero cost are skipped.
* The sales layer retains the **over-delivery guard** (SAL-005) and
  **order status sync** (PARTIAL/COMPLETED) which the inventory engine
  does not implement.
* `verify_schema.py` already opens an open period and checks
  `AccountMapping.objects.count() == len(MappingKey.choices)` as part of the
  end-to-end posting evidence.