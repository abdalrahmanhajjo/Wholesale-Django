# Warehouse ops: stock transfers and adjustments (Member 2, Day 6)

Day 5 built the weighted-average costing engine (`post_stock_movement()`) and
left it ready for the document types that hadn't landed yet. This PR is
those document types: moving stock between warehouses, and correcting stock
on hand with an audited reason — the two remaining ways stock changes
outside the purchase cycle (INV-008, INV-009), both posting through the same
engine and the same negative-stock policy as goods receipts.

## What's new

### Stock transfers (`/inventory/transfers/`)
`StockTransfer` / `StockTransferLine` already existed from the foundation
schema; this PR adds the application layer:

- **List / create-edit / detail** screens, same shape as goods receipts.
- The form rejects transferring a warehouse into itself before it ever
  reaches the DB's `stock_transfer_different_warehouses` constraint.
- **Posting** (`apps/inventory/services.py::post_stock_transfer`): each line
  becomes a `TRANSFER_OUT` at the source warehouse followed by a
  `TRANSFER_IN` at the destination, both through `post_stock_movement()` —
  the `IN` leg is costed at whatever the `OUT` leg actually left at, so a
  transfer carries the source's weighted average forward instead of
  inventing a new one. Insufficient stock at the source is refused by the
  same `wams_stock_negative_check` trigger a goods receipt would hit.
- **The general ledger only moves when it has to**: if the two warehouses
  resolve to the same Inventory account (the common case — CFG-007's
  per-warehouse override is optional and usually unset), a transfer is a
  pure stock relocation with no journal. When the warehouses do have
  distinct accounts, both legs clear through the seeded **Stock Transfer
  Clearing** account (`1330`) rather than debiting one account and
  crediting the other directly, so that account's activity is a reviewable
  record of transfers rather than an opaque net-zero pair.

### Stock adjustments (`/inventory/adjustments/`)
Same shape for `StockAdjustment` / `StockAdjustmentLine` / `AdjustmentReason`:

- **List / create-edit / detail** screens, plus the **approval workflow**
  (INV-009): draft → submit → approve/reject → post, mirroring the purchase
  order pattern exactly. A reason with `requires_approval=False` skips
  straight to approved on submit — none of the five seeded reasons currently
  do, but the admin can add one.
- **Direction enforced against the reason**: `AdjustmentReason.increases_stock`
  says whether a reason only ever raises or only ever lowers stock (e.g.
  "Damaged goods" only lowers). Nothing in the schema stopped a line from
  disagreeing with that before this PR — `services.validate_adjustment_directions()`
  checks it after the form and formset both validate and redisplays with one
  clear message instead of a confusing posting-time result.
- **Posting** (`apps/inventory/services.py::post_stock_adjustment`): each
  line posts as `ADJUSTMENT_IN` or `ADJUSTMENT_OUT` (sign of `quantity_delta`
  decides which) through the shared engine, then the net value books against
  the reason's own `gain_loss_account` — **Dr Inventory / Cr the account**
  for a net increase, the reverse for a decrease. All five seeded reasons
  already carry the right account (`COUNT-UP` → Inventory Adjustment Gain,
  `DAMAGE`/`EXPIRY`/`COUNT-DN` → Inventory Adjustment Loss, `OPENING` →
  Opening Balance Equity), so this just wires up data that already existed.

### Negative-stock policy
Both flows post through Day 5's `post_stock_movement()` unchanged, so
BR-017 applies identically: a transfer or a decrease adjustment that would
take a warehouse negative is refused by the database trigger and surfaced
as a `ValidationError`, exactly like a delivery would be. No new logic was
needed here — the point of building the shared engine last time was so this
would already be true.

## Files touched
Modified: `apps/inventory/{services,forms,views,urls,models,admin}.py`,
`templates/base.html` (nav links for the two new sections).
New: `templates/inventory/{stock_transfer_form,stock_transfer_detail,_st_line_row}.html`,
`templates/inventory/{stock_adjustment_form,stock_adjustment_detail,_sa_line_row}.html`,
`apps/inventory/tests/test_stock_transfers_adjustments.py`.

No new migrations: `StockTransfer.get_absolute_url()` and
`StockAdjustment.get_absolute_url()` are methods, not fields; every model
field and constraint used here already existed.

## Testing
- `python manage.py test apps.inventory` — **33/33 pass** (19 from before,
  14 new), run against the real Supabase database.
- New tests in `test_stock_transfers_adjustments.py`:
  - Transfers: numbering and the draft-time cost estimate, the
    same-warehouse form rejection, posting moves stock and carries cost
    forward, no journal when warehouses share an Inventory account, a
    journal through Stock Transfer Clearing when they don't, insufficient
    stock refused, and that Purchasing (as opposed to Warehouse) cannot post.
  - Adjustments: numbering, a reason/sign mismatch rejected before saving,
    the full submit → approve → post workflow booking the correct gain
    account, a no-approval-required reason skipping straight to approved,
    rejection requiring a reason, a decrease beyond on-hand refused at
    posting, and that Purchasing cannot approve.
- `ruff check` / `ruff format` — clean.
- `manage.py makemigrations inventory --check` — no changes detected.

## Follow-ups (out of scope here)
- Deliveries (`DeliveryNote`) are the one remaining document type still
  unposted — same engine, sales side owns the screen.
- `OVERRIDE_NEGATIVE_STOCK` still isn't wired to an actual override path,
  for the same reason `OVERRIDE_DUPLICATE_VENDOR_INVOICE` wasn't in the Day
  3–4 PR: the underlying DB trigger is a hard block regardless of Django
  permission, so a permission-gated UI for it would be misleading until
  that's revisited (e.g. a per-transaction flag the trigger itself reads).
