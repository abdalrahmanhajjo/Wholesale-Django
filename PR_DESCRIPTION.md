# Goods receipts + purchase bills (Member 2, Days 3–4)

Adds the next two steps of the purchase cycle on top of the purchase-order
work already merged: receiving goods against a PO (or directly), and billing
a vendor invoice. Together with purchase orders this closes the PUR-001
through PUR-008 loop — a purchasing clerk can now go from PO to receipt to
bill without leaving the app.

## What's new

### Goods receipts (`apps/inventory`)
The `GoodsReceipt` / `GoodsReceiptLine` models already existed from the
foundation schema; this PR adds the application layer around them:

- **List / create-edit / detail** screens, following the same shared
  list/form pattern as purchase orders.
- **Accept/reject split** (PUR-004): a clerk enters quantity received and
  quantity rejected per line; accepted quantity and unit cost are derived
  server-side, never taken from the form.
- **"Receive goods" from an approved PO** (PUR-003): the create screen
  accepts `?po=<id>` and prefills the header and one line per still-open PO
  line (ordered qty − already received). A "Receive goods" button was added
  to the PO detail page for this.
- **Posting** (`apps/inventory/services.py::post_goods_receipt`, INV-006):
  for each accepted line, writes an immutable `StockMovement`, updates
  `StockBalance` (quantity on hand + weighted-average cost) under a row lock
  so two concurrent receipts of the same item can't race, and books a
  balanced journal — **Dr Inventory / Cr Goods Received Not Invoiced**
  — using the accounts configured in `AccountMapping` (CFG-007). A receipt
  where every line is rejected posts with no journal, which the existing DB
  constraint on `GoodsReceipt` already expected.
- Feeds `PurchaseOrderLine.quantity_received` back so the three-way match
  (PUR-012) has real numbers to work with later.

### Purchase bills (`apps/purchases`)
Same shape: `PurchaseBill` / `PurchaseBillLine` already existed; this PR adds:

- **List / create-edit / detail** screens. A line is either a stock line
  (product) or a non-stock line (expense account) — Appendix A's posting
  split, enforced both in the form and by the existing DB constraint.
- **"Create bill" from an approved PO or a posted receipt**: same `?po=<id>`
  prefill pattern, offered from both the PO detail page and the goods
  receipt detail page once it's posted.
- **Duplicate vendor-invoice detection** (PUR-006): `PurchaseBillForm.clean()`
  checks for an existing bill with the same vendor + vendor invoice number
  (respecting the `Company.block_duplicate_vendor_invoice` toggle) and
  raises a field error naming the earlier bill, instead of letting the
  clerk hit the database's `pb_vendor_invoice_unique` constraint as a raw
  500.
- **Posting** (`apps/purchases/services.py::post_purchase_bill`, PUR-008):
  books a balanced journal — the tax-exclusive amount of each stock line
  debits **Inventory**, or **Goods Received Not Invoiced** instead if the
  line is linked back to a receipt line (clearing the accrual the receipt
  posted); each non-stock line debits its expense account; recoverable tax
  debits **Input Tax**; everything credits **Accounts Payable** against the
  vendor. Sets `open_txn`/`open_base` so the bill is immediately visible as
  an open item for payment allocation later.

### Refactor in `apps/purchases/services.py`
The purchase-order discount/tax arithmetic (`recalculate_order`) was
extracted into a shared `_recalculate_document()` used by both orders and
bills, since they're the same `FinancialDocumentBase` shape. While doing
that I also fixed a gap in the original PO code: line-level `net_base`,
`taxable_base_base`, `tax_base` and `total_base` were never populated (only
the header totals were converted to base currency). That's harmless for a
PO, which never posts, but it would have been silently wrong for a bill,
which does — the posting service needs the per-line base-currency amounts
to write correct journal lines.

## Files touched
New: `apps/inventory/{services,forms,views,urls,admin}.py`,
`apps/inventory/tests/test_goods_receipts.py`,
`apps/purchases/tests/test_purchase_bills.py`,
`templates/inventory/*`, `templates/purchases/bill_*.html`,
`templates/purchases/_bill_line_row.html`.
Modified: `apps/purchases/{services,forms,views,urls,admin,models}.py`,
`apps/inventory/models.py` (added `get_absolute_url` only — no migration),
`config/urls.py`, `templates/base.html`, `templates/purchases/purchase_order_detail.html`.

No new migrations: the only model changes are `get_absolute_url()` methods.

## Testing

- `manage.py check` — clean.
- `manage.py makemigrations --check` — no changes detected.
- Every new/changed module imports cleanly and every new URL name reverses.
- Every new/changed template parses.
- `python manage.py test apps.purchases apps.inventory` — **32/32 pass**,
  run against the real Supabase database. This run also confirms nothing in
  the existing purchase-order suite regressed.
- `ruff check` / `ruff format` — clean.

Note for whoever pulls this branch: the project's `.venv` was rebuilt on
**Python 3.12** (was 3.14). Django 5.1 doesn't yet support 3.14 — its test
client crashes (`AttributeError: 'super' object has no attribute 'dicts'`)
on any response that renders a template, which was silently failing even
the pre-existing purchase-order tests before this fix. If your own `.venv`
is still on 3.14, recreate it against 3.12 or 3.13.

Two real bugs surfaced and were fixed while getting the suite green:
- `PurchaseBill.open_txn`/`open_base` weren't kept in sync with the total on
  every save — only at posting — which violates the `pb_open_is_derived` DB
  constraint the moment a draft bill's lines are saved. Fixed in
  `_recalculate_document()`.
- Two bugs in my own new tests (not the app code): one reused a stale
  in-memory object after an HTTP call had changed its status in the DB, and
  one passed a raw date string to `.objects.create()` instead of a `date`,
  bypassing the parsing a form would normally do.

New tests added:
- `apps/inventory/tests/test_goods_receipts.py` — numbering, the
  accept/reject split and derived cost, weighted-average blending across two
  receipts, the balanced stock journal, status locking, and that a
  Purchasing-group user (as opposed to Warehouse) cannot post a receipt.
- `apps/purchases/tests/test_purchase_bills.py` — numbering, the duplicate
  vendor-invoice error, the tax/discount arithmetic, the balanced AP + tax
  journal, that a receipt-linked line clears GRNI instead of Inventory, and
  that Purchasing cannot post a bill (only Accountant/Owner-Admin can).

## Follow-ups (out of scope here)
- Purchase returns / vendor debit notes (Day 7) will need to reverse both
  the stock movement and the AP/tax journal this PR writes.
- The duplicate-invoice check doesn't implement an override path — the
  underlying DB constraint is a hard block regardless of permission, so an
  `OVERRIDE_DUPLICATE_VENDOR_INVOICE`-gated UI would be misleading until
  that's revisited.
