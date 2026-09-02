# Inventory core: weighted-average costing engine + stock ledger/valuation (Member 2, Day 5)

Goods receipts and purchase bills (previous PR) proved the posting shape end
to end, but the weighted-average balance math lived inline inside
`post_goods_receipt` — the next stock-moving document (delivery, transfer,
adjustment) would have had to copy it. This PR pulls that math out into a
shared costing engine and adds the two read screens INV-004/INV-005 promised:
a stock ledger and an inventory valuation view.

## What's new

### The costing engine (`apps/inventory/services.py`)
`post_stock_movement()` is now the one place that applies a movement to a
product/warehouse balance:

- Takes `SELECT ... FOR UPDATE` on the `StockBalance` row so two concurrent
  movements of the same item serialise instead of racing each other's
  read-modify-write of the running average (NFR-003, NFR-008) — the same
  guarantee `post_goods_receipt` already had, now shared.
- **Inbound** (`GOODS_RECEIPT`, `SALES_RETURN_IN`, `TRANSFER_IN`,
  `ADJUSTMENT_IN`, `OPENING`): blends the caller's `unit_cost` into the
  running weighted average.
- **Outbound** (`DELIVERY`, `PURCHASE_RETURN_OUT`, `TRANSFER_OUT`,
  `ADJUSTMENT_OUT`, `WRITE_OFF`): costs the line at the *current* average and
  ignores any `unit_cost` the caller passes — the balance is the only source
  of truth for what stock leaving the warehouse is worth (INV-005). Direction
  is derived from `movement_type` rather than asked for, so it can't drift
  from the DB's `stock_movement_direction_matches_type` constraint.
- Zeroes the balance's value exactly when a movement brings quantity to zero,
  instead of leaving a rounding residue behind.
- BR-017's negative-stock policy is still enforced by the
  `wams_stock_negative_check` database trigger, not duplicated here — this
  only catches the resulting `IntegrityError` and re-raises it as a
  `ValidationError` a view can display.

`post_goods_receipt` was refactored to call this instead of computing the
balance itself; its behaviour (and every existing test for it) is unchanged.
Deliveries, transfers and adjustments — not yet built — will post through the
same function rather than reimplementing it.

### Stock ledger (`/inventory/stock/ledger/`)
Every posted `StockMovement`, filterable by warehouse and movement type and
searchable by SKU/name/document number, with the running balance quantity,
average cost and value each line left behind (RPT-016, RPT-017) — the
on-screen equivalent of the existing `fn_stock_card()` function, across every
product rather than one at a time.

### Inventory valuation (`/inventory/stock/valuation/`)
Quantity on hand, reserved, average cost and value per product/warehouse,
filterable by warehouse, plus a below-reorder-level count in the summary row
(RPT-018) — the on-screen equivalent of `v_inventory_valuation`, backed
directly by `StockBalance` so it's always current rather than recomputed.

Both screens are built on the existing `FilteredListView` pattern (search,
filter, sort, pagination, CSV export, all for free) and are cross-linked: a
valuation row's "View" link opens the ledger pre-filtered to that item; a
ledger row's "View" link opens its source document when there is one (e.g.
the goods receipt that created it).

## Files touched
Modified: `apps/inventory/{services,views,urls,models}.py`,
`templates/base.html` (nav links for the two new screens; narrowed the
"Goods receipts" link's active-state check so it no longer lights up for
them).
New: `apps/inventory/tests/test_stock_ledger.py`.

No new migrations: `StockMovement.get_absolute_url()`/`signed_quantity` and
`StockBalance.get_absolute_url()` are methods/properties, not fields.

## Testing
- `python manage.py test apps.inventory` — **19/19 pass**, run against the
  real Supabase database. Confirms the goods-receipt suite from the previous
  PR still passes unchanged after the refactor.
- New tests in `test_stock_ledger.py`: the engine directly (weighted-average
  blending across two receipts, outbound costing at the current average
  regardless of what the caller passes, value zeroed at zero quantity,
  negative stock blocked by the DB policy and allowed when a warehouse opts
  in), plus both screens (list rendering, search narrowing to one product,
  the valuation view reflecting the posted balance, login required).
- `ruff check` / `ruff format` — clean.

## Follow-ups (out of scope here)
- Deliveries, transfers and adjustments still need their own document flows
  (forms, views, approval where relevant) — this PR only makes sure they'll
  have a correct, shared engine to post through when they land.
- The `ValidationError` raised when BR-017 blocks a movement currently
  surfaces Postgres's raw trigger message. That's readable enough today, but
  worth a friendlier translation once a screen actually lets a
  non-technical user trigger it (the goods-receipt flow never can, since
  receiving only increases stock).
