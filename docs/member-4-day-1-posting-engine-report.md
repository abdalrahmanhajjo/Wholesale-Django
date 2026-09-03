# Member 4 — Day 1 Posting Engine Stub

## Implementation and handoff report

| Item | Value |
|---|---|
| Project | Ledgerwise Wholesale Accounting & Business Management System |
| Team role | Member 4 — Payments, Ledger, and Reports |
| Assignment | Day 1 — Posting engine stub |
| Deliverables | Posting service interface, atomic transaction wrapper, journal-builder signature |
| Source branch | `dev` |
| Working branch | `m4/posting-engine-stub` |
| Repository | `abdalrahmanhajjo/Wholesale-Django` |
| Completion date | 2026-09-01 |
| Author identity | Abdalrahman Hajjo `<abedhajjo57@gmail.com>` |
| Status | Implemented, verified, committed, and pushed |

## 1. Executive summary

I implemented the shared Day-1 posting-engine contract required by the purchasing,
sales, inventory, and payments modules. The work establishes a single, safe entry point
for future general-ledger postings without implementing the Day-2 accounting rules too
early.

The deliverable provides:

- A typed service interface for every operational posting.
- An outer `transaction.atomic()` boundary owned by the ledger service.
- PostgreSQL row locking of the source document with `select_for_update()`.
- An immutable journal-builder input/output contract.
- A deterministic idempotency-key input for the Day-2 implementation.
- Safe previewing that always rolls back database changes.
- A concrete fail-fast stub that can never report a false posting success.
- Machine-readable domain errors and correlation-aware structured logging.
- Database-free unit tests and PostgreSQL integration tests.
- Team integration documentation and an architecture decision record.

This work allows Members 2 and 3 to build purchase-bill and sales-invoice journal
builders immediately. They do not need to know how journal rows, numbering, mappings,
idempotency, or audit records will be persisted internally on Day 2.

## 2. Assignment interpretation

The team plan defines the Member 4 Day-1 task as:

> Posting engine stub — posting service interface, atomic transaction wrapper, and
> journal builder signature; share with the team as soon as possible.

I treated “stub” as a stable architectural contract rather than a silent placeholder.
A no-op service would be dangerous in an accounting system because a caller could mark
an invoice as posted even though no journal exists. The resulting stub therefore fails
explicitly when real persistence is requested, while still allowing builders to be
previewed and tested.

## 3. Requirements traced to the implementation

| Requirement | Meaning | Implementation |
|---|---|---|
| BR-001 | Financial values use fixed-precision decimals | Draft amounts and rates require `Decimal`; floats are rejected |
| BR-004 | Posted financial records are not silently edited | All future journal writes are centralized behind one service boundary |
| BR-005 | Posting succeeds or fails as one unit | `PostingService.post()` owns the outer `transaction.atomic()` block |
| BR-006 | Journals must balance | Draft structure is defined now; exact whole-journal balance enforcement remains Day 2 |
| BR-013 | Preserve transaction and base currency context | Drafts carry currency, exchange rate, and both base/transaction amounts |
| BR-020 | Closed-period posting must be blocked | The service boundary provides the location for Day-2 period validation |
| GL-002 | Repeated posting must be idempotent | Every `PostingRequest` requires a deterministic idempotency key |
| GL-009 | Corrections use linked reversals | Centralization prevents operational apps from inventing edit-based corrections |
| GL-011 | Control accounts reconcile to subledgers | Lines support customer, vendor, product, warehouse, tax, and money-account dimensions |
| NFR-003 | Concurrency-sensitive work uses transactions and row locks | Source rows are reloaded using `select_for_update()` inside the transaction |
| NFR-004 | Never use binary floating point for money | Runtime validation rejects floats, NaN, infinity, and negative amounts |
| NFR-008 | Concurrent posting must not duplicate effects | The locked source and future idempotency lookup share one atomic boundary |
| NFR-009 | Posting failures roll back completely | Exceptions propagate through the atomic wrapper |
| NFR-014 | Business rules belong in services | Posting logic lives under `apps/ledger/services/`, not views or templates |
| NFR-015 | Posting behavior requires tests | Unit and PostgreSQL contract suites were added |
| NFR-016 | Posting failures require correlation identifiers | Requests receive UUID correlation IDs used in structured logs |

