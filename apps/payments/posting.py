"""Journal builders for payment posting and advance allocation.

The builders are deliberately pure: they resolve configured accounts and return
an immutable ``JournalDraft``. Persistence, balance checks, period checks, and
idempotency stay inside the centralized posting engine.
"""

from collections.abc import Callable
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError

from apps.ledger.models import Account, AccountMapping, JournalType, MappingKey
from apps.ledger.services.posting import JournalDraft, JournalLineDraft
from apps.payments.models import Payment, PaymentDirection


def _mapped_account(key: str) -> Account:
    mapping = AccountMapping.objects.select_related("account").filter(key=key).first()
    if mapping is None:
        raise ValidationError(
            f"No account mapping is configured for {MappingKey(key).label}. "
            "Ask an administrator to complete Account Mappings in Settings."
        )
    return mapping.account


def payment_required_mappings(payment: Payment) -> tuple[str, ...]:
    if payment.direction == PaymentDirection.RECEIPT:
        return (MappingKey.CUSTOMER_ADVANCE,)
    return (MappingKey.VENDOR_ADVANCE,)


def build_payment_journal(payment: Payment, *, user) -> JournalDraft:
    """Post cash/bank once; an unapplied payment initially becomes an advance."""
    cash_account = payment.money_account.gl_account
    description = f"{payment.get_direction_display()} {payment.number}"

    if payment.direction == PaymentDirection.RECEIPT:
        lines = (
            JournalLineDraft(
                account=cash_account,
                description=description,
                debit_base=payment.amount_base,
                debit_txn=payment.amount_txn,
                customer=payment.customer,
                money_account=payment.money_account,
            ),
            JournalLineDraft(
                account=_mapped_account(MappingKey.CUSTOMER_ADVANCE),
                description=f"Unallocated customer advance {payment.number}",
                credit_base=payment.amount_base,
                credit_txn=payment.amount_txn,
                customer=payment.customer,
            ),
        )
        source_doc_type = "RC"
    else:
        lines = (
            JournalLineDraft(
                account=_mapped_account(MappingKey.VENDOR_ADVANCE),
                description=f"Unallocated vendor advance {payment.number}",
                debit_base=payment.amount_base,
                debit_txn=payment.amount_txn,
                vendor=payment.vendor,
            ),
            JournalLineDraft(
                account=cash_account,
                description=description,
                credit_base=payment.amount_base,
                credit_txn=payment.amount_txn,
                vendor=payment.vendor,
                money_account=payment.money_account,
            ),
        )
        source_doc_type = "PV"

    return JournalDraft(
        entry_date=payment.posting_date,
        journal_type=JournalType.CASH,
        narration=description,
        currency=payment.currency,
        exchange_rate=payment.exchange_rate,
        source_doc_type=source_doc_type,
        source_doc_number=payment.number,
        lines=lines,
    )


def allocation_required_mappings(payment: Payment) -> tuple[str, ...]:
    if payment.direction == PaymentDirection.RECEIPT:
        return (MappingKey.CUSTOMER_ADVANCE, MappingKey.ACCOUNTS_RECEIVABLE)
    return (MappingKey.VENDOR_ADVANCE, MappingKey.ACCOUNTS_PAYABLE)


def make_allocation_journal_builder(
    *, allocation_date: date, amount_txn: Decimal, amount_base: Decimal
) -> Callable:
    """Build the advance-to-control-account reclassification for one batch."""

    def build(payment: Payment, *, user) -> JournalDraft:
        description = f"Allocate {payment.number} to open documents"
        if payment.direction == PaymentDirection.RECEIPT:
            lines = (
                JournalLineDraft(
                    account=_mapped_account(MappingKey.CUSTOMER_ADVANCE),
                    description=description,
                    debit_base=amount_base,
                    debit_txn=amount_txn,
                    customer=payment.customer,
                ),
                JournalLineDraft(
                    account=_mapped_account(MappingKey.ACCOUNTS_RECEIVABLE),
                    description=description,
                    credit_base=amount_base,
                    credit_txn=amount_txn,
                    customer=payment.customer,
                ),
            )
        else:
            lines = (
                JournalLineDraft(
                    account=_mapped_account(MappingKey.ACCOUNTS_PAYABLE),
                    description=description,
                    debit_base=amount_base,
                    debit_txn=amount_txn,
                    vendor=payment.vendor,
                ),
                JournalLineDraft(
                    account=_mapped_account(MappingKey.VENDOR_ADVANCE),
                    description=description,
                    credit_base=amount_base,
                    credit_txn=amount_txn,
                    vendor=payment.vendor,
                ),
            )

        return JournalDraft(
            entry_date=allocation_date,
            journal_type=JournalType.CASH,
            narration=description,
            currency=payment.currency,
            exchange_rate=payment.exchange_rate,
            source_doc_type="ALOC",
            source_doc_number=payment.number,
            lines=lines,
        )

    return build
