# Purchase returns and vendor debit notes (Member 2, Day 7)

Days 3-6 covered the forward purchase cycle (orders → receipts → bills) and
warehouse ops (transfers, adjustments). This PR adds the reverse side: goods
shipped back to a vendor, and the credit that follows — the same
physical/financial split the sales side uses for customer returns
(`PurchaseReturn` mirrors `SalesReturn`; `VendorDebitNote` mirrors a credit
note), so the shape is consistent everywhere a document is un-done.

## What's new

### Purchase returns (`/purchases/returns/`)
`PurchaseReturn` / `PurchaseReturnLine` already existed from the foundation
schema; this PR adds the application layer:

- **List / create-edit / detail** screens, plus a "Return goods" link from a
  posted bill's detail page.
- **Posting** (`apps/purchases/services.py::post_purchase_return`, RET-005):
  each stock-affecting line (`RESTOCK`/`WRITE_OFF` disposition) posts a
  `PURCHASE_RETURN_OUT` movement through the shared costing engine — costed
  at the warehouse's *current* average, same as any other outbound movement
  — while a `NO_STOCK_EFFECT` line (a paperwork-only correction) moves no
  stock at all. Every line, regardless of disposition, checks and consumes
  return eligibility against whichever original line it traces back to
  (BR-015), so the vendor can't be credited twice for the same units.
- **No journal on the return itself, on purpose.** Unlike every other
  posted document here, `PurchaseReturn` has no "posted must have a
  journal" DB constraint — money follows on the debit note, exactly like
  `SalesReturn`'s own docstring says for the sales side ("money follows on a
  credit note"). `OVERRIDE_RETURN_QUANTITY` isn't wired to bypass the
  eligibility check for the same reason `OVERRIDE_NEGATIVE_STOCK` and
  `OVERRIDE_DUPLICATE_VENDOR_INVOICE` weren't in earlier PRs: the database
  enforces the limit unconditionally, so a permission-gated override here
  would be misleading.

### Vendor debit notes (`/purchases/debit-notes/`)
Same shape for `VendorDebitNote` / `VendorDebitNoteLine`:

- **List / create-edit / detail** screens, plus a "Create debit note" link
  from a posted return's detail page, prefilling each line from whatever
  bill line it traces back to.
- **Posting** (`apps/purchases/services.py::post_vendor_debit_note`,
  RET-006): the exact mirror of `post_purchase_bill`, credited instead of
  debited — a stock line credits Inventory directly (by bill-posting time
  its value had already left GRNI one way or another, so there's no accrual
  left to re-touch), a non-stock line credits its own expense account or
  falls back to the dedicated **Purchase Returns** contra account (5150)
  rather than Purchase Expense, so returns show as their own line instead
  of silently netting against gross purchases, recoverable tax credits
  Input Tax, and everything debits Accounts Payable (BR-006 balanced). If a
  line traces back to a bill line directly (no physical return behind it —
  a pure pricing correction), *this* is what consumes that bill line's
  return eligibility instead — never both, so the same units are never
  credited twice.
- **Never touches physical stock.** A debit note with no return behind it
  has nothing physical to reverse, same as how a bill never itself moves
  stock — only a receipt or a return does.
- `recalculate_debit_note()` reuses `_recalculate_line()`'s per-line tax
  math but is its own function, not a call to `_recalculate_document()`:
  that function settles `credited_txn`, a field `VendorDebitNote` doesn't
  use — it settles against `refunded_txn` instead (RET-007) — and allocates
  a header discount the model has no field for.

## Files touched
Modified: `apps/purchases/{admin,forms,models,services,urls,views}.py`,
`templates/base.html` (nav links), `templates/purchases/bill_detail.html`
(the "Return goods" cross-link).
New: `templates/purchases/{purchase_return_form,purchase_return_detail,_pr_line_row}.html`,
`templates/purchases/{vendor_debit_note_form,vendor_debit_note_detail,_dbn_line_row}.html`,
`apps/purchases/tests/test_purchase_returns_debit_notes.py`.

No new migrations: `PurchaseReturn.get_absolute_url()` and
`VendorDebitNote.get_absolute_url()` are methods, not fields; every model
field and constraint used here already existed.

## Testing
- `python manage.py test apps.purchases apps.inventory` — **65/65 pass**,
  run against the real Supabase database.
- New tests in `test_purchase_returns_debit_notes.py`: numbering and the
  mandatory-reason check, posting ships a restocked line back out at the
  current average, a `NO_STOCK_EFFECT` line moves no stock, returning more
  than was billed is refused, the debit note books a balanced AP/inventory/
  tax reversal, a non-stock line credits Purchase Returns not Purchase
  Expense, a debit note created from a posted return doesn't double-count
  eligibility, and that Purchasing (as opposed to Accountant/Owner-Admin)
  can create but not post either document.
- `ruff check` / `ruff format` — clean.
- `manage.py makemigrations purchases --check` — no changes detected.

## Follow-ups (out of scope here)
- Applying a debit note's open balance to a future bill, or refunding it,
  is the payments app's allocation mechanism (RET-007) — not built here,
  same as a purchase bill's own payment allocation isn't.
