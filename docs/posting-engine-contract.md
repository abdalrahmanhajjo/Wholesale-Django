# Posting engine contract — Day 1 handoff

Members 2 and 3 should import the contract from `apps.ledger.services`. Operational
apps build journal drafts; only Member 4's posting service writes ledger rows.

```python
from decimal import Decimal

from apps.ledger.models import JournalType
from apps.ledger.services import JournalDraft, JournalLineDraft, PostingRequest


def build_purchase_bill_journal(bill, *, user):
    return JournalDraft(
        entry_date=bill.posting_date,
        journal_type=JournalType.PURCHASE,
        narration=f"Purchase bill {bill.number}",
        currency=bill.currency,
        exchange_rate=bill.exchange_rate,
        source_doc_type="PB",
        source_doc_number=bill.number,
        lines=(
            JournalLineDraft(account=inventory_account, debit_base=bill.taxable_base_base),
            JournalLineDraft(
                account=ap_account,
                credit_base=bill.total_base,
                vendor=bill.vendor,
            ),
        ),
    )


request = PostingRequest(
    source=bill,
    user=request.user,
    idempotency_key=f"purchase-bill:{bill.pk}:post:v1",
    build_journal=build_purchase_bill_journal,
    reason=form.cleaned_data.get("reason", ""),
)
journal = posting_service.post(request).journal_entry
```

## Contract rules

- Save the source before posting; the service reloads it with `select_for_update()`.
- Supply a deterministic idempotency key, maximum 120 characters. Do not use a random
  value for retries of the same business action.
- Use `Decimal` for every amount and rate. Never pass `float`.
- Builders return data only. They must not save rows, change document status, allocate
  a journal number, or create a nested transaction.
- Include customer/vendor and other dimensions on the relevant control-account lines.
- Let posting exceptions propagate. The service's outer `transaction.atomic()` rolls
  back the journal, status, stock/allocation effects, and audit event together.
- Do not create `JournalEntry`, `JournalLine`, or `PostingLink` directly outside the
  ledger service.

The Day-2 engine will add mapping validation, balance enforcement, idempotent lookup,
number allocation, persistence, status/audit callbacks, and safe concurrency handling
behind this unchanged public interface.
