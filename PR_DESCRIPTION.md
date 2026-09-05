# Day 8 buffer: three-way match indicator, low-stock view

Day 8 is buffer/polish. Two small additions, both read-only (no new
migrations, no change to posting):

## Three-way match indicator (purchases)

`PurchaseOrderLine` already tracked `quantity_received` and `quantity_billed`
(PUR-003 / PUR-012) but nothing surfaced them — a buyer had to compare
numbers by eye across the order, its receipts and its bills. The PO detail
screen now shows a **Match** badge per line, plus the received/billed
quantities themselves:

- **Not received** — nothing received or billed yet.
- **Partial** — received or billed, but short of the ordered qty (minus
  anything cancelled).
- **Matched** — received and billed in full.
- **Over-billed** — billed more than received. Shouldn't happen given the
  posting guards, but if it ever does, it's now visible instead of silent.
- **Cancelled** — the line's ordered qty was fully cancelled.

`match_status` is a plain model property (`apps/purchases/models.py`) — no
new field, no migration — computed from quantities the model already had.
Covered by `apps/purchases/tests/test_three_way_match.py`, a DB-free
`SimpleTestCase` since the property only reads in-memory fields.

## Low-stock view (inventory)

Inventory valuation already summarised a "Below reorder level" count but
had no screen to act on it. Added `LowStockListView`
(`apps/inventory/views.py`, `/inventory/stock/low/`) — the same
`StockBalance` rows as inventory valuation, filtered to
`quantity_on_hand <= product.reorder_level`, with a warehouse filter and
CSV export like every other list screen. Linked from the Inventory nav.

## Bug fixes

Checked `post_goods_receipt`, `post_stock_transfer` and
`post_stock_adjustment` against the same zero-value-line crash class fixed
in the previous PR (a journal line can't have both sides at zero). All
three already guard at the document-total level (`if total_cost > ZERO`,
etc.), so no fix was needed there. No other reported blockers to fix as of
this PR — will fold in anything teammates flag before Day 8 wraps.

## Files touched
Modified: `apps/purchases/models.py`, `apps/inventory/views.py`,
`apps/inventory/urls.py`, `templates/base.html`,
`templates/purchases/purchase_order_detail.html`.
New: `apps/purchases/tests/test_three_way_match.py`.

No new migrations (`manage.py makemigrations purchases inventory --check`
— no changes detected).

## Testing
- `apps.purchases.tests.test_three_way_match` — 7/7 pass (no DB).
- `manage.py check` — clean.
- `ruff check` / `ruff format` — clean.
- Did **not** run the full `apps.purchases apps.inventory` suite this
  round: the test database is the shared Supabase instance (via Supavisor),
  and it had a stale connection blocking recreation — didn't want to keep
  terminating backends on a database teammates may also be using. Worth
  running before merge once it's clear to do so.
