# FX settlement, reversal, and vouchers — Day 5 feature (PAY-010, PAY-011, PTY-003)

Three things the allocation work deliberately left for later, and the printable
voucher that goes with them. Delivered by Member 4.

- **Realised FX** — settling a document booked at a different rate no longer
  refuses; it settles, and the exchange difference is posted.
- **Reversal** — un-applying an allocation, and reversing a payment, without
  ever editing a posted journal.
- **Dependency checks** — a payment cannot be reversed while its money is still
  applied to something.
- **Vouchers** — a printable receipt or payment voucher.

---

## What was built

| Layer | File | Purpose |
|---|---|---|
| Ledger contract | `apps/ledger/services/posting.py` | `JournalDraft` gains `reverses` / `is_reversal` / `reversal_reason` |
| Journals | `apps/payments/posting.py` | FX-aware reclassification, the reversing-journal builder, `mappings_used_by` |
| Engine | `apps/payments/allocation.py` | per-target FX, rate mismatch now allowed |
| Reversal | `apps/payments/reversal.py` | `reverse_allocation_batch`, `reverse_payment`, `live_allocation_batches` |
| Forms | `apps/payments/forms.py` | `ReversalForm` — reason and date, both required |
| Views | `apps/payments/views.py` | `PaymentReverseView`, `AllocationReverseView`, `PaymentVoucherView` |
| Templates | `payment_reverse.html`, `payment_voucher.html` | confirm screen, printable voucher |
| Tests | `apps/payments/tests/test_fx_and_reversal.py` | 38 tests |

## Screens and routes

| Route | Name | What it does |
|---|---|---|
| `/payments/<pk>/reverse/` | `payments:payment_reverse` | reverse the payment itself |
| `/payments/<pk>/allocations/<batch_key>/reverse/` | `payments:allocation_reverse` | un-apply one allocation batch |
| `/payments/<pk>/voucher/` | `payments:payment_voucher` | printable voucher |

All three are reachable from the payment detail page, each gated on both the
permission and the document's state.

## Realised FX

### What changed

Allocation used to refuse a target whose stored exchange rate differed from the
payment's. That refusal is gone. Only a **different currency** is still
refused — settling a USD invoice with EUR cash needs a rate between two
non-base currencies, which nothing in the system holds.

`available_payment_targets` matches on currency alone, so a document booked at
an older rate now appears in the workspace like any other.

### The accounting

The settled amount is valued twice: once at the money's rate, once at the
document's own. Where they disagree, the difference is realised FX — the third
line that balances the entry.

A receipt of 100 USD at 1.25, against an invoice booked at 1.20:

```
Dr  Customer advances        125.00   (100 USD at 1.25)
    Cr  Accounts receivable  120.00   (100 USD at 1.20)
    Cr  Realised FX gain       5.00
```

Taking in more base currency than the receivable was carried at is a **gain**.
Paying out more base currency than the payable was carried at is a **loss**, so
the sign flips with the direction of the payment. `fx_gain_loss_base` on each
allocation row follows one convention throughout: **positive is a gain**.

The FX line carries **base currency only**. The difference exists solely
because two rates disagree, so it has no transaction-currency amount — which
the `journal_line_txn_side_matches_base` constraint permits, and the balance
trigger accepts because it checks base amounts alone.

Three details worth knowing:

- **One journal per batch, still.** Gains and losses inside a batch net off. A
  batch with +5 on one invoice and −5 on another needs no FX line at all,
  though each row still records its own figure.
- **`fx_journal_entry` points at the allocation journal.** The exchange
  difference cannot be posted apart from the reclassification it balances, so
  both fields name the same entry. It is left null when no rate moved.
- **The FX account is only required when it is used.** The posting engine
  rejects a required mapping the journal never touches, so `FX_GAIN` / `FX_LOSS`
  are named only when the rates actually moved.

## Reversal

### The ledger is append-only

