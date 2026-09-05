# Member 4 — Day 2 production posting engine report

## Delivery summary

Day 2 replaces the fail-fast stub with a production `PostingEngine` while retaining the
stable request, builder, draft, result, and atomic wrapper introduced on Day 1. The work
implements WHOL-26: account-mapping validation, idempotent posting, and balanced-journal
enforcement.

Branch: `m4/posting-engine-real`

Parent work item: `WHOL-26 — Posting engine: account mapping, idempotency, balanced journals`

## Implemented behavior

### Account-mapping validation

- `PostingRequest.required_mappings` is an immutable tuple of `MappingKey` values.
- The production engine rejects an empty mapping declaration, preventing validation from
  being bypassed by an incompletely upgraded caller.
- Unknown or duplicate mapping declarations fail at request construction.
- Missing mappings fail before the journal builder executes.
- Mappings to inactive or non-postable accounts are rejected.
- Every required mapped account must actually appear in the journal draft. This prevents
  a builder from declaring the correct configuration dependency but posting to a stale,
  hard-coded, or unrelated account.
- Every draft account is freshly loaded and checked for active/postable state, required
  customer/vendor dimensions, and single-currency compatibility.

### Balanced-journal enforcement

- Base-currency debits and credits are summed with `Decimal` and must be equal and non-zero.
- Validation happens before allocating a journal number or writing ledger rows.
- Header totals are calculated by the engine, never trusted from a caller.
- PostgreSQL's deferred `wams_journal_balance_check` trigger independently proves that
  header totals match persisted lines and that the journal remains balanced at commit.

### Idempotent and concurrent posting

- The existing unique `JournalEntry.idempotency_key` is the authoritative retry token.
- An exact retry for the same content type and source primary key returns the original
  journal with `created=False` and creates no duplicate lines or posting links.
- Reusing a key for a different source raises the stable `IDEMPOTENCY_CONFLICT` code.
- The source is locked before lookup, serializing duplicate submissions for one document.
- The unique constraint handles concurrent use of one key across different sources. The
  inner savepoint rolls back the losing insert before the service resolves the winner.

### Atomic persistence and traceability

- The service resolves exactly one open fiscal period for the posting date and locks it.
- The active journal-entry sequence is row-locked, reset by configured year/month policy,
  incremented atomically, and bounded to the model's 32-character number limit.
- `JournalEntry`, numbered `JournalLine` rows, and a journal `PostingLink` are written in
  one transaction with source content type/object ID, source document identity, actor,
  timestamps, currency, exchange rate, and idempotency key.
- Any validation, builder, database, or commit-trigger exception rolls back the complete
  posting unit, including the allocated sequence number.

## Stable error contract

The following machine-readable `PostingErrorCode` values were added:

- `missing_account_mapping`
- `invalid_account_mapping`
- `unbalanced_journal`
- `closed_fiscal_period`
- `idempotency_conflict`
- `journal_sequence_unavailable`

Application boundaries should branch on these codes and display a suitable safe message;
they should not parse exception text.

## Verification coverage

The PostgreSQL integration suite proves:

- balanced journal persistence and complete source/actor traceability;
- one header, two lines, and one posting link for a successful post;
- exact retries return the original entry without duplicates;
- a key cannot be reused for another source;
- unbalanced drafts write nothing;
- missing mappings stop before the builder is invoked; and
- required mappings cannot be declared and then ignored by a builder.

The suite uses `TransactionTestCase`, so deferred constraint triggers execute at a real
commit boundary. Its fixture uses deterministic high IDs and creates its own currency,
fiscal year, open period, sequence, accounts, and mappings; it does not depend on mutable
shared Supabase business data.

## Integration guidance for Members 2 and 3

1. Continue returning immutable `JournalDraft` objects from source-specific builders.
2. Add the exact `MappingKey` dependencies to `PostingRequest.required_mappings`.
3. Use a deterministic key such as `sales-invoice:{pk}:post:v1`; keep it identical across
   browser resubmission, job retry, and network retry for the same business action.
4. Inject `PostingEngine`, not `PostingEngineStub`, when enabling real persistence.
5. Use `PostingResult.created` only to distinguish a new write from a retry. Both results
   are successful and point to the authoritative `journal_entry`.
6. Never write `JournalEntry`, `JournalLine`, or `PostingLink` directly.

The source-specific status transition and any stock/payment side effects remain owned by
their application services. They must participate in the same posting transaction when
integrated so a failure cannot leave source state and financial effects inconsistent.

## Security and operational notes

- Logs contain correlation ID, model label, source ID, and actor ID; no amounts,
  narration, credentials, or personal data are logged.
- The implementation uses Django's server-side PostgreSQL connection. No Supabase secret
  or service-role credential is introduced into browser code.
- No production schema migration is required for Day 2; the necessary unique constraints,
  checks, and triggers already exist in committed migrations.
- Current Supabase breaking changes affecting automatic Data API exposure are irrelevant
  because this engine writes through Django/PostgreSQL rather than the public Data API.

## Commands

```bash
ruff format --check .
ruff check .
python manage.py check
python manage.py test apps.ledger.tests
```
