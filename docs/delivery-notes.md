# Delivery Notes — Day 3 feature (SAL-005, INV-007)

Delivery-note **creation** against an approved sales order, with **partial
delivery** and the order-fulfilment counters, delivered by Member 3.

Scope: the `DeliveryNote` / `DeliveryNoteLine` screens — list, create (order
picker + header + lines), detail, and post. The actual **stock-posting engine**
(StockMovement rows, weighted-average COGS, the COGS journal entry) belongs to
Member 2's Day 5 milestone (INV-003 / INV-005) and is deliberately **out of
scope** here; this work ships a clearly marked seam for it. Everything else in
the delivery flow is complete and testable without it.

---

## What was built

| Layer | File | Purpose |
|---|---|---|
| Routes | `apps/sales/urls.py` | delivery list / create / detail / post |
| Services | `apps/sales/services.py` | DN numbering, remaining-to-deliver, draft, post, order fulfilment sync |
| Forms | `apps/sales/forms.py` | `DeliveryNoteForm`, `DeliveryLineForm`, `DeliveryLineFormSet` |
| Views | `apps/sales/views.py` | `DeliveryNoteListView`, `DeliveryNoteCreateView`, `DeliveryNoteDetailView`, `DeliveryNotePostView` |
| Templates | `templates/sales/delivery_note_form.html`, `delivery_note_detail.html` | create + detail screens |
| Tests | `apps/sales/tests/test_delivery.py` + `make_user` in `factories.py` | 22 delivery tests |

The `DeliveryNote` / `DeliveryNoteLine` models already live in `apps/inventory`
(Member 2's app, BRD §11.2) — Member 3 reads them but did not own them. The
screens and business flow are owned in `apps/sales`.

## Screens and routes

| URL | View | Requirement |
|---|---|---|
| `/sales/deliveries/` | `DeliveryNoteListView` | list + search/filter/export (UX-002, UX-005) |
| `/sales/deliveries/new/` | `DeliveryNoteCreateView` | two-step: pick approved order → edit quantities |
| `/sales/deliveries/<pk>/` | `DeliveryNoteDetailView` | detail + audit history (ACC-005) |
| `/sales/deliveries/<pk>/post/` | `DeliveryNotePostView` | post a DRAFT note (WAREHOUSE, `core.post_delivery`) |

Entry points: the "New delivery note" button on the list, and a "Create
delivery note" action on an **APPROVED** sales order detail (which also shows
the order's existing delivery notes). Permissions are enforced server-side on
the views (ACC-004): `inventory.add_deliverynote` to create,
`inventory.view_deliverynote` to view, `core.post_delivery` to post.

## Business rules implemented

### Numbering (`allocate_dn_number`, NFR-008)
Same concurrency-safe pattern as SO numbering — `SELECT ... FOR UPDATE` on the
`DocumentSequence` row for `document_type="DN"`, producing `DN-00001`, ... The
sequence was seeded with the migration (`DN-`, padding 5).

### Remaining to deliver (`remaining_to_deliver`, SAL-005)
```
remaining = line.quantity − line.quantity_delivered
```
Used everywhere: to know which order lines are candidates for a new note, to
pre-fill quantities, to validate on the form, and to re-validate at post time
(so a note drafted earlier can't over-deliver once another delivery has
consumed the remaining quantity).

### Create flow (`draft_delivery_from_order`)
* Only an **APPROVED** (or **PARTIAL**, for a follow-up delivery) order is a
  candidate — DRAFT/SUBMITTED/REJECTED/COMPLETED orders are rejected.
* Quantities are checked against the remaining amount — **over-delivery is
  blocked** (SAL-005) at draft time (and rechecked at post time).
* The note is created **DRAFT** with its number, customer, warehouse, dates,
  shipping snapshot, and the selected lines (quantity only; `unit_cost` /
  `total_cost` stay zero until Member 2's costing engine runs).
* Creating and posting are split so a warehouse user can recheck before the
  note is committed.

### Posting (`post_delivery`, SAL-005 / INV-007)
Inside one `transaction.atomic()`:
1. Validates the note is still **DRAFT** and every line still has remaining
   quantity on its order line.
2. Writes stock movements through the Member 2 seam (`_commit_stock_movements`)
   — a no-op until his Day 5 engine lands.
3. Increments `SalesOrderLine.quantity_delivered` with `F()` (race-safe).
4. Flips the note to **POSTED** (`posted_at` / `posted_by` recorded).
5. Syncs the order (`_sync_order_fulfilment`): **PARTIAL** while any line
   remains, **COMPLETED** once every line is fully delivered.
6. Records the POST audit event (ACC-005, BR-005).

```text
DRAFT ──post──▶ POSTED   (order: APPROVED/PARTIAL → PARTIAL/COMPLETED)
```

### Partial delivery (SAL-005)
The create screen pre-fills each line with its remaining quantity; the user can
ship less (or the full amount). Multiple partial notes accumulate into
`quantity_delivered` and the order only completes when every line hits zero
remaining. A second note on a **PARTIAL** order is allowed.

### Stock-posting seam (`_commit_stock_movements`)
Owned by Member 2's engine. Until it lands it returns `None` and the flow works
end to end anyway. The idempotency key shape it should use is
`"delivery:{note.number}:{warehouse_id}:{line_no}"` — the same
`idempotency_key` / `source_doc_type` / `source_doc_number` shape defined on
`StockMovement` (GL-002).

### Schema-constraint note
`DeliveryNote` carries a DB constraint `delivery_note_posted_has_journal`
(once `total_cost_base > 0`, a posted note needs its COGS journal entry). This
flow posts with `total_cost_base = 0` and lets Member 2's engine set costs and
attach the journal later, keeping the constraint satisfied and the ownership
split clean.

## Tests

```bash
python manage.py test apps.sales.tests.test_delivery --keepdb
```

Coverage:

* **remaining_to_deliver** — before, after a partial delivery, and at zero.
* **build_delivery_lines** — fully-delivered lines are excluded; partial
  remaining amounts are returned.
* **draft_delivery_from_order** — note + lines created DRAFT with number
  `DN-…`, warehouse/date overrides, and a REAL `User` on the audit events.
* **Guards** — rejects non-approved orders, over-delivery, and empty
  quantities; allows a follow-up delivery on a PARTIAL order.
* **post_delivery** — sets POSTED + `posted_at`/`posted_by`, increments the
  order-line counter, flips the order to PARTIAL then COMPLETED, and records an
  audit event.
* **Idempotency** — a note can only be posted once (double post raises).
* **Post-time re-validation** — a note drafted before another delivery consumed
  the remaining quantity is rejected at post time ("can deliver at most …").
* **Two partials then full** — the full 6+4 → 10 counter path through two
  notes.

> **Note on `--keepdb`.** Same shared-DB constraint as Day 2 — the suite reuses
> the existing `test_postgres` database, so run with `--keepdb`. Tests are
> written to be resilient to data left behind between runs (e.g.
> `NumberingTests` clears any stale `SO/DEFAULT` sequence row first).

## Ownership flags for the team

Per CONTRIBUTING.md, two touches to other people's code are flagged (both
minimal and additive, no migrations generated):

* `apps/inventory/models.py` — added `DeliveryNote.get_absolute_url()` (the
  shared list template links rows via that method; Member 2's model had none).
* `apps/sales/tests/test_services.py` — `NumberingTests`: deleted before
  asserting the no-sequence error, to be robust to `--keepdb` residue.

Member 2's remaining work for INV-007 / SAL-005: implement the
`_commit_stock_movements` seam, set `unit_cost` / `total_cost` from the
weighted-average engine, and attach the COGS journal entry.