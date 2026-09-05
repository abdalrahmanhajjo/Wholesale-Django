"""Trial balance, profit and loss, and balance sheet (RPT-001..RPT-005).

All three come from one place: `fn_trial_balance`. That is the whole design.

A trial balance, a P&L and a balance sheet are not three different questions
about the ledger - they are three presentations of the same account balances,
sliced differently. Computing them from three queries is how an organisation
ends up with a balance sheet that does not agree with its own trial balance,
and then spends a week finding out which one lied. Here the SQL function is
asked once per report and everything else is arithmetic on the answer, so the
statements cannot disagree.

Two pieces of ledger semantics that this module depends on, both of which look
like bugs to someone reading quickly:

**Reversed entries are included, deliberately.** The ledger is append-only
(BR-004): reversing entry A does not remove A's lines, it writes a second entry
B holding the opposite ones, and flips A's status to REVERSED. Both sets of
lines stay. Filtering to `status = 'POSTED'` would drop A while keeping B and
leave every report short by the reversal - which is why neither this module nor
`fn_trial_balance` filters on status at all.

**A balance sheet before year-end close must carry the year's result.** Until a
closing entry moves income and expense into retained earnings, the profit lives
in the P&L accounts, and a balance sheet listing only asset, liability and
equity accounts will not balance by exactly that amount. `balance_sheet` adds
it as an equity line. Once a year *is* closed, those accounts are zero and the
line reports nothing, so the same code is right in both cases without knowing
which it is looking at.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from operator import attrgetter

from django.db import connection

ZERO = Decimal("0")

#: Earlier than any plausible entry date. Used where a report wants balances
#: "since inception" - fn_trial_balance takes a window, so inception is
#: expressed as a window that starts before the business did.
INCEPTION = date(1900, 1, 1)

# Account types, matching apps.ledger.models.AccountType.
ASSET, LIABILITY, EQUITY, INCOME, EXPENSE = (
    "ASSET",
    "LIABILITY",
    "EQUITY",
    "INCOME",
    "EXPENSE",
)

#: Which side each account type is expected to sit on. A balance is presented
#: positive when it is on its natural side, so a reader is never asked to
#: interpret a negative liability. A contra account inverts this by its nature -
#: sales returns are debit-natured income - and needs no special case: the
#: subtraction below simply yields a negative, which is exactly what a contra
#: balance should look like inside its own section.
DEBIT_NATURED = frozenset({ASSET, EXPENSE})


@dataclass(frozen=True, slots=True)
class AccountBalance:
    """One account's movement and standing over a window."""

    account_id: int
    code: str
    name: str
    account_type: str
    subtype: str
    is_contra: bool
    opening_debit: Decimal
    opening_credit: Decimal
    period_debit: Decimal
    period_credit: Decimal
    closing_debit: Decimal
    closing_credit: Decimal

    @property
    def opening_net(self) -> Decimal:
        """Signed on the account's natural side."""
        return self._natural(self.opening_debit, self.opening_credit)

    @property
    def period_net(self) -> Decimal:
        return self._natural(self.period_debit, self.period_credit)

    @property
    def closing_net(self) -> Decimal:
        return self._natural(self.closing_debit, self.closing_credit)

    def _natural(self, debit: Decimal, credit: Decimal) -> Decimal:
        if self.account_type in DEBIT_NATURED:
            return debit - credit
        return credit - debit

    @property
    def has_activity(self) -> bool:
        """Whether this account is worth a row at all."""
        return any(
            (
                self.opening_debit,
                self.opening_credit,
                self.period_debit,
                self.period_credit,
                self.closing_debit,
                self.closing_credit,
            )
        )


def _decimal(value) -> Decimal:
    return ZERO if value is None else Decimal(value)


def account_balances(date_from: date, date_to: date) -> tuple[AccountBalance, ...]:
    """Every account's balances over a window, straight from fn_trial_balance.

    Joined to `account` for the subtype and contra flag, which the function does
    not return and which the statement groupings need. One query, and crucially
    one definition of "balance" shared by all three reports.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT tb.account_id, tb.account_code, tb.account_name, tb.account_type,
                   a.subtype, a.is_contra,
                   tb.opening_debit, tb.opening_credit,
                   tb.period_debit, tb.period_credit,
                   tb.closing_debit, tb.closing_credit
            FROM fn_trial_balance(%s, %s) AS tb
            JOIN account a ON a.id = tb.account_id
            ORDER BY tb.account_code
            """,
            [date_from, date_to],
        )
        rows = cursor.fetchall()

    return tuple(
        AccountBalance(
            account_id=row[0],
            code=row[1],
            name=row[2],
            account_type=row[3],
            subtype=row[4],
            is_contra=row[5],
            opening_debit=_decimal(row[6]),
            opening_credit=_decimal(row[7]),
            period_debit=_decimal(row[8]),
            period_credit=_decimal(row[9]),
            closing_debit=_decimal(row[10]),
            closing_credit=_decimal(row[11]),
        )
        for row in rows
    )


# ---------------------------------------------------------------------------
# Trial balance (RPT-003)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class TrialBalance:
    date_from: date
    date_to: date
    rows: tuple[AccountBalance, ...]
    opening_debit: Decimal
    opening_credit: Decimal
    period_debit: Decimal
    period_credit: Decimal
    closing_debit: Decimal
    closing_credit: Decimal

    @property
    def is_balanced(self) -> bool:
        """Debits equal credits in every column.

        This should be impossible to violate - the posting engine rejects an
        unbalanced journal and a database trigger rejects one that gets past it
        - which is exactly why it is worth showing. If it ever reads false, the
        ledger has been changed by something that is not the posting engine,
        and that is the most important thing this screen could tell anyone.
        """
        return (
            self.opening_debit == self.opening_credit
            and self.period_debit == self.period_credit
            and self.closing_debit == self.closing_credit
        )

    @property
    def out_of_balance_by(self) -> Decimal:
        return self.closing_debit - self.closing_credit


