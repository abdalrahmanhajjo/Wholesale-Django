"""Ageing, tax and register reports (RPT-006, RPT-007, RPT-008, RPT-013)."""

from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase

from apps.core.models import Currency, DocumentStatus, FiscalPeriod, FiscalYear
from apps.parties.models import Customer
from apps.reports import ageing, registers
from apps.sales.models import SalesInvoice

ZERO = Decimal("0")


class AgeingFixture:
    """Invoices at known distances from their due date, in two currencies."""

    AS_OF = date(2095, 6, 30)

    @classmethod
    def setUpTestData(cls):
        cls.usd = Currency.objects.create(
            code="R8A", name="Ageing Currency A", symbol="A", is_active=True
        )
        cls.eur = Currency.objects.create(
            code="R8B", name="Ageing Currency B", symbol="B", is_active=True
        )
        cls.alice = Customer.objects.create(
            code="R8-ALICE", name="Alice Trading", currency=cls.usd
        )
        cls.bob = Customer.objects.create(code="R8-BOB", name="Bob Supplies", currency=cls.usd)
        # Spans two years because the oldest invoice here is 200 days overdue
        # at AS_OF, which puts its journal back in 2094 - and BR-020 requires an
        # entry date to fall inside the period it points at.
        year = FiscalYear.objects.create(
            code="R8-FY95", start_date=date(2094, 1, 1), end_date=date(2095, 12, 31)
        )
        cls.period = FiscalPeriod.objects.create(
            fiscal_year=year,
            period_no=1,
            name="R8 2094-95",
            start_date=date(2094, 1, 1),
            end_date=date(2095, 12, 31),
        )
        # days_overdue relative to AS_OF decides the bucket.
        cls.current = cls._invoice(
            cls.alice, "R8-INV-CURRENT", cls.usd, days_overdue=-5, total="100"
        )
        cls.mid = cls._invoice(cls.alice, "R8-INV-45", cls.usd, days_overdue=45, total="200")
        cls.ancient = cls._invoice(
            cls.bob, "R8-INV-200", cls.usd, days_overdue=200, total="500"
        )
        cls.other_currency = cls._invoice(
            cls.bob, "R8-INV-FX", cls.eur, days_overdue=10, total="999"
        )

    @classmethod
    def _accounts(cls):
        """Two accounts, made once, for the stub journals the invoices need."""
        from apps.ledger.models import (
            Account,
            AccountSubtype,
            AccountType,
            NormalBalance,
        )

        if not hasattr(cls, "_receivable"):
            cls._receivable = Account.objects.create(
                code="R8-1210",
                name="Ageing receivables",
                account_type=AccountType.ASSET,
                subtype=AccountSubtype.CURRENT_ASSET,
                normal_balance=NormalBalance.DEBIT,
            )
            cls._revenue = Account.objects.create(
                code="R8-4100",
                name="Ageing revenue",
                account_type=AccountType.INCOME,
                subtype=AccountSubtype.REVENUE,
                normal_balance=NormalBalance.CREDIT,
            )
        return cls._receivable, cls._revenue

    @classmethod
    def _journal(cls, number, when, amount, currency):
        """si_posted_has_journal means a posted invoice must carry one, and a
        deferred trigger means that journal must have balanced lines."""
        from apps.ledger.models import JournalEntry, JournalLine, JournalType

        receivable, revenue = cls._accounts()
        entry = JournalEntry.objects.create(
            number=f"JE-{number}",
            entry_date=when,
            fiscal_period=cls.period,
            journal_type=JournalType.SALES,
            narration=f"Stub journal for {number}",
            currency=currency,
            exchange_rate=Decimal("1"),
            total_debit_base=amount,
            total_credit_base=amount,
        )
        for line_no, (account, side) in enumerate(
            ((receivable, "debit"), (revenue, "credit")), start=1
        ):
            JournalLine.objects.create(
                entry=entry,
                line_no=line_no,
                account=account,
                currency=currency,
                exchange_rate=Decimal("1"),
                **{f"{side}_base": amount, f"{side}_txn": amount},
            )
        return entry

    @classmethod
    def _invoice(cls, customer, number, currency, *, days_overdue, total):
        due = cls.AS_OF - timedelta(days=days_overdue)
        amount = Decimal(total)
        return SalesInvoice.objects.create(
            journal_entry=cls._journal(number, due - timedelta(days=30), amount, currency),
            number=number,
            document_date=due - timedelta(days=30),
            due_date=due,
            posting_date=due - timedelta(days=30),
            fiscal_period=cls.period,
            customer=customer,
            currency=currency,
            exchange_rate=Decimal("1"),
            total_txn=amount,
            total_base=amount,
            open_txn=amount,
            status=DocumentStatus.POSTED,
        )


