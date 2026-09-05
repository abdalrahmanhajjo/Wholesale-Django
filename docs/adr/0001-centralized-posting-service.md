# ADR 0001: Centralize all ledger writes behind the posting service

- Status: Accepted
- Date: 2026-09-01
- Owners: Member 4 (`ledger`, `payments`, `reports`)
- Requirements: BR-004, BR-005, BR-006, GL-002, GL-009, NFR-008, NFR-014

## Context

Sales invoices, purchase bills, payments, returns, and inventory operations all create
accounting effects. Allowing each Django app to write `JournalEntry`, `JournalLine`, or
`PostingLink` independently would duplicate balance, numbering, period, idempotency,
locking, audit, and reversal rules. The result would be inconsistent behavior and a
high risk of partial or duplicate postings.

## Decision

All automatic ledger writes go through `apps.ledger.services.PostingService`.
Operational apps provide an immutable `JournalDraft` through the shared
`JournalBuilder` signature. The posting service owns the outer transaction, source-row
lock, validation, number allocation, persistence, source linkage, and audit boundary.

`PostingService.post()` and `PostingService.preview()` are template methods and may not
be overridden. Implementations override `_post_locked()` only. Preview builders run in
a rollback-only transaction so previewing can never persist a financial effect.

The Day-1 `PostingEngineStub` fails explicitly. It never reports success without a
persisted journal. The Day-2 engine will implement persistence behind the same public
contract.

## Consequences

- Members 2 and 3 can integrate against one stable interface.
- Every real post has one atomic and concurrency-safe boundary.
- Views can handle stable `PostingErrorCode` values without parsing messages.
- Correlation IDs connect application logs to audit events without logging amounts,
  credentials, narrations, or other sensitive business content.
- Operational apps must not create or modify journal models directly.
- The posting service becomes critical infrastructure and requires focused unit,
  integration, concurrency, reversal, and idempotency tests.

## Rejected alternatives

- **Signals:** execution order and failure behavior are too implicit for accounting.
- **Journal writes inside views/models:** duplicates policy and weakens transaction
  ownership.
- **One posting implementation per operational app:** makes reconciliation and rule
  evolution unsafe.
- **A no-op stub:** callers could incorrectly mark documents posted without a journal.
