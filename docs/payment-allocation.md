# Payment Allocation — Day 4 feature (PAY-003..PAY-007)

Applying money that has already been received or paid to the documents it
settles: **partial** allocation, one payment across **many invoices**,
**advances** that sit unapplied until someone decides where they go, and
**credit notes** applied without any cash moving. Delivered by Member 4.

The governing idea is that cash moves **once**. Posting a payment puts its
value in an advance account. Every later allocation reclassifies that advance
to the receivable or payable control account and never touches cash again. A
credit note has no cash leg at all, so applying one writes allocation rows and
updates balances without producing a journal.

Cross-currency settlement was out of scope here. **Superseded:** a target
booked at a different *rate* now settles and realises an exchange difference —
see [FX settlement, reversal, and vouchers](fx-and-reversal.md). A target in a
different *currency* is still refused.

---

## What was built

| Layer | File | Purpose |
|---|---|---|
| Engine | `apps/payments/allocation.py` | every settlement invariant: locking, validation, allocation rows, balances, journal |
| Journals | `apps/payments/posting.py` | payment posting journal, and the advance → control reclassification builder |
| Posting | `apps/payments/services.py` | `post_payment` — cash in or out, exactly once |
| Model | `apps/payments/models.py` | `Allocation.batch_key`, `source` / `target` accessors, batch-scoped uniqueness |
| Guards | `apps/payments/migrations/0004_allocation_consistency_guards.py` | extends the BR-008 trigger to credits |
| Forms | `apps/payments/forms.py` | `PaymentAllocationHeaderForm`, `AllocationLineForm` + formset |
| Views | `apps/payments/views.py` | `PaymentPostView`, `PaymentAllocationView` |
| Template | `templates/payments/payment_allocation.html` | the allocation workspace |
| Tests | `apps/payments/tests/test_allocation.py` | 45 tests, engine and workspace |

## Screens and routes

| Route | Name | What it does |
|---|---|---|
| `/payments/<pk>/post/` | `payments:payment_post` | posts the payment; cash moves here and only here |
| `/payments/<pk>/allocate/` | `payments:payment_allocate` | the allocation workspace |

The workspace lists every open document the payment could legally settle —
same party, same currency, same stored rate, posted or partial, not reversed —
ordered by due date so the oldest debt settles first. The cashier types amounts
against the rows they want. Blank and zero rows are deliberate no-ops, so a
long list does not have to be cleared before submitting.

Documents excluded only because of a currency or rate mismatch are **counted
and reported** rather than silently dropped, so a cashier who expects to see an
invoice is told why it is missing instead of concluding the system lost it.

## Business rules implemented

### The engine owns the rules (`allocation.py`)

Views collect intent and nothing else. Every rule is enforced in the engine,
against freshly locked rows, inside one transaction — so a rule cannot be
bypassed by driving the model directly, and a validation that passed against
the rendered page cannot go stale between render and submit.

### Locking and ordering

The source is locked first, then every target in **primary-key order**. Fixed
ordering is what stops two cashiers allocating the same two invoices in
opposite orders from deadlocking.

Locks use `select_for_update(of=("self",))`, which matters twice over. Postgres
refuses `FOR UPDATE` against the nullable side of an outer join, and a payment
joins to both `customer` and `vendor`, exactly one of which is null. Beyond
that, an unqualified lock would also cover the joined **currency** row, so two
allocations touching unrelated invoices in the same currency would serialise on
it — contention the pk ordering could never have relieved, because it would not
have been on the documents at all.

### Idempotency (`batch_key`)

Every request carries a `batch_key`. Submitting it again returns the original
result instead of allocating twice; submitting it again with **different
amounts** is refused rather than silently diverging. This is what makes the
workspace safe against a double-click, an impatient refresh, or a retried POST.

Uniqueness moved from `(source, target)` to `(batch_key, source, target)`. The
old shape would have forbidden a legitimate second allocation from the same
advance to the same invoice in a later batch; the new one keeps retries
idempotent without forbidding it.

### Money

`Decimal` throughout, quantized to four places at every boundary. A value with
more precision than that is **rejected, not rounded** — silently absorbing a
fifth decimal is how ledgers drift. Zero and negative amounts are refused, as
is an empty batch.

### What is refused

- more than the payment has left unallocated, checked on the **batch total**,
  not merely line by line
- more than a target has open
- a document belonging to a different party
- a different currency (a different *rate* is now allowed, and realises FX)
- an unposted payment, or a target that is draft, reversed, or has no journal
- an allocation date earlier than the payment or than the target's document date
- the same target twice in one batch

A batch that fails any of these writes **nothing** — no rows, no balance
change, no journal.

### The accounting (`posting.py`)

Posting a customer receipt debits cash and credits **customer advances**.
Allocating it later debits customer advances and credits **accounts
receivable**. Vendor payments mirror this through vendor advances and accounts
payable. One batch produces exactly **one** journal for the whole batch, not one
per line, because a batch is a single accounting event.

Applying a credit note produces **no journal**. The credit was already posted
when the note was; applying it only moves which document it offsets.

### BR-008, enforced by the database

`payments/0004` extends the deferred constraint trigger from
`ledger/0003_posting_guards` so it also covers `credited_txn` and the
credit-note sources. The trigger is `DEFERRABLE INITIALLY DEFERRED`, which is
what lets the engine write allocation rows and then update balances inside one
transaction and still be checked at commit.

The effect is that a document whose stored balance disagrees with the
allocation rows behind it **cannot be committed** — not by this engine, not by
a management command, not by a hand-written `UPDATE`.

## Tests

`apps/payments/tests/test_allocation.py`, 45 tests:

| Class | Covers |
|---|---|
| `PaymentAllocationTests` | partial, full, multi-document, draw-down across batches, vendor direction, derived `open_base` |
| `AllocationIdempotencyTests` | replayed key, changed amounts, wrong payment, legitimate re-allocation |
| `AllocationGuardTests` | every refusal listed above, and that a failed batch writes nothing |
| `AllocationJournalTests` | advance → control reclassification, no cash line, one journal per batch, row contents |
| `CreditAllocationTests` | customer and vendor credits, credit and payment on one invoice, over-application |
| `AllocationDatabaseGuardTests` | the BR-008 trigger, proved by tampering |
| `AllocationTargetTests` | which documents are offered, and their ordering |
| `AllocationWorkspaceTests` | permission, render, allocate, both error paths, double submit |

Two tests assert **database** guarantees rather than engine ones. A posted
invoice cannot exist without a journal, so the engine's check for that is a
second line of defence and the test says as much rather than pretending to
exercise it. The BR-008 trigger is proved by writing an inconsistent balance
and watching the commit fail.

Run them against a real PostgreSQL — the deferred triggers and check
constraints are the point, and SQLite would not exercise any of it:

```
python manage.py test apps.payments.tests.test_allocation
```

## Hand-off notes

- **Reversal is now built** — see [fx-and-reversal.md](fx-and-reversal.md).
  `reverse_allocation_batch` un-applies a batch and `reverse_payment` reverses
  the cash, with a dependency check between them.
- **Credit allocation has no screen.** `allocate_sales_credit` and
  `allocate_vendor_credit` are complete and tested, but only the payment
  workspace is wired to a route. A credit-application screen can call straight
  into them.
- **Realised FX has landed.** The rate check in `_validate_target` is gone;
  only the currency check remains. True cross-currency settlement is still open.
- **`available_payment_targets` is the one source of truth** for what may be
  settled. A future ageing or statement screen should call it rather than
  rebuilding the filter, or the two will drift.