class ReceivablesAgeingTests(AgeingFixture, TestCase):
    def report(self, currency=None):
        return ageing.ageing(ageing.AR, self.AS_OF, currency or self.usd.code)

    def test_documents_land_in_the_right_bucket(self):
        by_number = {
            item.document_number: item
            for party in self.report().parties
            for item in party.items
        }
        self.assertEqual(by_number["R8-INV-CURRENT"].bucket, "CURRENT")
        self.assertEqual(by_number["R8-INV-45"].bucket, "31-60")
        self.assertEqual(by_number["R8-INV-200"].bucket, "90+")

    def test_bucket_totals_add_up_to_the_report_total(self):
        report = self.report()
        self.assertEqual(sum(report.buckets.values(), ZERO), report.total)
        self.assertEqual(report.total, Decimal("800.0000"))

    def test_another_currency_is_excluded_and_named(self):
        """A total that added the two currencies together would be meaningless."""
        report = self.report()
        numbers = {item.document_number for p in report.parties for item in p.items}
        self.assertNotIn("R8-INV-FX", numbers)
        self.assertEqual(report.other_currencies, ((self.eur.code, 1),))

    def test_switching_currency_shows_the_other_documents(self):
        report = self.report(self.eur.code)
        numbers = {item.document_number for p in report.parties for item in p.items}
        self.assertEqual(numbers, {"R8-INV-FX"})
        self.assertEqual(report.total, Decimal("999.0000"))

    def test_the_worst_debt_is_listed_first(self):
        """An ageing is a work list; the top of it is who to ring today."""
        names = [party.party_name for party in self.report().parties]
        self.assertEqual(names[0], "Bob Supplies")

    def test_overdue_excludes_what_is_not_yet_due(self):
        report = self.report()
        self.assertEqual(report.overdue, Decimal("700.0000"))
        self.assertEqual(report.buckets["CURRENT"], Decimal("100.0000"))

    def test_bucket_cells_are_ordered_and_labelled(self):
        """Templates iterate these rather than looking a dict up by key."""
        cells = self.report().bucket_cells
        self.assertEqual([cell.key for cell in cells], list(ageing.BUCKETS))
        self.assertTrue(all(cell.label for cell in cells))

    def test_an_item_fills_only_its_own_bucket_column(self):
        item = next(
            item
            for party in self.report().parties
            for item in party.items
            if item.document_number == "R8-INV-45"
        )
        filled = [cell for cell in item.bucket_cells if not cell.is_empty]
        self.assertEqual(len(filled), 1)
        self.assertEqual(filled[0].key, "31-60")

    def test_an_earlier_as_of_date_ages_documents_less(self):
        """The function recomputes from allocations, so history is answerable."""
        earlier = ageing.ageing(ageing.AR, self.AS_OF - timedelta(days=60), self.usd.code)
        by_number = {
            item.document_number: item for party in earlier.parties for item in party.items
        }
        # 45 days overdue at AS_OF was not yet due 60 days earlier.
        self.assertEqual(by_number["R8-INV-45"].bucket, "CURRENT")

    def test_an_unknown_side_is_refused(self):
        with self.assertRaises(ValueError):
            ageing.ageing("SOMETHING", self.AS_OF, self.usd.code)


class TaxReportTests(TestCase):
    def test_the_two_sides_are_reported_separately(self):
        """A return is filed as output and input tax, not as one netted figure."""
        report = registers.tax_report(date(2095, 1, 1), date(2095, 12, 31))
        self.assertEqual(report.sales.side, registers.SALES)
        self.assertEqual(report.purchases.side, registers.PURCHASE)
        self.assertEqual(len(report.sides), 2)

    def test_net_payable_excludes_non_recoverable_input_tax(self):
        """Non-recoverable input tax is a cost, not something to reclaim."""
        report = registers.tax_report(date(2095, 1, 1), date(2095, 12, 31))
        expected = report.sales.tax_amount - (
            report.purchases.tax_amount - report.purchases.non_recoverable
        )
        self.assertEqual(report.net_payable, expected)

    def test_group_totals_add_up_to_the_side_total(self):
        report = registers.tax_report(date(2000, 1, 1), date(2100, 12, 31))
        for side in report.sides:
            with self.subTest(side=side.side):
                self.assertEqual(
                    sum((group.tax_amount for group in side.groups), ZERO),
                    side.tax_amount,
                )


