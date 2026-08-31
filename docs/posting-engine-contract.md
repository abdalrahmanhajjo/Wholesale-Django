# Posting engine contract — Day 1 handoff

Members 2 and 3 should import the contract from `apps.ledger.services`. Operational
apps build journal drafts; only Member 4's posting service writes ledger rows.

Architecture rationale: [ADR 0001](adr/0001-centralized-posting-service.md).

```python
from decimal import Decimal

from apps.ledger.models import JournalType
from apps.ledger.services import JournalDraft, JournalLineDraft, PostingRequest


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
    reason=form.cleaned_data.get("reason", ""),
)
# Available on Day 1: build and validate the draft without persisting anything.
draft = posting_service.preview(request)

# Day 2: the concrete service will be injected into the view/application service.
result = posting_service.post(request)
journal = result.journal_entry
```

## Contract rules

- Save the source before posting; the service reloads it with `select_for_update()`.
- Supply a deterministic idempotency key, maximum 120 characters. Do not use a random
  value for retries of the same business action.
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

## Day-1 runtime behavior

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

The Day-2 engine will add mapping validation, balance enforcement, idempotent lookup,
number allocation, persistence, status/audit callbacks, and safe concurrency handling
behind this unchanged public interface.

## Verification commands

The database-free contract suite is safe on every workstation:

```bash
python manage.py test apps.ledger.tests.test_posting_unit
```

The PostgreSQL integration suite verifies row locking and transactional rollback and
must run against an isolated test database, never the shared Supabase database:

```bash
python manage.py test apps.ledger.tests.test_posting_contract
```
