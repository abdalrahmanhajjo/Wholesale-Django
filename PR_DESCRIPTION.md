# Fix: zero-value lines crashing purchase bill / vendor debit note posting

Found while manually testing the purchase-returns/debit-notes PR after it
landed on `dev`: posting a vendor debit note with a line that had no unit
price set crashed with a raw `IntegrityError` (`journal_line_debit_xor_credit`)
instead of a clean message. The same latent bug existed in `post_purchase_bill`
— it just hadn't been hit yet, since every existing test happened to use a
non-zero price.

## What's wrong and why

A journal line can never have both its debit and credit sides at zero — the
database enforces that unconditionally. But nothing stopped a document line
with `unit_price = 0` (or a resulting `taxable_base_txn` of exactly zero) from
reaching the posting service, which then tried to write a journal line with
both sides at zero for that line. The database is right to reject it; the
services just weren't guarding against it, so the clerk got a 500 instead of
a clear explanation.

## What's fixed

- **`post_purchase_bill`** and **`post_vendor_debit_note`**: skip writing a
  journal line for any line whose `taxable_base_txn` is zero — it has nothing
  to book, same reasoning already used for the tax line (`if line.tax_txn:`).
  This doesn't affect the document's total or BR-006 balance, since a
  zero-value line was already contributing zero either way.
- Both services now also refuse to post a document whose **total** is zero,
  with a clear `ValidationError` instead of crashing on the final
  Accounts-Payable line.
- **`VendorDebitNoteLineForm`** now rejects a `$0` unit price outright, with
  a clear field error. `PurchaseBillLineForm` was deliberately *not* given
  the same form-level block — a free/bonus line at $0 is a real scenario for
  a bill, so that side is only guarded at the service level, not blocked at
  entry.

## Files touched
Modified: `apps/purchases/services.py`, `apps/purchases/forms.py`,
`apps/purchases/tests/test_purchase_returns_debit_notes.py` (a pre-existing
lint fix — `assert` → `self.assertEqual` — caught by `ruff check` while
working on this).

No new migrations.

## Testing
- `python manage.py test apps.purchases apps.inventory` — **70/70 pass**
  (65 from before + 5 new since the last report), run against the real
  Supabase database.
- Manually reproduced the original crash (a debit note line with
  `unit_price=0`), confirmed it now returns a clean form error instead of a
  500, and confirmed a corrected line posts successfully.
- `ruff check` / `ruff format` — clean.