Nothing here edits or deletes a posted journal (BR-004). Undoing a posting is
always a **second journal** that mirrors the first and points back at it through
`JournalEntry.reverses` — a `OneToOneField`, so the database itself guarantees a
journal can be reversed at most once.

`JournalDraft` gained `reverses`, `is_reversal` and `reversal_reason` so a
reversal is recorded as one at the moment it is created, rather than patched in
afterwards. The draft refuses `is_reversal` without both a saved original and a
reason.

`make_reversing_journal_builder` reads the original's lines **back from the
database** and swaps every side. That matters: the reversal cancels what was
actually posted, even if the account mappings have been reconfigured since.
`mappings_used_by` derives the required mappings from the original's own
accounts, for the same reason.

### Reversing an allocation

Un-applies every line of one batch as a single accounting event: the settled
documents get their open balance back, statuses are recomputed, the source's
advance is restored, any realised FX is reversed with it, and every row is
marked with the reason.

A payment allocation restores `allocated_txn`; a credit application restores
`credited_txn`. The freed advance can be allocated again immediately.

### Reversing a payment

Reverses the cash itself and marks the payment `REVERSED`, recording who, when,
why, and the reversing journal.

`allocated_txn` and `unallocated_txn` are deliberately **not** touched. Nothing
was applied — the dependency check guarantees it — so they already read zero and
the full amount, and `payment_unallocated_is_derived` requires that they agree.

### The dependency check

A payment cannot be reversed while its money is still applied. The check runs
against freshly locked rows, so it cannot be raced, and the error **names the
documents** rather than saying no:

> `ARC-0001 is still applied to INV-0007, INV-0009. Reverse the allocation
> first, then reverse the payment.`

The reversal screen lists those batches with a link to reverse each one, so the
required order is visible rather than something to guess at.

Also refused: reversing twice, reversing a draft, and reversing without a
reason. `payment_reversal_attributable` enforces the last of those in the
database too — a reversal is always attributable and reasoned.

## Vouchers

`payment_voucher.html` prints a **receipt voucher** or a **payment voucher**
depending on direction, because they are different documents to an auditor. It
shows the party, method and reference, the amount with its rate and base value,
what the money was applied to with any exchange difference per document, the
unapplied remainder, the journal number, and signature lines.

A reversed payment prints with a prominent notice carrying the reason, the date
and the reversing journal number — a voucher that no longer represents live
money should not look like one that does.

## Tests

`apps/payments/tests/test_fx_and_reversal.py`, 38 tests:

| Class | Covers |
|---|---|
| `RealisedFxTests` | gain, loss, both directions, no-movement, netting within a batch, partial, missing mapping, currency still refused |
| `AllocationReversalTests` | balances restored, mirrored journal, FX reversed, reason recorded, double reversal, re-allocation, credit applications |
| `PaymentReversalTests` | reversal and its journal, the dependency check, correct order, double reversal, drafts, reasons |
| `ReversalAndVoucherViewTests` | permissions, both reversal screens, the dependency message, and the voucher in three states |

FX is asserted **in the ledger**, not only on the allocation row — a stored
figure that no journal agrees with would be worse than no figure at all.

Run against a real PostgreSQL; the constraints and deferred triggers are the
point:

```
python manage.py test apps.payments
```

## Hand-off notes

- **True cross-currency settlement is still not built.** Same currency,
  different rate is done. Settling a USD invoice with EUR cash needs a
  cross-rate the system does not hold; `_validate_target` is where it would go.
- **Reversing a credit note itself** is not built — only the *application* of
  one can be reversed. Same shape as `reverse_payment`, against
  `SalesCreditNote` / `VendorDebitNote`.
- **Refunds are untouched.** `refunded_txn` is read when recomputing a credit
  note's open balance but nothing writes it yet.
- **Reversal posts into the date you give it**, and the posting engine refuses a
  closed period. Reversing into a prior period therefore fails by design rather
  than silently reopening it.
