"""The accounting identities the three statements are supposed to preserve.

These are written against a ledger this module builds line by line, rather than
against seeded data, because the properties worth protecting only appear when
there is a profit that has not been closed yet - and no fixture happens to have
one lying around.
"""

from datetime import date
from decimal import Decimal

from django.test import TestCase

from apps.core.models import Currency, FiscalPeriod, FiscalYear
from apps.ledger.models import (
    Account,
    AccountSubtype,
    AccountType,
    JournalEntry,
    JournalLine,
    JournalType,
    NormalBalance,
)
from apps.reports import services

ZERO = Decimal("0")


class LedgerFixture:
    """A tiny but complete chart of accounts, and a way to post into it."""

    @classmethod
    def setUpTestData(cls):
        cls.currency = Currency.objects.create(
            code="R6T", name="Reports Test Currency", symbol="R", is_active=True
        )
        year = FiscalYear.objects.create(
            code="R6-FY93", start_date=date(2093, 1, 1), end_date=date(2093, 12, 31)
        )
        cls.period = FiscalPeriod.objects.create(
            fiscal_year=year,
            period_no=1,
            name="Reports 2093",
            start_date=date(2093, 1, 1),
            end_date=date(2093, 12, 31),
        )
        cls.cash = cls._account(
            "R6-1100", "Cash", AccountType.ASSET, AccountSubtype.CURRENT_ASSET
        )
        cls.equipment = cls._account(
            "R6-1500", "Equipment", AccountType.ASSET, AccountSubtype.NONCURRENT_ASSET
        )
        cls.payables = cls._account(
            "R6-2100", "Payables", AccountType.LIABILITY, AccountSubtype.CURRENT_LIABILITY
        )
        cls.loan = cls._account(
            "R6-2500", "Bank loan", AccountType.LIABILITY, AccountSubtype.NONCURRENT_LIABILITY
        )
        cls.capital = cls._account(
            "R6-3100", "Share capital", AccountType.EQUITY, AccountSubtype.EQUITY
        )
        cls.revenue = cls._account(
            "R6-4100", "Sales", AccountType.INCOME, AccountSubtype.REVENUE
        )
        cls.returns = cls._account(
            "R6-4200",
            "Sales returns",
            AccountType.INCOME,
            AccountSubtype.REVENUE,
            is_contra=True,
        )
        cls.cogs = cls._account(
            "R6-5010", "Cost of sales", AccountType.EXPENSE, AccountSubtype.COGS
        )
        cls.rent = cls._account(
            "R6-6200", "Rent", AccountType.EXPENSE, AccountSubtype.OPERATING_EXPENSE
        )
        cls._entry_no = 0

    @classmethod
    def _account(cls, code, name, account_type, subtype, *, is_contra=False):
        debit_natured = account_type in (AccountType.ASSET, AccountType.EXPENSE)
        if is_contra:
            debit_natured = not debit_natured
        return Account.objects.create(
            code=code,
            name=name,
            account_type=account_type,
            subtype=subtype,
            normal_balance=(NormalBalance.DEBIT if debit_natured else NormalBalance.CREDIT),
            is_contra=is_contra,
        )

    @classmethod
    def post(cls, entry_date, *pairs):
        """Post one balanced journal. Each pair is (account, signed amount).

        A positive amount is a debit and a negative one a credit, which keeps
        the tests below readable as accounting rather than as bookkeeping
        plumbing.
        """
        cls._entry_no += 1
        total = sum(amount for _, amount in pairs)
        if total != ZERO:
            # A test that posts an unbalanced journal would be testing the
            # database's trigger, not this module, and its failure would point
            # at the wrong place. Fail here instead, where the mistake is.
            raise ValueError(f"test journal does not balance, off by {total}")
        entry = JournalEntry.objects.create(
            number=f"R6-JE-{cls._entry_no:04d}",
            entry_date=entry_date,
            fiscal_period=cls.period,
            journal_type=JournalType.GENERAL,
            narration="Reports test entry",
            currency=cls.currency,
            exchange_rate=Decimal("1"),
            total_debit_base=sum(a for _, a in pairs if a > 0),
            total_credit_base=-sum(a for _, a in pairs if a < 0),
        )
        for line_no, (account, amount) in enumerate(pairs, start=1):
            JournalLine.objects.create(
                entry=entry,
                line_no=line_no,
                account=account,
                currency=cls.currency,
                exchange_rate=Decimal("1"),
                debit_base=amount if amount > 0 else ZERO,
                debit_txn=amount if amount > 0 else ZERO,
                credit_base=-amount if amount < 0 else ZERO,
                credit_txn=-amount if amount < 0 else ZERO,
            )
        return entry