## 4. Repository and Git work

The local repository was already cloned. I verified that `origin` points to:

```text
https://github.com/abdalrahmanhajjo/Wholesale-Django.git
```

I fetched `origin/dev` and created the feature branch from it. Git references cannot
contain spaces, and the repository contribution guide requires a member prefix, so the
requested “Posting engine stub” branch became:

```text
m4/posting-engine-stub
```

The repository-local commit identity was configured as:

```text
Abdalrahman Hajjo <abedhajjo57@gmail.com>
```

The implementation was delivered in three incremental commits:

| Commit | Description |
|---|---|
| `107842f` | Added the initial posting service contract |
| `cde9f88` | Hardened validation and added the concrete fail-fast stub |
| `8b4783e` | Completed previewing, error codes, logging, testing, and architecture documentation |

The branch was pushed to GitHub and tracks `origin/m4/posting-engine-stub`.

Pull-request URL:

```text
https://github.com/abdalrahmanhajjo/Wholesale-Django/pull/new/m4/posting-engine-stub
```

## 5. Files created or updated

| File | Purpose |
|---|---|
| `apps/ledger/services/__init__.py` | Stable public imports for operational apps |
| `apps/ledger/services/exceptions.py` | Posting exception hierarchy and error codes |
| `apps/ledger/services/posting.py` | DTOs, builder protocol, request/result contracts, atomic service, preview, and stub |
| `apps/ledger/tests/test_posting_unit.py` | Database-free public-contract tests |
| `apps/ledger/tests/test_posting_contract.py` | PostgreSQL transaction, rollback, locking, and integration tests |
| `docs/posting-engine-contract.md` | Short integration guide for Members 2 and 3 |
| `docs/adr/0001-centralized-posting-service.md` | Architecture decision and rejected alternatives |
| `docs/member-4-day-1-posting-engine-report.md` | This complete implementation report |

No Django models, migrations, database tables, shared configuration files, views, or
templates were changed. This respects the repository’s app-ownership rules.

## 6. Public posting API

Operational apps use one public import location:

```python
from apps.ledger.services import (
    JournalBuilder,
    JournalDraft,
    JournalLineDraft,
    PostingContractError,
    PostingEngineStub,
    PostingEngineUnavailable,
    PostingError,
    PostingErrorCode,
    PostingRequest,
    PostingResult,
    PostingService,
)
```

This prevents callers from depending on internal module layout and gives the team a
stable contract that the Day-2 engine can implement without changing sales or purchase
code.

## 7. Journal line draft

`JournalLineDraft` represents one proposed debit or credit before any `JournalLine`
database row exists. It is a frozen, slotted dataclass, so its values cannot be changed
after construction.

It carries:

- Posting account
- Description
- Base-currency debit and credit
- Transaction-currency debit and credit
- Customer dimension
- Vendor dimension
- Product dimension
- Warehouse dimension
- Tax-code dimension
- Money-account dimension

The constructor rejects:

- Unsaved or fake account instances
- Floats or other non-`Decimal` amount types
- NaN or infinite decimals
- Negative amounts
- A line with neither side populated
- A line with both debit and credit populated
- Transaction debit/credit values on the opposite base side
- A line linked to both a customer and vendor
- Descriptions exceeding the database limit

These are structural contract validations. Whole-journal balance, account mapping,
account activity, control-account requirements, and rounding tolerance remain part of
the Day-2 posting engine.

## 8. Journal draft

`JournalDraft` is the complete immutable journal proposed by an operational builder.
It contains:

- Entry date
- Journal type
- Narration
- Transaction currency
- Snapshotted exchange rate
- Source document type
- Source document number
- An immutable tuple of `JournalLineDraft` values

It validates that:

- The entry date is a date value.
- The journal type is supported by `JournalType`.
- The currency is a real saved Django model.
- The exchange rate is a finite, positive `Decimal`.
- Source identifiers fit the existing database columns.
- At least one journal line exists.
- Lines are supplied as an immutable tuple.
- Every tuple item is a `JournalLineDraft`.

