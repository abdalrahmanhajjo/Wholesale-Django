# Sales Credit Notes — Day 7 feature (RET-003, RET-004, SAL-007, BR-016)

The financial reversal of a sale: reduces AR and reverses revenue/tax
**proportionally from the original invoice snapshots**, through the same
submit → approve → post lifecycle as returns. Delivered by Member 3.

Scope: the `SalesCreditNote` / `SalesCreditNoteLine` screens — list, create
(posted invoice picker + quantities to credit + mandatory reason), detail,
submit, approve / reject, and post. The credit note is **financial only**: it
never touches inventory and never consumes a return's `quantity_returned`
eligibility. Its unapplied remainder becomes **customer credit**
(RET-004, BR-016) for Member 4's payment allocation / refund modules.

---

## What was built

| Layer | File | Purpose |
|---|---|---|
| Routes | `apps/sales/urls.py` | credit-note list / create / detail / submit / approve / reject / post |
| Services | `apps/sales/services.py` | CN numbering, draft from invoice, recalc, submit, approve, reject, journal builder, post |
| Models | `apps/sales/models.py` | `SalesCreditNote` / `SalesCreditNoteLine`; `submitted_by` added in migration `0004_salescreditnote_submitted_by` |
| Views | `apps/sales/views.py` | `CreditNoteListView`, `CreditNoteInvoiceSelectForm`, `CreditNoteCreateView`, `CreditNoteDetailView`, `CreditNoteSubmitView / ApproveView / RejectView / PostView` |
| Templates | `templates/sales/credit_note_form.html`, `credit_note_detail.html` | picker + draft, detail |
| Tests | `apps/sales/tests/test_credit_note.py` | draft, lifecycle, journal, posting, tax reversal |

The credit-note **models already existed** in `0001_initial` (with the
`cn_*` constraints and the `ix_cn_open_credit` partial index); Day 7 adds the
workflow glue and the reversal journal. The journal **contract** lives in
`apps/ledger/services` (Member 4).

## Screens and routes

| URL | View | Requirement |
|---|---|---|
| `/sales/credit-notes/` | `CreditNoteListView` | list + search/filter/export (UX-002, UX-005) |
| `/sales/credit-notes/new/` | `CreditNoteCreateView` | pick a POSTED/PARTIAL/COMPLETED invoice → quantities to credit + reason → draft |
| `/sales/credit-notes/<pk>/` | `CreditNoteDetailView` | detail + audit history (ACC-005) |
| `/sales/credit-notes/<pk>/submit/` | `CreditNoteSubmitView` | DRAFT/REJECTED → SUBMITTED (`change_salescreditnote`) |
| `/sales/credit-notes/<pk>/approve/` | `CreditNoteApproveView` | SUBMITTED → APPROVED (see permissions note below) |
| `/sales/credit-notes/<pk>/reject/` | `CreditNoteRejectView` | SUBMITTED → REJECTED (reason required, ACC-008) |
| `/sales/credit-notes/<pk>/post/` | `CreditNotePostView` | SUBMITTED → POSTED via engine (`core.post_credit_note`) |

### Permissions note (pending)
`core.post_credit_note` already exists in the seed list
(`ACTION_PERMISSIONS`) — posting is live. There is **no**
`approve_sales_credit_note` permission yet; it is being added by the sales
owner via migration `0013_grant_sales_credit_note_permissions` (creates the
row and grants it to Owner/Admin + Accountant) together with the
`APPROVE_SALES_CREDIT_NOTE` constant in `apps/core/permissions.py`. Until that
lands, `CreditNoteApproveView` / `CreditNoteRejectView` fall back to
`core.approve_sales_return` as a placeholder.

## Business rules implemented

### Lifecycle

```text
POSTED invoice ─create──▶ DRAFT ─submit──▶ SUBMITTED ─approve──▶ APPROVED ─post──▶ POSTED
(RET-003)                                (SAL-007)
                                              │
                                              └─reject─▶ REJECTED ─submit─▶ SUBMITTED
```

