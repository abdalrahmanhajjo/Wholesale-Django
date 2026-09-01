# Member 4 — Day 3: Payments and Receipts (WHOL-27)

## Delivered scope

This work completes the Day 3 payment-entry slice: a cashier can record either
a customer receipt (money in) or a vendor payment (money out), save it as a
controlled draft, review it in the payment register, and edit it while it is
still a draft.

The slice implements BRD requirements PAY-001 and PAY-002 at entry stage. It
does not post the draft to the general ledger or allocate it to invoices/bills;
those are separate controlled workflows under PAY-003 onward.

## Business behavior

- **Customer receipt:** direction `RECEIPT`, exactly one customer, no vendor,
  and an `RC` document number.
- **Vendor payment:** direction `PAYMENT`, exactly one vendor, no customer, and
  a `PV` document number.
- Payment method is configurable. When a method has `requires_reference=True`,
  the entry cannot be saved without a cheque number, bank reference, or card
  authorization reference.
- An active money account is required. A method's configured default money
  account is used when the cashier leaves the account blank.
- The transaction amount must be positive. The service calculates
  `amount_base = amount_txn × exchange_rate` to four decimal places.
- A new payment has zero allocation and the entire amount is stored as
  `unallocated_txn`, ready for the later allocation workflow.
- Posting date must belong to an open fiscal period. A missing or closed period
  blocks the complete transaction and does not consume a document number.
- New and edited records carry user attribution and an immutable audit event.

## Example

Cashier records USD 1,250 received from customer `C-100 / Cedar Market` on
1 September 2026 by bank transfer, reference `TRX-88420`, exchange rate 1.00.

The service locks the receipt sequence and issues `RC-00001`, resolves the open
September fiscal period, then saves:

| Field | Saved value |
|---|---:|
| Direction | Customer receipt |
| Transaction amount | USD 1,250.0000 |
| Base amount | 1,250.0000 |
| Allocated | 0.0000 |
| Unallocated | 1,250.0000 |
| Status | Draft |

No journal entry or invoice settlement is created at this stage. That
separation prevents an incomplete cashier entry from changing financial
statements.

## Technical implementation

- `apps/payments/models.py`: reusable party display, detail URL, and model-level
  validation for direction, active configuration, and required reference.
- `apps/payments/forms.py`: cashier-facing form, active-master filtering,
  contextual validation, sensible date defaults, and default account behavior.
- `apps/payments/services.py`: atomic create/update use cases, row-locked fiscal
  period and number sequence, reset-policy support, derived amounts, and full
  model validation before persistence.
- `apps/payments/views.py` and `urls.py`: permission-protected register,
  create/edit/detail screens, search, filtering, summaries, CSV export, audit,
  and draft-only edit enforcement.
- `templates/payments/`: responsive payment entry and review screens, including
  direction-aware customer/vendor fields.
- `apps/payments/admin.py`: administration for money accounts, methods,
  payments, allocations, and refunds.
- `config/urls.py` and `templates/base.html`: application routing and navigation.
- `apps/payments/tests/test_payments.py`: five PostgreSQL-backed form/service
  tests for the highest-risk business controls.

## Controls and design decisions

1. **Atomicity:** sequence allocation, fiscal-period resolution, validation,
   payment persistence, and audit recording share one outer transaction.
2. **Concurrency:** `SELECT ... FOR UPDATE` protects both the document sequence
   and fiscal-period decision from races.
3. **No float arithmetic:** all money and FX calculations use `Decimal` and the
   project's four-decimal money policy.
4. **Server authorization:** direct URLs enforce Django permissions; hiding an
   action in the template is only presentation.
5. **Draft boundary:** posted records cannot be edited through this screen.
6. **Database boundary:** existing database CHECK constraints remain the final
   guard for positive values, party direction, allocation arithmetic, posting
   traceability, and reversal attribution.

## Verification evidence

- `python manage.py check`: passed, zero issues.
- `ruff check apps/payments config/urls.py`: passed.
- `python manage.py makemigrations payments --check --dry-run`: no schema drift.
- `python manage.py test apps.payments.tests --keepdb --noinput`: 5 tests passed.

## Acceptance checklist

- [x] Customer receipt and vendor payment are distinct, validated directions.
- [x] Party, date, posting date, currency, rate, amount, method, account,
      reference, and narration are captured.
- [x] Required-reference configuration is enforced.
- [x] Unique receipt/payment numbering is transaction-safe.
- [x] Closed or missing fiscal periods are rejected atomically.
- [x] Base and unallocated values are derived consistently.
- [x] Register, search, filters, summary, CSV, detail, and draft edit are usable.
- [x] Permissions and audit trail are enforced server-side.
- [x] Automated tests and project checks pass.
