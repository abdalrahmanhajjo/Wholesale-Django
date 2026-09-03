# Delivery note posting: the weighted-average engine's missing outbound leg (Member 2)

Every physical stock document had a posting path through the shared
`post_stock_movement()` engine (goods receipts, transfers, adjustments,
purchase returns) except one: `DeliveryNote`, the outbound counterpart to a
goods receipt. It was flagged as a follow-up in three prior PRs and kept
getting deferred as "sales side owns the screen" — true for the create
screen, but the posting *service* belongs in `apps/inventory/services.py`
regardless of which app's workflow triggers it, same as goods receipts. This
PR closes that gap: the engine, the COGS journal, and the fulfilment-counter
update — no screens, since none exist yet for `DeliveryNote` anywhere in the
codebase to hang a "post" button off.

## What's new

### `apps/inventory/services.py`
- `allocate_dn_number()` — allocates the existing `DN-` sequence.
- `recalculate_delivery()` — a draft-time cost preview from the warehouse's
  current average, same pattern as `recalculate_transfer()`.
- `post_delivery()` — the real posting service. Mirrors `post_goods_receipt()`
  with the direction and accounts swapped: each line becomes a `DELIVERY`
  movement through the shared costing engine (outbound, so it's costed at
  the warehouse's *current* weighted average — which is exactly what COGS
  means), the line's `unit_cost`/`total_cost` are written back from what the
  engine actually used, the linked `SalesOrderLine.quantity_delivered` is
  bumped when there is one, and — unless every line happened to cost zero —
  **Dr Cost of Goods Sold / Cr Inventory** is booked for the total.

## Files touched
Modified: `apps/inventory/services.py`.
New: `apps/inventory/tests/test_delivery_posting.py`.

No new migrations: no model fields changed.

## Testing
- `python manage.py test apps.inventory.tests.test_delivery_posting` —
  **5/5 pass**: stock moves and gets costed at the blended weighted average
  (seeded via two receipts at different prices, so a wrong hardcoded cost
  would fail), the COGS journal balances, the linked sales-order line's
  fulfilment counter updates, insufficient stock is refused, and a second
  post attempt on an already-posted delivery is refused.
- `python manage.py test apps.purchases apps.inventory` — **65/65 pass**,
  confirming nothing in the existing suites regressed.
- `ruff check` / `ruff format` — clean.
- `manage.py makemigrations inventory --check` — no changes detected.

## Follow-ups (out of scope here)
- The actual `DeliveryNote` create/edit/detail/post screens still need to be
  built — this PR only makes sure whoever builds them (or the sales-order
  "create delivery" flow feeding it) has a correct engine to post through,
  same as the previous PRs did for transfers/adjustments/returns before
  their screens landed.