class TrialBalanceTests(LedgerFixture, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.post(
            date(2093, 1, 10), (cls.cash, Decimal("1000")), (cls.capital, Decimal("-1000"))
        )
        cls.post(date(2093, 3, 5), (cls.cash, Decimal("400")), (cls.revenue, Decimal("-400")))

    def _rows(self):
        tb = services.trial_balance(date(2093, 1, 1), date(2093, 12, 31))
        return tb, {row.code: row for row in tb.rows}

    def test_debits_equal_credits_in_every_column(self):
        tb, _ = self._rows()
        self.assertTrue(tb.is_balanced, f"out of balance by {tb.out_of_balance_by}")
        self.assertEqual(tb.out_of_balance_by, ZERO)

    def test_only_accounts_with_activity_appear(self):
        _, by_code = self._rows()
        self.assertIn("R6-1100", by_code)
        # Never posted to, so it is not a row on the report.
        self.assertNotIn("R6-2500", by_code)

    def test_an_earlier_window_puts_the_movement_in_opening(self):
        """The same entries, seen from a later window, are opening balances."""
        tb = services.trial_balance(date(2093, 6, 1), date(2093, 12, 31))
        by_code = {row.code: row for row in tb.rows}
        cash = by_code["R6-1100"]
        self.assertEqual(cash.opening_debit, Decimal("1400.0000"))
        self.assertEqual(cash.period_debit, ZERO)
        self.assertEqual(cash.closing_debit, Decimal("1400.0000"))

    def test_a_reversed_entry_and_its_reversal_cancel(self):
        """Reversal is a second entry, not a deletion - both must be counted.

        Filtering the ledger to status='POSTED' would drop the original and
        keep the reversal, leaving every report wrong by the amount reversed.
        """
        entry = self.post(
            date(2093, 4, 1), (self.cash, Decimal("250")), (self.revenue, Decimal("-250"))
        )
        self.post(
            date(2093, 4, 2), (self.cash, Decimal("-250")), (self.revenue, Decimal("250"))
        )
        entry.status = "REVERSED"
        entry.save(update_fields=["status"])

        tb = services.trial_balance(date(2093, 1, 1), date(2093, 12, 31))
        by_code = {row.code: row for row in tb.rows}
        self.assertTrue(tb.is_balanced)
        # Net effect of the pair is nothing; the 400 sale is all that remains.
        self.assertEqual(by_code["R6-4100"].closing_net, Decimal("400.0000"))


class TradingYearFixture(LedgerFixture):
    """A year with capital, a loan, an asset, sales, returns, and costs."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # Funded, then equipped, then trading.
        cls.post(
            date(2093, 1, 2), (cls.cash, Decimal("5000")), (cls.capital, Decimal("-5000"))
        )
        cls.post(date(2093, 1, 3), (cls.cash, Decimal("2000")), (cls.loan, Decimal("-2000")))
        cls.post(
            date(2093, 1, 15), (cls.equipment, Decimal("3000")), (cls.cash, Decimal("-3000"))
        )
        # A sale, the cost of it, a return, and a bill not yet paid.
        cls.post(
            date(2093, 2, 1), (cls.cash, Decimal("4000")), (cls.revenue, Decimal("-4000"))
        )
        cls.post(date(2093, 2, 1), (cls.cogs, Decimal("2400")), (cls.cash, Decimal("-2400")))
        cls.post(date(2093, 2, 20), (cls.returns, Decimal("500")), (cls.cash, Decimal("-500")))
        cls.post(
            date(2093, 3, 31), (cls.rent, Decimal("900")), (cls.payables, Decimal("-900"))
        )


class ProfitAndLossTests(TradingYearFixture, TestCase):
    def setUp(self):
        self.pl = services.profit_and_loss(date(2093, 1, 1), date(2093, 12, 31))

    def test_a_contra_account_reduces_its_own_section(self):
        """Sales returns are debit-natured income; revenue nets to 3,500."""
        self.assertEqual(self.pl.revenue.total, Decimal("3500.0000"))

    def test_gross_profit_is_revenue_less_cost_of_sales(self):
        self.assertEqual(self.pl.cost_of_sales.total, Decimal("2400.0000"))
        self.assertEqual(self.pl.gross_profit, Decimal("1100.0000"))

    def test_operating_profit_carries_the_expenses(self):
        self.assertEqual(self.pl.operating_expenses.total, Decimal("900.0000"))
        self.assertEqual(self.pl.operating_profit, Decimal("200.0000"))
        self.assertEqual(self.pl.net_profit, Decimal("200.0000"))

    def test_margin_is_reported_against_net_revenue(self):
        # 1,100 / 3,500
        self.assertEqual(self.pl.gross_margin_percent, Decimal("31.4"))

    def test_no_revenue_gives_no_margin_rather_than_a_crash(self):
        quiet = services.profit_and_loss(date(2093, 7, 1), date(2093, 7, 31))
        self.assertEqual(quiet.revenue.total, ZERO)
        self.assertIsNone(quiet.gross_margin_percent)

    def test_the_statement_covers_a_window_not_all_of_history(self):
        """A P&L that reported closing balances would grow forever."""
        february = services.profit_and_loss(date(2093, 2, 1), date(2093, 2, 28))
        self.assertEqual(february.revenue.total, Decimal("3500.0000"))
        # March's rent is outside the window.
        self.assertEqual(february.operating_expenses.total, ZERO)

    def test_balance_sheet_accounts_are_not_on_the_profit_and_loss(self):
        codes = {
            row.code
            for section in (
                self.pl.revenue,
                self.pl.cost_of_sales,
                self.pl.operating_expenses,
                self.pl.other_income,
                self.pl.other_expenses,
            )
            for row in section.rows
        }
        self.assertNotIn("R6-1100", codes)
        self.assertNotIn("R6-3100", codes)


class BalanceSheetTests(TradingYearFixture, TestCase):
    def setUp(self):
        self.bs = services.balance_sheet(date(2093, 12, 31))

    def test_it_balances(self):
        self.assertTrue(
            self.bs.is_balanced,
            f"assets {self.bs.total_assets} vs liabilities and equity "
            f"{self.bs.total_liabilities_and_equity}, out by {self.bs.out_of_balance_by}",
        )

    def test_it_only_balances_because_the_unclosed_profit_is_carried(self):
        """The point of result_for_period, stated as a test.

        Nothing has closed the year, so the 200 profit is still sitting in the
        income and expense accounts. Drop it from equity and the statement is
        out by exactly that much - which is what this asserts, so that removing
        the line fails here rather than silently producing a wrong statement.
        """
        self.assertEqual(self.bs.result_for_period, Decimal("200.0000"))
        equity_without_result = self.bs.equity.total
        self.assertNotEqual(
            self.bs.total_assets, self.bs.total_liabilities + equity_without_result
        )
        self.assertEqual(
            self.bs.total_assets - (self.bs.total_liabilities + equity_without_result),
            self.bs.result_for_period,
        )

    def test_the_result_matches_the_profit_and_loss_for_the_same_span(self):
        """The two statements are one calculation seen twice; they must agree."""
        pl = services.profit_and_loss(date(2093, 1, 1), date(2093, 12, 31))
        self.assertEqual(self.bs.result_for_period, pl.net_profit)

    def test_assets_are_split_current_and_non_current(self):
        # 5,000 + 2,000 - 3,000 + 4,000 - 2,400 - 500 = 5,100 cash
        self.assertEqual(self.bs.current_assets.total, Decimal("5100.0000"))
        self.assertEqual(self.bs.noncurrent_assets.total, Decimal("3000.0000"))
        self.assertEqual(self.bs.total_assets, Decimal("8100.0000"))

    def test_liabilities_are_split_and_positive(self):
        """A liability is credit-natured; it should never read as negative."""
        self.assertEqual(self.bs.current_liabilities.total, Decimal("900.0000"))
        self.assertEqual(self.bs.noncurrent_liabilities.total, Decimal("2000.0000"))
        self.assertGreater(self.bs.total_liabilities, ZERO)

    def test_it_still_balances_at_a_date_mid_year(self):
        mid = services.balance_sheet(date(2093, 2, 15))
        self.assertTrue(mid.is_balanced, f"out by {mid.out_of_balance_by}")
        # The February return and the March rent have not happened yet.
        self.assertEqual(mid.result_for_period, Decimal("1600.0000"))

    def test_closing_the_year_moves_the_result_into_equity(self):
        """After a closing entry the result line is zero and equity absorbs it.

        Same code, both sides of a year-end, which is the reason it is written
        as an identity rather than as a special case for open years.
        """
        self.post(
            date(2093, 12, 31),
            (self.revenue, Decimal("4000")),
            (self.returns, Decimal("-500")),
            (self.cogs, Decimal("-2400")),
            (self.rent, Decimal("-900")),
            (self.capital, Decimal("-200")),
        )
        closed = services.balance_sheet(date(2093, 12, 31))
        self.assertEqual(closed.result_for_period, ZERO)
        self.assertEqual(closed.equity.total, Decimal("5200.0000"))
        self.assertTrue(closed.is_balanced, f"out by {closed.out_of_balance_by}")
        self.assertEqual(closed.total_assets, self.bs.total_assets)