## 9. Journal builder signature

Members 2 and 3 implement a builder with this signature:

```python
def build_journal(source, *, user) -> JournalDraft:
    ...
```

The source type is generic, so the same service can support:

- `PurchaseBill`
- `SalesInvoice`
- `Payment`
- `Refund`
- `SalesCreditNote`
- `PurchaseDebitNote`
- Inventory delivery or receipt events
- Future supported accounting sources

Builder responsibilities:

- Read the locked or refreshed source document.
- Calculate proposed debit and credit lines using `Decimal`.
- Preserve transaction-currency and base-currency values.
- Attach required customer, vendor, tax, product, warehouse, or money dimensions.
- Return one immutable `JournalDraft`.

Builders must not:

- Create `JournalEntry`, `JournalLine`, or `PostingLink` rows.
- Change the source document’s status.
- Allocate a journal number.
- Create audit events.
- Open a separate transaction.
- Catch and suppress posting failures.

## 10. Posting request

`PostingRequest` groups the inputs required for every posting:

```python
request = PostingRequest(
    source=bill,
    user=request.user,
    idempotency_key=f"purchase-bill:{bill.pk}:post:v1",
    build_journal=build_purchase_bill_journal,
    reason="Approved purchase bill",
)
```

The request validates:

- The source is a saved Django model, not merely an object with a manually assigned PK.
- The actor is a saved, authenticated user.
- The idempotency key is present and no longer than 120 characters.
- The idempotency key has no surrounding whitespace.
- The journal builder is callable.
- The reason is text.
- The correlation identifier is a UUID.

Each request receives a UUID correlation ID by default. Future views may pass an
existing request-level correlation ID so posting logs and audit records can be joined.

## 11. Idempotency contract

The Day-1 interface requires an idempotency key even though the database lookup is a
Day-2 responsibility. Requiring it now prevents later API churn.

The key must describe the business action deterministically. For example:

```text
purchase-bill:42:post:v1
sales-invoice:125:post:v1
customer-receipt:88:post:v1
```

Retrying the same action must reuse the same key. A caller must not generate a random
key per HTTP request, because that would defeat duplicate-posting protection.

`PostingResult.created` will allow the Day-2 engine to distinguish a new journal from
an existing journal returned for an idempotent retry.

## 12. Atomic posting flow

`PostingService.post()` is the protected template method for real posting:

```text
Caller constructs PostingRequest
        |
        v
Open transaction.atomic()
        |
        v
Reload source with SELECT ... FOR UPDATE
        |
        v
Attach safe structured logging context
        |
        v
Call concrete _post_locked() implementation
        |
        +---- exception ----> log failure -> re-raise -> database rollback
        |
        v
Validate PostingResult
        |
        v
Log success -> commit transaction -> return result
```

The service deliberately reloads the source instead of trusting the object supplied by
the view. `select_for_update()` locks the row until the transaction finishes. Two
concurrent attempts to post the same document therefore cannot both operate on an
unlocked stale copy.

Exceptions are not swallowed. Django and PostgreSQL roll back all work performed in
the transaction, which will eventually include:

- Journal header
- Journal lines
- Posting links
- Document status and journal link
- Stock movements
- Payment allocations
- Audit event
- Number-sequence update

The concrete Day-2 implementation may override only `_post_locked()`. The base class
rejects subclasses that attempt to replace `post()` or `preview()`, preventing a future
implementation from accidentally bypassing locking or transactions.

## 13. Safe preview flow

`PostingService.preview()` allows Members 2 and 3 to execute their journal builders on
Day 1:

```python
draft = PostingEngineStub().preview(posting_request)
```

Preview behavior:

1. Opens an atomic block.
2. Reloads a fresh copy of the source document without taking a posting lock.
3. Calls the supplied journal builder.
4. Verifies that the builder returned `JournalDraft`.
5. Marks the transaction rollback-only.
6. Returns the validated draft after the rollback completes.

The rollback-only design protects the database even if a developer accidentally puts a
`.save()` inside a builder. Preview is advisory and cannot persist a financial effect.

## 14. Concrete Day-1 stub

