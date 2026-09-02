# Posting engine contract — Day 2 production handoff

Members 2 and 3 should import the contract from `apps.ledger.services`. Operational
apps build journal drafts; only Member 4's posting service writes ledger rows.

Architecture rationale: [ADR 0001](adr/0001-centralized-posting-service.md).

```python
from decimal import Decimal

from apps.ledger.models import JournalType, MappingKey
from apps.ledger.services import JournalDraft, JournalLineDraft, PostingEngine, PostingRequest


def build_purchase_bill_journal(bill, *, user):
    lines = [
        JournalLineDraft(
            account=inventory_account,
            debit_base=bill.taxable_base_base,
        )
    ]
    if bill.tax_base:
        lines.append(
            JournalLineDraft(account=input_tax_account, debit_base=bill.tax_base)
        )
    lines.append(
        JournalLineDraft(
            account=ap_account,
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


request = PostingRequest(
    source=bill,
    user=request.user,
    idempotency_key=f"purchase-bill:{bill.pk}:post:v1",
    build_journal=build_purchase_bill_journal,
    required_mappings=(
        MappingKey.INVENTORY,
        MappingKey.INPUT_TAX,
        MappingKey.ACCOUNTS_PAYABLE,
    ),
    reason=form.cleaned_data.get("reason", ""),
)
posting_service = PostingEngine()
draft = posting_service.preview(request)  # advisory and rollback-only
result = posting_service.post(request)  # atomic production persistence
journal = result.journal_entry
```

## Contract rules

- Save the source before posting; the service reloads it with `select_for_update()`.
- Supply a deterministic idempotency key, maximum 120 characters. Do not use a random
  value for retries of the same business action.
- Declare every configuration mapping the builder relies on in `required_mappings`.
  The engine fails before the builder runs if any mapping is absent or invalid, and it
  proves that every required mapped account appears in the resulting journal.
  Production posting requires at least one mapping; an empty tuple is accepted only by
  the Day-1 contract/stub so existing integrations can be upgraded deliberately.
- Use `Decimal` for every amount and rate. Never pass `float`.
- Pass journal lines as a tuple. Drafts are immutable by design.
- Builders return data only. They must not save rows, change document status, allocate
  a journal number, or create a nested transaction.
- Include customer/vendor and other dimensions on the relevant control-account lines.
- Let posting exceptions propagate. The service's outer `transaction.atomic()` rolls
  back the journal, status, stock/allocation effects, and audit event together.
- Do not create `JournalEntry`, `JournalLine`, or `PostingLink` directly outside the
  ledger service.
- Preserve the request's `correlation_id` when handing it to audit or diagnostic code.

## Production runtime behavior

`PostingEngine` performs the complete journal write behind the Day-1 interface:

1. Lock the source row and look up an existing idempotent result.
2. Validate required account mappings and build the immutable journal draft.
3. Enforce a non-zero, balanced base-currency journal and validate every account.
4. Lock the open fiscal period and journal-number sequence.
5. Persist `JournalEntry`, all `JournalLine` rows, and the `PostingLink` atomically.
6. Return `PostingResult(created=False)` for an exact retry of the same source and key.

Reusing an idempotency key for another source raises `IDEMPOTENCY_CONFLICT`. PostgreSQL
unique constraints close the cross-source concurrency race, while the source-row and
sequence-row locks serialize repeated posting and number allocation. Database triggers
independently re-check balance, period, account, and immutability rules at commit.

The engine deliberately does not change a source document's status. The owning sales,
purchasing, inventory, or payment application service should update its source within
the same outer posting call when that workflow is introduced; callers must never infer
status from `created=True` alone.

## Legacy stub

`PostingEngineStub` is a concrete, fail-fast implementation of the interface. It locks
the source inside the atomic wrapper and raises `PostingEngineUnavailable` without
writing anything. This makes unfinished integration visible and prevents a caller from
mistaking a no-op for a successful financial posting.

```python
from apps.ledger.services import PostingEngineStub

posting_service = PostingEngineStub()
posting_service.post(request)  # raises PostingEngineUnavailable on Day 1
```

Callers may catch `PostingError` at an application boundary to show a safe message, but
must not swallow it or mark the source document as posted.

Errors expose a stable `code` value from `PostingErrorCode`; views should branch on that
code rather than parsing human-readable messages. Posting logs include only correlation
ID, model label, source ID, and actor ID—never amounts, credentials, or narration.

Use `PostingEngineStub` only in an integration that intentionally has persistence
disabled. New operational code should inject `PostingEngine`.

## Verification commands

The database-free contract suite is safe on every workstation:

```bash
python manage.py test apps.ledger.tests.test_posting_unit
```

The PostgreSQL integration suite verifies row locking and transactional rollback and
must run against an isolated test database, never the shared Supabase database:

```bash
python manage.py test apps.ledger.tests.test_posting_contract
python manage.py test apps.ledger.tests.test_posting_engine
```