def trial_balance(date_from: date, date_to: date) -> TrialBalance:
    rows = tuple(row for row in account_balances(date_from, date_to) if row.has_activity)

    def total(field: str) -> Decimal:
        return sum((getattr(row, field) for row in rows), ZERO)

    return TrialBalance(
        date_from=date_from,
        date_to=date_to,
        rows=rows,
        opening_debit=total("opening_debit"),
        opening_credit=total("opening_credit"),
        period_debit=total("period_debit"),
        period_credit=total("period_credit"),
        closing_debit=total("closing_debit"),
        closing_credit=total("closing_credit"),
    )


# ---------------------------------------------------------------------------
# Statement scaffolding
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Section:
    """A named group of accounts and its total, as printed."""

    title: str
    rows: tuple[AccountBalance, ...]
    total: Decimal

    @property
    def is_empty(self) -> bool:
        return not self.rows


def _section(title: str, rows, amount_of) -> Section:
    kept = tuple(row for row in rows if amount_of(row) != ZERO)
    return Section(title=title, rows=kept, total=sum((amount_of(r) for r in kept), ZERO))


# ---------------------------------------------------------------------------
# Profit and loss (RPT-002)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ProfitAndLoss:
    date_from: date
    date_to: date
    revenue: Section
    cost_of_sales: Section
    operating_expenses: Section
    other_income: Section
    other_expenses: Section

    @property
    def gross_profit(self) -> Decimal:
        return self.revenue.total - self.cost_of_sales.total

    @property
    def operating_profit(self) -> Decimal:
        return self.gross_profit - self.operating_expenses.total

    @property
    def net_profit(self) -> Decimal:
        return self.operating_profit + self.other_income.total - self.other_expenses.total

    @property
    def gross_margin_percent(self) -> Decimal | None:
        if not self.revenue.total:
            return None
        return (self.gross_profit / self.revenue.total * 100).quantize(Decimal("0.1"))


def profit_and_loss(date_from: date, date_to: date) -> ProfitAndLoss:
    """Income and expenditure *over a window* - movement, not standing balance.

    Uses period_net rather than closing_net, which is the difference between
    "what did the business earn in March" and "what has it earned since it
    opened". Getting that wrong produces a P&L that grows forever and is the
    single easiest mistake to make here.
    """
    rows = account_balances(date_from, date_to)
    movement = attrgetter("period_net")

    def of(subtype: str):
        return (row for row in rows if row.subtype == subtype)

    return ProfitAndLoss(
        date_from=date_from,
        date_to=date_to,
        revenue=_section("Revenue", of("REVENUE"), movement),
        cost_of_sales=_section("Cost of sales", of("COGS"), movement),
        operating_expenses=_section("Operating expenses", of("OPERATING_EXPENSE"), movement),
        other_income=_section("Other income", of("OTHER_INCOME"), movement),
        other_expenses=_section("Other expenses", of("OTHER_EXPENSE"), movement),
    )


# ---------------------------------------------------------------------------
# Balance sheet (RPT-001)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class BalanceSheet:
    as_of: date
    current_assets: Section
    noncurrent_assets: Section
    current_liabilities: Section
    noncurrent_liabilities: Section
    equity: Section
    #: Income less expenses to date, still sitting in the P&L accounts because
    #: no closing entry has moved it. Zero on a closed year.
    result_for_period: Decimal

    @property
    def total_assets(self) -> Decimal:
        return self.current_assets.total + self.noncurrent_assets.total

    @property
    def total_liabilities(self) -> Decimal:
        return self.current_liabilities.total + self.noncurrent_liabilities.total

    @property
    def total_equity(self) -> Decimal:
        return self.equity.total + self.result_for_period

    @property
    def total_liabilities_and_equity(self) -> Decimal:
        return self.total_liabilities + self.total_equity

    @property
    def is_balanced(self) -> bool:
        return self.total_assets == self.total_liabilities_and_equity

    @property
    def out_of_balance_by(self) -> Decimal:
        return self.total_assets - self.total_liabilities_and_equity


def balance_sheet(as_of: date) -> BalanceSheet:
    """What the business owns and owes at a single date.

    Standing balances since inception, not movement - which is why the window
    starts at INCEPTION rather than at the beginning of anything.

    The result line is the part worth understanding. Every journal balances, so
    across all accounts debits equal credits, which rearranges to:

        assets - liabilities - equity = income - expenses

    The right-hand side is the profit still held in the P&L accounts. Adding it
    to equity is not a plug to force the statement to balance; it is the same
    identity written the way a reader expects to see it. A business that has
    closed its year has zero there and the line disappears on its own.
    """
    rows = account_balances(INCEPTION, as_of)
    standing = attrgetter("closing_net")

    def of(subtype: str):
        return (row for row in rows if row.subtype == subtype)

    def total_for(account_type: str) -> Decimal:
        return sum((row.closing_net for row in rows if row.account_type == account_type), ZERO)

    return BalanceSheet(
        as_of=as_of,
        current_assets=_section("Current assets", of("CURRENT_ASSET"), standing),
        noncurrent_assets=_section("Non-current assets", of("NONCURRENT_ASSET"), standing),
        current_liabilities=_section("Current liabilities", of("CURRENT_LIABILITY"), standing),
        noncurrent_liabilities=_section(
            "Non-current liabilities", of("NONCURRENT_LIABILITY"), standing
        ),
        equity=_section("Equity", of("EQUITY"), standing),
        result_for_period=total_for(INCOME) - total_for(EXPENSE),
    )
