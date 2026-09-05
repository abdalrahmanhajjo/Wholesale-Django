"""Tax transactions and the money-account register (RPT-008, RPT-013).

Two listings that answer "show me the underlying transactions", which is what
someone reaches for when a statement figure looks wrong.

The tax report is the one with a rule worth knowing: **output and input tax are
not netted here.** A return is filed as tax charged on sales and tax reclaimed
on purchases, and the difference is a consequence of those two numbers rather
than a number in its own right. Summing them into one figure loses the two
lines the filing actually asks for.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.db import connection

ZERO = Decimal("0")

SALES, PURCHASE = "SALES", "PURCHASE"

SIDE_LABELS = {
    SALES: "Output tax (on sales)",
    PURCHASE: "Input tax (on purchases)",
}


@dataclass(frozen=True, slots=True)
class TaxLine:
    tax_side: str
    document_date: date
    document_number: str
    party_name: str
    tax_code: str
    tax_treatment: str
    rate_percent: Decimal
    is_inclusive: bool
    is_recoverable: bool
    taxable_base: Decimal
    tax_amount: Decimal


@dataclass(frozen=True, slots=True)
class TaxGroup:
    """One tax code within one side of the return."""

    tax_code: str
    treatment: str
    rate_percent: Decimal
    lines: tuple[TaxLine, ...]
    taxable_base: Decimal
    tax_amount: Decimal
    #: Non-recoverable input tax is a cost, not something to reclaim, so it is
    #: carried separately rather than folded into the reclaimable total.
    non_recoverable: Decimal


@dataclass(frozen=True, slots=True)
class TaxSide:
    side: str
    groups: tuple[TaxGroup, ...]
    taxable_base: Decimal
    tax_amount: Decimal
    non_recoverable: Decimal

    @property
    def label(self) -> str:
        return SIDE_LABELS.get(self.side, self.side)


@dataclass(frozen=True, slots=True)
class TaxReport:
    date_from: date
    date_to: date
    sales: TaxSide
    purchases: TaxSide

    @property
    def sides(self) -> tuple[TaxSide, TaxSide]:
        """Both sides in filing order, so a template can iterate them."""
        return (self.sales, self.purchases)

    @property
    def net_payable(self) -> Decimal:
        """Output tax less the input tax that may actually be reclaimed."""
        reclaimable = self.purchases.tax_amount - self.purchases.non_recoverable
        return self.sales.tax_amount - reclaimable


def _tax_lines(date_from: date, date_to: date) -> list[TaxLine]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT tax_side, document_date, document_number, party_name,
                   tax_code, tax_treatment, tax_rate_percent,
                   tax_is_inclusive, tax_is_recoverable,
                   taxable_base, tax_amount_base
            FROM v_tax_transaction
            WHERE document_date BETWEEN %s AND %s
            ORDER BY tax_side, tax_code, document_date, document_number
            """,
            [date_from, date_to],
        )
        return [
            TaxLine(
                tax_side=row[0],
                document_date=row[1],
                document_number=row[2],
                party_name=row[3],
                tax_code=row[4] or "—",
                tax_treatment=row[5] or "",
                rate_percent=Decimal(row[6] or 0),
                is_inclusive=bool(row[7]),
                is_recoverable=bool(row[8]),
                taxable_base=Decimal(row[9] or 0),
                tax_amount=Decimal(row[10] or 0),
            )
            for row in cursor.fetchall()
        ]


def _side(side: str, lines: list[TaxLine]) -> TaxSide:
    mine = [line for line in lines if line.tax_side == side]
    by_code: dict[str, list[TaxLine]] = {}
    for line in mine:
        by_code.setdefault(line.tax_code, []).append(line)

    groups = []
    for code, group_lines in sorted(by_code.items()):
        groups.append(
            TaxGroup(
                tax_code=code,
                treatment=group_lines[0].tax_treatment,
                rate_percent=group_lines[0].rate_percent,
                lines=tuple(group_lines),
                taxable_base=sum((line.taxable_base for line in group_lines), ZERO),
                tax_amount=sum((line.tax_amount for line in group_lines), ZERO),
                non_recoverable=sum(
                    (line.tax_amount for line in group_lines if not line.is_recoverable),
                    ZERO,
                ),
            )
        )
    return TaxSide(
        side=side,
        groups=tuple(groups),
        taxable_base=sum((group.taxable_base for group in groups), ZERO),
        tax_amount=sum((group.tax_amount for group in groups), ZERO),
        non_recoverable=sum((group.non_recoverable for group in groups), ZERO),
    )


def tax_report(date_from: date, date_to: date) -> TaxReport:
    lines = _tax_lines(date_from, date_to)
    return TaxReport(
        date_from=date_from,
        date_to=date_to,
        sales=_side(SALES, lines),
        purchases=_side(PURCHASE, lines),
    )


# ---------------------------------------------------------------------------
# Money account register (RPT-013)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class RegisterEntry:
    entry_date: date
    entry_number: str
    journal_type: str
    description: str
    money_in: Decimal
    money_out: Decimal
    balance: Decimal


@dataclass(frozen=True, slots=True)
class MoneyRegister:
    money_account_id: int | None
    date_from: date
    date_to: date
    opening_balance: Decimal
    entries: tuple[RegisterEntry, ...]
    total_in: Decimal
    total_out: Decimal

    @property
    def closing_balance(self) -> Decimal:
        return self.opening_balance + self.total_in - self.total_out


def money_register(*, money_account_id: int, date_from: date, date_to: date) -> MoneyRegister:
    """One money account's movements, with the balance each one left behind.

    The opening balance is everything before the window, fetched separately
    rather than by pulling the whole history and slicing it - an account that
    has been running for years should not cost a full table scan to show one
    month of it.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT COALESCE(SUM(net_base), 0)
            FROM v_money_account_activity
            WHERE money_account_id = %s AND entry_date < %s
            """,
            [money_account_id, date_from],
        )
        opening = Decimal(cursor.fetchone()[0] or 0)

        cursor.execute(
            """
            SELECT entry_date, entry_number, journal_type, description,
                   money_in_base, money_out_base
            FROM v_money_account_activity
            WHERE money_account_id = %s AND entry_date BETWEEN %s AND %s
            ORDER BY entry_date, entry_number
            """,
            [money_account_id, date_from, date_to],
        )
        rows = cursor.fetchall()

    balance = opening
    entries = []
    total_in = total_out = ZERO
    for row in rows:
        money_in = Decimal(row[4] or 0)
        money_out = Decimal(row[5] or 0)
        balance = balance + money_in - money_out
        total_in += money_in
        total_out += money_out
        entries.append(
            RegisterEntry(
                entry_date=row[0],
                entry_number=row[1],
                journal_type=row[2],
                description=row[3] or "",
                money_in=money_in,
                money_out=money_out,
                balance=balance,
            )
        )

    return MoneyRegister(
        money_account_id=money_account_id,
        date_from=date_from,
        date_to=date_to,
        opening_balance=opening,
        entries=tuple(entries),
        total_in=total_in,
        total_out=total_out,
    )