### Numbering (`allocate_credit_note_number`, NFR-008)
Concurrency-safe `SELECT ... FOR UPDATE` on the `DocumentSequence` row for
`document_type="CN"` (seeded with `("CN", "CN-", 5)`), producing `CN-00001`, ...

### Draft from an invoice (`draft_credit_note_from_invoice`)
* Only a **POSTED / PARTIAL / COMPLETED** invoice is a candidate (RET-003).
* Quantities must be positive and at most the **invoiced** quantity
  (`il.quantity`) — you cannot credit more than was billed.
* Financial only: **no** `quantity_returned` increment, **no** stock effect —
  returns and credit notes are independent (a return restocks; a credit note
  reverses money).
* Each line snapshots `unit_price`, `discount_percent`, `tax_rate_percent`,
  `tax_is_inclusive` / `tax_is_recoverable`, and the billing `tax_code` /
  `revenue_account` from the invoice line, exactly like the invoice itself —
  re-resolving a price later (Member 1's pricing work) can never alter
  history.
* The `reason` is **mandatory** (RET-008).
* Recalculates all totals by reusing the Day-2 arithmetic unchanged
  (`recalculate_credit_note` → `calculate_line` / `calculate_totals` on the
  `DocumentLineBase` / `FinancialDocumentBase` shape).
* Created **DRAFT**; submit / approve / post are separate, permission-gated
  steps.

### Open credit (RET-004, BR-016)
The database enforces `open_txn = total_txn − allocated_txn − refunded_txn`
(`cn_open_is_derived`, `cn_open_nonneg`) and that applied + refunded never
exceed the credit (`cn_settlement_within_total`). The partial index
`ix_cn_open_credit` surfaces still-open credit for Member 4's
`PaymentAllocation` / `Refund`.

### Journal builder (`build_sales_credit_note_journal`, SAL-007 / RET-003)
Builds an immutable `JournalDraft` — no database writes, proportional to the
original invoice snapshots (`credited qty / billed qty`), mirroring
`build_sales_return_journal` **without the stock legs**:

| Leg | Account | Amount |
|---|---|---|
| CR | `ACCOUNTS_RECEIVABLE` (1210), tagged with the customer | revenue + tax reversed |
| DR | `SALES_REVENUE` (4100) | revenue reversed |
| DR | `OUTPUT_TAX` (2310) — only when the source line was taxed | tax reversed |

### Posting (`post_credit_note`)
Inside one `transaction.atomic()`:
1. Validates the credit note is **SUBMITTED**.
2. Calls `posting_service.post(...)` with the idempotency key
   `sales-credit-note:{cn.pk}:post:v1` and `required_mappings` derived by
   `_credit_note_required_mappings` (always AR + revenue; `OUTPUT_TAX` only when
   a referenced invoice line is taxed — it must mirror the builder's conditional
   leg or the engine rejects the post).
3. On success: flips to POSTED, attaches `journal_entry`, records
   `posted_at`/`posted_by`, records the POST audit event (BR-005, ACC-005). The
   posted credit then carries `open_txn > 0` until M4 allocates or refunds it.

## Tests

```bash
python manage.py test apps.sales.tests.test_credit_note --keepdb
```

16 tests: draft creates a DRAFT with lines linked to the invoice (snapshotted
`unit_price`, correct `total_txn`); drafting does **not** touch
`quantity_returned`; over-credit and unposted-invoice rejection; mandatory
reason; submit / approve / reject (with `submitted_by` / timestamps); reject
requires a reason; balanced reversal journal exceeding zero; AR leg credited
exactly (`500.00` for 5 × 100); end-to-end post persists a `JournalEntry` and
flips to POSTED; post requires SUBMITTED; re-posting is rejected; output-tax
leg appears and posts for a taxed invoice.

## Hand-off notes

* The credit note's open credit is ready to be consumed by Member 4's payment
  allocation / refund flow — nothing further is needed on the sales side.
* Publishing the `APPROVE_SALES_CREDIT_NOTE` permission (constant +
  `0013` grant migration + view swap) is the only remaining wiring for the
  approval step.
* Once Member 1's `price_for()` lands, sales lines must resolve prices at entry
  time and keep snapshotting — the credit-note reversal already reads the
  snapshots and so stays correct automatically.