class MoneyRegisterTests(TestCase):
    """RPT-013: the running balance has to start from what was already there."""

    @classmethod
    def setUpTestData(cls):
        from apps.ledger.models import (
            Account,
            AccountSubtype,
            AccountType,
            NormalBalance,
        )
        from apps.payments.models import MoneyAccount

        cls.currency = Currency.objects.create(
            code="R8C", name="Register Currency", symbol="C", is_active=True
        )
        year = FiscalYear.objects.create(
            code="R8-FY96", start_date=date(2096, 1, 1), end_date=date(2096, 12, 31)
        )
        cls.period = FiscalPeriod.objects.create(
            fiscal_year=year,
            period_no=1,
            name="R8 2096",
            start_date=date(2096, 1, 1),
            end_date=date(2096, 12, 31),
        )
        cls.gl = Account.objects.create(
            code="R8-1100",
            name="Register bank",
            account_type=AccountType.ASSET,
            subtype=AccountSubtype.CURRENT_ASSET,
            normal_balance=NormalBalance.DEBIT,
        )
        cls.other = Account.objects.create(
            code="R8-4100",
            name="Register income",
            account_type=AccountType.INCOME,
            subtype=AccountSubtype.REVENUE,
            normal_balance=NormalBalance.CREDIT,
        )
        cls.account = MoneyAccount.objects.create(
            code="R8-BANK",
            name="Register bank",
            currency=cls.currency,
            gl_account=cls.gl,
        )
        cls._no = 0
        # One movement before the window, two inside it.
        cls._movement(date(2096, 1, 10), Decimal("500"))
        cls._movement(date(2096, 6, 5), Decimal("200"))
        cls._movement(date(2096, 6, 20), Decimal("-50"))

    @classmethod
    def _movement(cls, when, amount):
        from apps.ledger.models import JournalEntry, JournalLine, JournalType

        cls._no += 1
        entry = JournalEntry.objects.create(
            number=f"R8-JE-{cls._no:03d}",
            entry_date=when,
            fiscal_period=cls.period,
            journal_type=JournalType.CASH,
            narration="Register movement",
            currency=cls.currency,
            exchange_rate=Decimal("1"),
            total_debit_base=abs(amount),
            total_credit_base=abs(amount),
        )
        JournalLine.objects.create(
            entry=entry,
            line_no=1,
            account=cls.gl,
            money_account=cls.account,
            currency=cls.currency,
            exchange_rate=Decimal("1"),
            debit_base=amount if amount > 0 else ZERO,
            debit_txn=amount if amount > 0 else ZERO,
            credit_base=-amount if amount < 0 else ZERO,
            credit_txn=-amount if amount < 0 else ZERO,
        )
        JournalLine.objects.create(
            entry=entry,
            line_no=2,
            account=cls.other,
            currency=cls.currency,
            exchange_rate=Decimal("1"),
            credit_base=amount if amount > 0 else ZERO,
            credit_txn=amount if amount > 0 else ZERO,
            debit_base=-amount if amount < 0 else ZERO,
            debit_txn=-amount if amount < 0 else ZERO,
        )
        return entry

    def register(self):
        return registers.money_register(
            money_account_id=self.account.pk,
            date_from=date(2096, 6, 1),
            date_to=date(2096, 6, 30),
        )

    def test_the_opening_balance_carries_what_happened_before_the_window(self):
        """A register that starts at zero when the account did not reads as missing money."""
        self.assertEqual(self.register().opening_balance, Decimal("500.0000"))

    def test_only_movements_inside_the_window_are_listed(self):
        register = self.register()
        self.assertEqual(len(register.entries), 2)
        self.assertEqual(register.total_in, Decimal("200.0000"))
        self.assertEqual(register.total_out, Decimal("50.0000"))

    def test_the_running_balance_ends_where_the_closing_balance_says(self):
        register = self.register()
        self.assertEqual(register.entries[-1].balance, register.closing_balance)
        self.assertEqual(register.closing_balance, Decimal("650.0000"))

    def test_closing_is_opening_plus_the_movements(self):
        register = self.register()
        self.assertEqual(
            register.closing_balance,
            register.opening_balance + register.total_in - register.total_out,
        )