`PostingEngineStub` implements the public interface but intentionally does not persist
accounting data.

Calling `post()`:

- Opens the atomic wrapper.
- Locks the source row.
- Enters the standard logging path.
- Raises `PostingEngineUnavailable`.
- Rolls back without writing anything.

This is safer than returning `None`, a fake journal, or a success flag. A caller cannot
mistake unfinished Day-1 behavior for a completed financial posting.

## 15. Error handling

All posting errors inherit from `PostingError`. Contract errors also inherit from
`ValueError`, allowing normal invalid-input handling while preserving a domain-specific
exception boundary.

| Error/code | Use |
|---|---|
| `PostingContractError` | Malformed request, draft, builder output, or engine output |
| `PostingEngineUnavailable` | Day-1 stub cannot persist a real journal |
| `invalid_request` | General contract failure |
| `invalid_draft` | Reserved for detailed draft validation |
| `invalid_amount` | Wrong, negative, or non-finite financial value |
| `unsaved_object` | Source, account, currency, or actor is not persisted |
| `unauthenticated_actor` | Posting actor is not a saved authenticated user |
| `invalid_builder_result` | Builder did not return `JournalDraft` |
| `invalid_service_result` | Concrete engine did not return `PostingResult` |
| `engine_unavailable` | Persistence is intentionally unavailable in the stub |

Views should branch on `exception.code`, not parse human-readable messages. Messages may
change; codes form the stable application contract.

## 16. Structured logging and security

Posting logs contain only:

- Correlation ID
- Source model label
- Source primary key
- Actor primary key

They deliberately exclude:

- Database credentials
- Account balances or journal amounts
- Narration and internal notes
- Party details
- Payment references
- Passwords, tokens, and keys

The service emits start, completion, preview, and failure events. Failures retain the
stack trace and correlation context while the original exception continues upward.

## 17. Testing strategy

### 17.1 Database-free unit tests

`apps/ledger/tests/test_posting_unit.py` uses `SimpleTestCase` and does not initialize a
database. It verifies:

- Immutable drafts
- Decimal-only values
- Stable error codes
- Rejection of fake saved accounts
- Idempotent-result representation
- Protection of template methods

Run it with:

```bash
python manage.py test apps.ledger.tests.test_posting_unit
```

Result during implementation:

```text
Found 5 tests
Ran 5 tests
OK
Skipping setup of unused database(s): default
```

### 17.2 PostgreSQL integration tests

`apps/ledger/tests/test_posting_contract.py` verifies behavior requiring a real
transactional database:

- Saved-source validation
- Journal-builder signature
- Immutable DTO behavior
- Preview rollback of accidental writes
- Invalid builder result handling
- Float and two-sided amount rejection
- Authenticated actor validation
- Fail-fast stub behavior
- Fresh locked source delivery
- Protected template methods
- Invalid service result handling
- Machine-readable error codes
- Complete rollback after an implementation failure
- New-versus-retried result representation

Run this suite only against an isolated PostgreSQL test database:

```bash
python manage.py test apps.ledger.tests.test_posting_contract
```

It was intentionally not run against the shared Supabase database because Django’s test
runner creates, writes, flushes, and destroys test data. The shared environment was
treated as read-only.

## 18. Quality checks completed

| Check | Result |
|---|---|
| Ruff formatting | Passed |
| Ruff linting | Passed |
| Python compilation | Passed |
| Django system checks | Passed |
| Database-free unit tests | 5/5 passed |
| MyPy service type check | Passed with no issues in three service files |
| Git whitespace check | Passed |
| Posting contract smoke test | Passed |
| Supabase connection | Passed read-only |
| Supabase ledger migration check | Three ledger migrations applied |
| Supabase public schema check | 77 public tables detected |
| Remote branch verification | Branch synchronized with GitHub |

MyPy and Django stubs were installed only in the local virtual environment for the
verification pass. Shared dependency files were not changed because tooling and root
configuration are owned by Member 1.

## 19. Supabase verification

The database URL already existed in the ignored local `.env` file. It was not written
to source code, documentation, commits, logs, or command output.

Only read-only database checks were performed. They confirmed:

