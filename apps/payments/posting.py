"""Journal builders for payment posting and advance allocation.

The builders are deliberately pure: they resolve configured accounts and return
an immutable ``JournalDraft``. Persistence, balance checks, period checks, and
idempotency stay inside the centralized posting engine.
"""

from collections.abc import Callable
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError

from apps.ledger.models import (
    Account,
    AccountMapping,
    JournalEntry,
    JournalType,
    MappingKey,
)
from apps.ledger.services.posting import JournalDraft, JournalLineDraft
from apps.payments.models import Payment, PaymentDirection

ZERO = Decimal("0")


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
        keys = [MappingKey.CUSTOMER_ADVANCE]
    else:
        keys = [MappingKey.VENDOR_ADVANCE]
    # Gated on the base amount, not the transaction amount, because that is the
    # test the builder applies before it emits the line — and the engine rejects
    # a required mapping the journal turns out never to use.
    if payment.fee_base > ZERO:
        keys.append(MappingKey.MERCHANT_FEE)
    return tuple(keys)


def build_payment_journal(payment: Payment, *, user) -> JournalDraft:
    """Post cash/bank once; an unapplied payment initially becomes an advance.

    A processor fee splits the cash side without touching the advance. The party
    is settled for the gross either way — that is what clears their balance — so
    only the money that genuinely moved reaches the money account, and the
    difference lands in expense.

    The base-currency fee is subtracted from (or added to) ``amount_base``
    rather than re-derived from the net at the exchange rate. Converting the net
    separately can round a quantum away from the gross and leave the journal
    a hundredth of a cent out of balance.
    """
    cash_account = payment.money_account.gl_account
    description = f"{payment.get_direction_display()} {payment.number}"
    has_fee = payment.fee_base > ZERO
    fee_description = f"Processor fee on {payment.number}"

    if payment.direction == PaymentDirection.RECEIPT:
        lines = (
            JournalLineDraft(
                account=cash_account,
                description=description,
                debit_base=payment.amount_base - payment.fee_base,
                debit_txn=payment.amount_txn - payment.fee_txn,
                customer=payment.customer,
                money_account=payment.money_account,
            ),
            *(
                (
                    JournalLineDraft(
                        account=_mapped_account(MappingKey.MERCHANT_FEE),
                        description=fee_description,
                        debit_base=payment.fee_base,
                        debit_txn=payment.fee_txn,
                        customer=payment.customer,
                    ),
                )
                if has_fee
                else ()
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
            *(
                (
                    JournalLineDraft(
                        account=_mapped_account(MappingKey.MERCHANT_FEE),
                        description=fee_description,
                        debit_base=payment.fee_base,
                        debit_txn=payment.fee_txn,
                        vendor=payment.vendor,
                    ),
                )
                if has_fee
                else ()
            ),
            JournalLineDraft(
                account=cash_account,
                description=description,
                credit_base=payment.amount_base + payment.fee_base,
                credit_txn=payment.amount_txn + payment.fee_txn,
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


def allocation_required_mappings(
    payment: Payment, *, fx_difference_base: Decimal = ZERO
) -> tuple[str, ...]:
    """Accounts the allocation journal must be able to reach.

    The FX account is only listed when the rates actually moved. The posting
    engine rejects a required mapping the journal never uses, so asking for an
    FX account on a no-FX allocation would fail the post.
    """
    if payment.direction == PaymentDirection.RECEIPT:
        keys = [MappingKey.CUSTOMER_ADVANCE, MappingKey.ACCOUNTS_RECEIVABLE]
    else:
        keys = [MappingKey.VENDOR_ADVANCE, MappingKey.ACCOUNTS_PAYABLE]
    if fx_difference_base:
        keys.append(_fx_mapping_key(payment, fx_difference_base))
    return tuple(keys)


def _fx_mapping_key(payment: Payment, fx_difference_base: Decimal) -> str:
    """Which FX account absorbs the difference.

    ``fx_difference_base`` is the advance's carrying value minus the document's
    — that is, what the money was worth when it arrived, against what the debt
    was booked at. Receiving more base currency than the receivable carried is a
    gain; paying more base currency than the payable carried is a loss.
    """
    if payment.direction == PaymentDirection.RECEIPT:
        return MappingKey.FX_GAIN if fx_difference_base > ZERO else MappingKey.FX_LOSS
    return MappingKey.FX_LOSS if fx_difference_base > ZERO else MappingKey.FX_GAIN


def make_allocation_journal_builder(
    *,
    allocation_date: date,
    amount_txn: Decimal,
    source_base: Decimal,
    target_base: Decimal,
) -> Callable:
    """Build the advance-to-control reclassification, with realised FX.

    ``source_base`` values the settled amount at the payment's own rate;
    ``target_base`` values it at each settled document's rate. When a rate has
    moved between the two dates the figures differ, and the difference is a
    realised exchange gain or loss — the third line that balances the entry.
    Where no rate moved the two agree and the journal is the plain two-line
    reclassification it always was.
    """

    def build(payment: Payment, *, user) -> JournalDraft:
        description = f"Allocate {payment.number} to open documents"
        difference = source_base - target_base
        fx_line = ()
        if difference:
            fx_account = _mapped_account(_fx_mapping_key(payment, difference))
            fx_description = f"Realised FX on {payment.number}"
            # The FX line carries base currency only: the difference exists
            # solely because two rates disagree, so it has no transaction amount.
            if payment.direction == PaymentDirection.RECEIPT:
                fx_line = (
                    JournalLineDraft(
                        account=fx_account,
                        description=fx_description,
                        credit_base=difference,
                        customer=payment.customer,
                    )
                    if difference > ZERO
                    else JournalLineDraft(
                        account=fx_account,
                        description=fx_description,
                        debit_base=-difference,
                        customer=payment.customer,
                    ),
                )
            else:
                fx_line = (
                    JournalLineDraft(
                        account=fx_account,
                        description=fx_description,
                        debit_base=difference,
                        vendor=payment.vendor,
                    )
                    if difference > ZERO
                    else JournalLineDraft(
                        account=fx_account,
                        description=fx_description,
                        credit_base=-difference,
                        vendor=payment.vendor,
                    ),
                )

        if payment.direction == PaymentDirection.RECEIPT:
            lines = (
                JournalLineDraft(
                    account=_mapped_account(MappingKey.CUSTOMER_ADVANCE),
                    description=description,
                    debit_base=source_base,
                    debit_txn=amount_txn,
                    customer=payment.customer,
                ),
                JournalLineDraft(
                    account=_mapped_account(MappingKey.ACCOUNTS_RECEIVABLE),
                    description=description,
                    credit_base=target_base,
                    credit_txn=amount_txn,
                    customer=payment.customer,
                ),
                *fx_line,
            )
        else:
            lines = (
                JournalLineDraft(
                    account=_mapped_account(MappingKey.ACCOUNTS_PAYABLE),
                    description=description,
                    debit_base=target_base,
                    debit_txn=amount_txn,
                    vendor=payment.vendor,
                ),
                JournalLineDraft(
                    account=_mapped_account(MappingKey.VENDOR_ADVANCE),
                    description=description,
                    credit_base=source_base,
                    credit_txn=amount_txn,
                    vendor=payment.vendor,
                ),
                *fx_line,
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


def make_reversing_journal_builder(
    original: JournalEntry, *, entry_date: date, reason: str
) -> Callable:
    """Mirror an existing journal, swapping every debit and credit.

    The ledger is append-only (BR-004), so undoing a posting is never an edit
    to the original — it is a second journal that cancels it and points back at
    what it reverses. Reading the lines back from the database rather than
    rebuilding them from the source means the reversal cancels what was
    *actually* posted, even if the account mappings have since been changed.
    """

    def build(source, *, user) -> JournalDraft:
        lines = []
        for line in original.lines.select_related("account").order_by("line_no"):
            shared = {
                "account": line.account,
                "description": f"Reversal of {original.number}",
                "customer": line.customer,
                "vendor": line.vendor,
                "product": line.product,
                "warehouse": line.warehouse,
                "tax_code": line.tax_code,
                "money_account": line.money_account,
            }
            if line.debit_base > ZERO:
                lines.append(
                    JournalLineDraft(
                        **shared, credit_base=line.debit_base, credit_txn=line.debit_txn
                    )
                )
            else:
                lines.append(
                    JournalLineDraft(
                        **shared, debit_base=line.credit_base, debit_txn=line.credit_txn
                    )
                )

        return JournalDraft(
            entry_date=entry_date,
            journal_type=original.journal_type,
            narration=f"Reversal of {original.number}: {reason}",
            currency=original.currency,
            exchange_rate=original.exchange_rate,
            source_doc_type="REVR",
            source_doc_number=original.source_doc_number or original.number,
            lines=tuple(lines),
            reverses=original,
            is_reversal=True,
            reversal_reason=reason,
        )

    return build


def mappings_used_by(journal: JournalEntry) -> tuple[str, ...]:
    """The configured mapping keys whose account this journal actually touches.

    A reversal takes its accounts from the journal it mirrors, not from the
    current configuration, so it cannot state its required mappings up front.
    Deriving them from the original satisfies the posting engine's two rules at
    once: at least one mapping is required, and every one named must be used.
    """
    account_ids = set(journal.lines.values_list("account_id", flat=True))
    return tuple(
        sorted(
            AccountMapping.objects.filter(account_id__in=account_ids).values_list(
                "key", flat=True
            )
        )
    )
