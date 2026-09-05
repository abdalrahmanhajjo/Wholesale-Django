# Sales Returns — Day 6 feature (RET-001..RET-009)

The physical/authorisation side of a customer return — eligibility tracking,
the **draft → submit → approve → post** lifecycle, and the **reversal journal**
(running stock back in) handed to the ledger posting engine. Delivered by
Member 3.

Scope: the `SalesReturn` / `SalesReturnLine` screens — list, create (posted
invoice picker + remaining quantities + disposition), detail, submit, approve
/ reject, and post. Money-related settlement belongs to the **credit note**
(Day 7, RET-003), not to the return; the return's `total_cost_base` captures
the stock value only.

---

## What was built

| Layer | File | Purpose |
|---|---|---|
| Routes | `apps/sales/urls.py` | return list / create / detail / submit / approve / reject / post |
| Services | `apps/sales/services.py` | RET numbering, remaining-to-return, draft from invoice, recalc, submit, approve, reject, journal builder, post |
| Models | `apps/sales/models.py` | `SalesReturn`, `SalesReturnLine`, `ReturnDisposition` (RESTOCK / WRITE_OFF / NO_STOCK_EFFECT) |
| Views | `apps/sales/views.py` | `SalesReturnListView`, `ReturnInvoiceSelectForm`, `SalesReturnCreateView`, `SalesReturnDetailView`, `SalesReturnSubmitView / ApproveView / RejectView / PostView` |
| Templates | `templates/sales/return_form.html`, `return_detail.html` | picker + draft, detail (both added as part of Day 7 to close the Day 6 gap) |
| Tests | `apps/sales/tests/test_return.py` | eligibility, draft, lifecycle, journal, posting |

The journal **contract** (`JournalDraft`, `PostingRequest`, `PostingError`)
lives in `apps/ledger/services` — Member 4 owns its implementation. The return
reversal reuses the real engine's idempotent, balanced posting path.

## Screens and routes

| URL | View | Requirement |
|---|---|---|
| `/sales/returns/` | `SalesReturnListView` | list + search/filter/export (UX-002, UX-005) |
| `/sales/returns/new/` | `SalesReturnCreateView` | pick a POSTED/PARTIAL/COMPLETED invoice → edit remaining quantities + disposition → draft |
| `/sales/returns/<pk>/` | `SalesReturnDetailView` | detail + audit history (ACC-005) |
| `/sales/returns/<pk>/submit/` | `SalesReturnSubmitView` | DRAFT/REJECTED → SUBMITTED (`change_salesreturn`) |
| `/sales/returns/<pk>/approve/` | `SalesReturnApproveView` | SUBMITTED → APPROVED (`core.approve_sales_return`) |
| `/sales/returns/<pk>/reject/` | `SalesReturnRejectView` | SUBMITTED → REJECTED (`core.approve_sales_return`, reason required) |
| `/sales/returns/<pk>/post/` | `SalesReturnPostView` | SUBMITTED → POSTED via engine (`core.post_sales_return`) |

## Business rules implemented

### Lifecycle

```text
POSTED invoice ─create─▶ DRAFT ─submit──▶ SUBMITTED ─approve─▶ APPROVED ─post──▶ POSTED
(RET-001/002)            (RET-008)         (RET-007)                                    (RET-009)
                                              │
                                              └─reject─▶ REJECTED ─submit─▶ SUBMITTED
```

### Numbering (`allocate_return_number`, NFR-008)
Concurrency-safe `SELECT ... FOR UPDATE` on the `DocumentSequence` row for
`document_type="SR"`, producing `SRT-00001`, ... (seeded with `("SR", "SRT-", 5)`).

### Remaining to return (`remaining_to_return`, RET-001)
```
remaining = invoice_line.quantity − invoice_line.quantity_returned
```
The invoiced-quantity ceiling is also enforced by the database constraint
`si_line_returned_within_invoiced` (BR-015), so eligibility can never exceed
what was billed.

### Draft from an invoice (`draft_return_from_invoice`)
* Only a **POSTED / PARTIAL / COMPLETED** invoice is a candidate (RET-001).
* Quantities must be positive and at most `remaining_to_return` — over-returns
  are rejected with a message naming the line.
* Each returned line is locked with `select_for_update`, then
  `quantity_returned = F("quantity_returned") + qty` on the **invoice line**
  so a second return can't consume the same units.
* Lines link back to the originating invoice line / delivery line and snapshot
  `unit_cost` / `total_cost` from the delivery line (0 until Member 2's costing
  is set).
* `disposition` is RESTOCK / WRITE_OFF / NO_STOCK_EFFECT — it drives whether
  the posting journal restocks inventory.
* Created **DRAFT** with the mandatory `reason` (RET-008); submit, approve and
  post are separate, permission-gated steps.

`recalculate_return` sums line `total_cost` into `SalesReturn.total_cost_base`.

### Journal builder (`build_sales_return_journal`, RET-003)
Builds an immutable `JournalDraft` — no database writes, proportional to the
original invoice snapshots (`returned qty / billed qty`):

| Leg | Account | Amount |
|---|---|---|
| CR | `ACCOUNTS_RECEIVABLE` (1210), tagged with the customer | revenue + tax reversed |
| DR | `SALES_REVENUE` (4100) | revenue reversed |
| DR | `OUTPUT_TAX` (2310) — only when the source line was taxed | tax reversed |
| DR / CR | `INVENTORY` (1310) / `COGS` (5010) — only for RESTOCK lines whose delivery line has `unit_cost > 0` | `unit_cost × qty` |

Accounts are resolved through `AccountMapping` (`_resolve_account`); a missing
mapping raises a loud `PostingError` (CFG-007).

### Posting (`post_return`, RET-009)
Inside one `transaction.atomic()`:
1. Validates the return is **SUBMITTED**.
2. Calls `posting_service.post(...)` with the idempotency key
   `sales-return:{return.pk}:post:v1` and `required_mappings` derived by
   `_return_required_mappings` (always AR + revenue, `OUTPUT_TAX` when the
   invoice was taxed, `COGS` + `INVENTORY` when a RESTOCK line has a costed
   delivery line). The engine enforces the balance and rolls back on any error.
3. On success: flips to POSTED, attaches `journal_entry`, records
   `posted_at`/`posted_by`, records the POST audit event (BR-005, ACC-005).

## Tests

```bash
python manage.py test apps.sales.tests.test_return --keepdb
```

Coverage: eligibility before/after a partial return; draft creates a DRAFT with
lines and links the invoice; draft increments `quantity_returned`; over-return
and unposted-invoice rejection; submit / approve / reject moves (with
`submitted_by`); balanced journal exceeding zero; end-to-end post persists a
`JournalEntry` and flips to POSTED; post requires SUBMITTED; re-posting an
already-posted return is rejected.

## Hand-off notes

* Money follows on a **credit note**, not the return — the return authorises
  and runs stock back in; `SalesCreditNote.sales_return` links the two
  (Day 7).
* The stock-restock journal legs only appear once Member 2's costing sets
  `DeliveryNoteLine.unit_cost`; un-costed lines are skipped by design.