- The PostgreSQL service is reachable.
- Django migrations are present.
- All three ledger migrations are applied.
- The public schema contains 77 tables.
- Existing posting-guard migrations remain intact.

No Supabase schema, table, row, policy, function, migration history, or configuration
was changed by this Day-1 task.

## 20. Prototype and design review

The Ledgerwise public prototype was reviewed for product context. Its accounting
direction supports the implementation decisions made here:

- One source of truth
- Accrual accounting
- Immutable ledger
- Balanced postings
- Source traceability
- Connected inventory, cash, receivables, payables, and reporting

The Figma integration was authenticated, but its Starter-plan MCP quota had been
exhausted, so the supplied node could not be retrieved programmatically. This did not
block the assignment because Day 1 has no frontend deliverable.

## 21. Team handoff for Members 2 and 3

Members 2 and 3 can now:

1. Import DTOs from `apps.ledger.services`.
2. Implement a pure builder for `PurchaseBill` or `SalesInvoice`.
3. Construct a deterministic `PostingRequest`.
4. Call `PostingEngineStub().preview(request)` to inspect and test the draft.
5. Keep the actual view action disabled or handle `PostingEngineUnavailable` until the
   Day-2 engine is merged.

They must not:

- Write journal models directly.
- Mark the source posted after a preview.
- Mark the source posted when the stub raises.
- Generate random idempotency keys.
- Add money arithmetic to views or templates.
- Modify shared ledger constraints to work around a posting error.

## 22. Example integration

```python
from apps.ledger.models import JournalType
from apps.ledger.services import (
    JournalDraft,
    JournalLineDraft,
    PostingEngineStub,
    PostingRequest,
)


def build_purchase_bill_journal(bill, *, user):
    lines = [
        JournalLineDraft(
            account=inventory_account,
            debit_base=bill.taxable_base_base,
        )
    ]

    if bill.tax_base:
        lines.append(
            JournalLineDraft(
                account=input_tax_account,
                debit_base=bill.tax_base,
            )
        )

    lines.append(
        JournalLineDraft(
            account=payable_account,
            credit_base=bill.total_base,
            vendor=bill.vendor,
        )
    )

    return JournalDraft(
        entry_date=bill.posting_date,
        journal_type=JournalType.PURCHASE,
        narration=f"Purchase bill {bill.number}",
        currency=bill.currency,
        exchange_rate=bill.exchange_rate,
        source_doc_type="PB",
        source_doc_number=bill.number,
        lines=tuple(lines),
    )


posting_request = PostingRequest(
    source=bill,
    user=request.user,
    idempotency_key=f"purchase-bill:{bill.pk}:post:v1",
    build_journal=build_purchase_bill_journal,
    reason="Approved for posting",
)

draft = PostingEngineStub().preview(posting_request)
```

The example is intentionally a builder/preview example. The Day-1 stub does not create
a journal.

## 23. Scope intentionally deferred to Day 2

The following items are not missing from Day 1; they are explicitly the next scheduled
deliverable:

- Account-mapping lookup and validation
- Active/postable account validation
- Whole-journal debit/credit balance enforcement
- Fiscal-period resolution and closed-period rejection
- Journal-number allocation under row lock
- `JournalEntry` persistence
- Bulk `JournalLine` persistence
- `PostingLink` creation
- Idempotent lookup and retry behavior
- Source status and `journal_entry` updates
- Posting audit event
- Database integrity-error translation
- Reversal posting service
- Concurrency tests with competing database connections
- Purchase-bill and sales-invoice end-to-end posting fixtures

Keeping these out of the Day-1 branch protects the shared interface from being mixed
with unfinished accounting policy and preserves the eight-day team plan.

## 24. Final outcome

The Day-1 posting engine is now a safe and usable team contract rather than an empty
placeholder. It gives downstream developers a concrete builder API, validates the most
dangerous input mistakes, guarantees transaction ownership, defines concurrency
behavior, prevents false success, supports safe previews, and establishes a clean path
to the real Day-2 engine.

The completed work is available on:

```text
Branch: m4/posting-engine-stub
Latest implementation commit: 8b4783e
```
