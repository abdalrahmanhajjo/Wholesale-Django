"""Allocation engine and workspace tests (PAY-003..PAY-007, BR-008).

The engine owns every settlement invariant, so most of this file exercises it
directly. The last two classes drive the same paths through the HTTP workspace,
because a rule enforced in the service but bypassed by the view is not enforced.
"""

from datetime import date
from decimal import Decimal
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse

from apps.core.models import (
    Currency,
    DocumentSequence,
    DocumentStatus,
    DocumentType,
    FiscalPeriod,
    FiscalYear,
)
from apps.ledger.models import (
    Account,
    AccountMapping,
    AccountSubtype,
    AccountType,
    JournalEntry,
    JournalLine,
    JournalType,
    MappingKey,
    NormalBalance,
)
from apps.parties.models import Customer, Vendor
from apps.payments.allocation import (
    AllocationLineInput,
    allocate_payment,
    allocate_sales_credit,
    allocate_vendor_credit,
    available_payment_targets,
)
from apps.payments.models import (
    Allocation,
    MoneyAccount,
    PaymentDirection,
    PaymentMethod,
)
from apps.payments.services import create_payment, post_payment
from apps.purchases.models import PurchaseBill, VendorDebitNote
from apps.sales.models import SalesCreditNote, SalesInvoice

RATE = Decimal("1.25")
TODAY = date(2091, 9, 10)


class AllocationFixtureMixin:
    """Everything the engine needs: mappings, a posted advance, open documents."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            id=920_001,
            username="allocation-member4",
            email="allocation-member4@example.com",
        )
        cls.currency = Currency.objects.create(
            code="A4T", name="Allocation Test Currency", symbol="A", is_active=True
        )
        cls.customer = Customer.objects.create(
            code="A4-CUST", name="Allocation Customer", currency=cls.currency
        )
        cls.vendor = Vendor.objects.create(
            code="A4-VEND", name="Allocation Vendor", currency=cls.currency
        )
        cls.other_customer = Customer.objects.create(
            code="A4-CUST2", name="Someone Else", currency=cls.currency
        )
        cls.other_currency = Currency.objects.create(
            code="A4X", name="Other Currency", symbol="X", is_active=True
        )

        cls.cash_account = cls._account("A4-CASH", AccountType.ASSET, NormalBalance.DEBIT)
        cls.money_account = MoneyAccount.objects.create(
            code="A4-BANK",
            name="Allocation test bank",
            currency=cls.currency,
            gl_account=cls.cash_account,
        )
        cls.method = PaymentMethod.objects.create(
            code="A4-WIRE",
            name="Wire transfer",
            requires_reference=False,
            default_money_account=cls.money_account,
        )

        # The engine reclassifies advance -> control account, so all four must map.
        cls._map(
            MappingKey.CUSTOMER_ADVANCE, "A4-CADV", AccountType.LIABILITY, NormalBalance.CREDIT
        )
        cls._map(MappingKey.VENDOR_ADVANCE, "A4-VADV", AccountType.ASSET, NormalBalance.DEBIT)
        cls._map(
            MappingKey.ACCOUNTS_RECEIVABLE, "A4-AR", AccountType.ASSET, NormalBalance.DEBIT
        )
        cls._map(
            MappingKey.ACCOUNTS_PAYABLE, "A4-AP", AccountType.LIABILITY, NormalBalance.CREDIT
        )

        year = FiscalYear.objects.create(
            code="A4-FY91", start_date=date(2091, 1, 1), end_date=date(2091, 12, 31)
        )
        cls.period = FiscalPeriod.objects.create(
            fiscal_year=year,
            period_no=9,
            name="Allocation September",
            start_date=date(2091, 9, 1),
            end_date=date(2091, 9, 30),
        )
        for doc_type, prefix in (
            (DocumentType.CUSTOMER_RECEIPT, "ARC-"),
            (DocumentType.VENDOR_PAYMENT, "APV-"),
            (DocumentType.JOURNAL_ENTRY, "AJE-"),
        ):
            DocumentSequence.objects.update_or_create(
                document_type=doc_type,
                series="DEFAULT",
                defaults={
                    "prefix": prefix,
                    "padding": 4,
                    "next_number": 1,
                    "period_key": "",
                },
            )

    # ---------------------------------------------------------------- helpers

    #: account_subtype_matches_type only accepts a subtype from its own family.
    SUBTYPE_FOR_TYPE = {
        AccountType.ASSET: AccountSubtype.CURRENT_ASSET,
        AccountType.LIABILITY: AccountSubtype.CURRENT_LIABILITY,
        AccountType.INCOME: AccountSubtype.OTHER_INCOME,
        AccountType.EXPENSE: AccountSubtype.OTHER_EXPENSE,
    }

    @classmethod
    def _account(cls, code, account_type, normal_balance):
        subtype = cls.SUBTYPE_FOR_TYPE[account_type]
        return Account.objects.create(
            code=code,
            name=f"{code} account",
            account_type=account_type,
            subtype=subtype,
            normal_balance=normal_balance,
            is_postable=True,
            is_active=True,
        )

    @classmethod
    def _map(cls, key, code, account_type, normal_balance):
        account = cls._account(code, account_type, normal_balance)
        AccountMapping.objects.update_or_create(key=key, defaults={"account": account})
        return account

    @classmethod
    def _journal(cls, number):
        """A minimal balanced entry, so documents look genuinely posted."""
        entry = JournalEntry.objects.create(
            number=number,
            entry_date=date(2091, 9, 1),
            fiscal_period=cls.period,
            journal_type=JournalType.SALES,
            currency=cls.currency,
            exchange_rate=RATE,
            total_debit_base=Decimal("1.0000"),
            total_credit_base=Decimal("1.0000"),
        )
        JournalLine.objects.create(
            entry=entry,
            line_no=1,
            account=cls.cash_account,
            currency=cls.currency,
            exchange_rate=RATE,
            debit_base=Decimal("1.0000"),
            debit_txn=Decimal("0.8000"),
        )
        JournalLine.objects.create(
            entry=entry,
            line_no=2,
            account=cls.cash_account,
            currency=cls.currency,
            exchange_rate=RATE,
            credit_base=Decimal("1.0000"),
            credit_txn=Decimal("0.8000"),
        )
        return entry

    def make_invoice(self, number, total, *, customer=None, currency=None, rate=RATE, **extra):
        total = Decimal(total)
        fields = {
            "number": number,
            "document_date": date(2091, 9, 1),
            "posting_date": date(2091, 9, 1),
            "due_date": date(2091, 9, 30),
            "customer": customer or self.customer,
            "currency": currency or self.currency,
            "exchange_rate": rate,
            "total_txn": total,
            "open_txn": total,
            "open_base": (total * rate).quantize(Decimal("0.0001")),
            "status": DocumentStatus.POSTED,
            "journal_entry": self._journal(f"J-{number}"),
        }
        fields.update(extra)
        return SalesInvoice.objects.create(**fields)

    def make_bill(self, number, total, *, vendor=None, rate=RATE, **extra):
        total = Decimal(total)
        fields = {
            "number": number,
            "document_date": date(2091, 9, 1),
            "posting_date": date(2091, 9, 1),
            "due_date": date(2091, 9, 30),
            "vendor": vendor or self.vendor,
            "vendor_invoice_number": f"V-{number}",
            "currency": self.currency,
            "exchange_rate": rate,
            "total_txn": total,
            "open_txn": total,
            "open_base": (total * rate).quantize(Decimal("0.0001")),
            "status": DocumentStatus.POSTED,
            "journal_entry": self._journal(f"J-{number}"),
        }
        fields.update(extra)
        return PurchaseBill.objects.create(**fields)

    def make_payment(self, amount, *, direction=PaymentDirection.RECEIPT, post=True):
        payment = create_payment(
            user=self.user,
            direction=direction,
            payment_date=date(2091, 9, 5),
            posting_date=date(2091, 9, 5),
            customer=self.customer if direction == PaymentDirection.RECEIPT else None,
            vendor=self.vendor if direction == PaymentDirection.PAYMENT else None,
            currency=self.currency,
            exchange_rate=RATE,
            amount_txn=Decimal(amount),
            method=self.method,
            money_account=self.money_account,
            reference="ALLOC-TEST",
            narration="Advance awaiting allocation",
        )
        if post:
            post_payment(payment, user=self.user)
            payment.refresh_from_db()
        return payment

    def allocate(self, payment, pairs, *, when=TODAY, key=None):
        return allocate_payment(
            payment,
            lines=[
                AllocationLineInput(target_id=t.pk, amount_txn=Decimal(a)) for t, a in pairs
            ],
            allocation_date=when,
            user=self.user,
            batch_key=key or uuid4(),
        )


# ---------------------------------------------------------------------------
# Partial allocation, multi-document allocation, and advances
# ---------------------------------------------------------------------------


class PaymentAllocationTests(AllocationFixtureMixin, TestCase):
    def test_partial_allocation_leaves_both_sides_partial(self):
        invoice = self.make_invoice("INV-P1", "100")
        payment = self.make_payment("80")

        result = self.allocate(payment, [(invoice, "30")])

        invoice.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual(invoice.allocated_txn, Decimal("30.0000"))
        self.assertEqual(invoice.open_txn, Decimal("70.0000"))
        self.assertEqual(invoice.status, DocumentStatus.PARTIAL)
        self.assertEqual(payment.allocated_txn, Decimal("30.0000"))
        self.assertEqual(payment.unallocated_txn, Decimal("50.0000"))
        self.assertEqual(payment.status, DocumentStatus.PARTIAL)
        self.assertEqual(result.remaining_txn, Decimal("50.0000"))
        self.assertTrue(result.created)

    def test_settling_an_invoice_completes_it(self):
        invoice = self.make_invoice("INV-P2", "100")
        payment = self.make_payment("100")

        self.allocate(payment, [(invoice, "100")])

        invoice.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual(invoice.open_txn, Decimal("0.0000"))
        self.assertEqual(invoice.status, DocumentStatus.COMPLETED)
        self.assertEqual(payment.unallocated_txn, Decimal("0.0000"))
        self.assertEqual(payment.status, DocumentStatus.COMPLETED)

    def test_one_payment_settles_many_invoices_in_one_batch(self):
        first = self.make_invoice("INV-M1", "40")
        second = self.make_invoice("INV-M2", "60")
        third = self.make_invoice("INV-M3", "25")
        payment = self.make_payment("120")

        result = self.allocate(payment, [(first, "40"), (second, "60"), (third, "20")])

        self.assertEqual(len(result.allocations), 3)
        for invoice, expected_open, expected_status in (
            (first, "0.0000", DocumentStatus.COMPLETED),
            (second, "0.0000", DocumentStatus.COMPLETED),
            (third, "5.0000", DocumentStatus.PARTIAL),
        ):
            invoice.refresh_from_db()
            self.assertEqual(invoice.open_txn, Decimal(expected_open), invoice.number)
            self.assertEqual(invoice.status, expected_status, invoice.number)
        payment.refresh_from_db()
        self.assertEqual(payment.unallocated_txn, Decimal("0.0000"))

    def test_advance_draws_down_across_separate_batches(self):
        first = self.make_invoice("INV-S1", "50")
        second = self.make_invoice("INV-S2", "50")
        payment = self.make_payment("90")

        self.allocate(payment, [(first, "50")])
        payment.refresh_from_db()
        self.assertEqual(payment.unallocated_txn, Decimal("40.0000"))

        self.allocate(payment, [(second, "40")])
        payment.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(payment.unallocated_txn, Decimal("0.0000"))
        self.assertEqual(payment.status, DocumentStatus.COMPLETED)
        self.assertEqual(second.open_txn, Decimal("10.0000"))

    def test_unallocated_payment_is_a_pure_advance_until_applied(self):
        payment = self.make_payment("75")
        self.assertEqual(payment.allocated_txn, Decimal("0.0000"))
        self.assertEqual(payment.unallocated_txn, Decimal("75.0000"))
        self.assertEqual(payment.status, DocumentStatus.POSTED)

    def test_vendor_payment_settles_a_purchase_bill(self):
        bill = self.make_bill("BILL-1", "200")
        payment = self.make_payment("150", direction=PaymentDirection.PAYMENT)

        self.allocate(payment, [(bill, "150")])

        bill.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual(bill.allocated_txn, Decimal("150.0000"))
        self.assertEqual(bill.open_txn, Decimal("50.0000"))
        self.assertEqual(bill.status, DocumentStatus.PARTIAL)
        self.assertEqual(payment.status, DocumentStatus.COMPLETED)

    def test_open_base_is_recomputed_from_the_stored_rate(self):
        invoice = self.make_invoice("INV-B1", "100")
        payment = self.make_payment("100")

        self.allocate(payment, [(invoice, "40")])

        invoice.refresh_from_db()
        self.assertEqual(invoice.open_txn, Decimal("60.0000"))
        self.assertEqual(invoice.open_base, Decimal("75.0000"))  # 60 * 1.25


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class AllocationIdempotencyTests(AllocationFixtureMixin, TestCase):
    def test_replaying_a_batch_key_does_not_allocate_twice(self):
        invoice = self.make_invoice("INV-R1", "100")
        payment = self.make_payment("100")
        key = uuid4()

        first = self.allocate(payment, [(invoice, "40")], key=key)
        second = self.allocate(payment, [(invoice, "40")], key=key)

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(Allocation.objects.filter(batch_key=key).count(), 1)
        invoice.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual(invoice.allocated_txn, Decimal("40.0000"))
        self.assertEqual(payment.allocated_txn, Decimal("40.0000"))

    def test_same_key_with_different_amounts_is_refused(self):
        invoice = self.make_invoice("INV-R2", "100")
        payment = self.make_payment("100")
        key = uuid4()
        self.allocate(payment, [(invoice, "40")], key=key)

        with self.assertRaises(ValidationError) as ctx:
            self.allocate(payment, [(invoice, "50")], key=key)
        self.assertIn("already submitted with different values", str(ctx.exception))

    def test_same_key_on_a_different_payment_is_refused(self):
        invoice = self.make_invoice("INV-R3", "100")
        first = self.make_payment("50")
        second = self.make_payment("50")
        key = uuid4()
        self.allocate(first, [(invoice, "20")], key=key)

        with self.assertRaises(ValidationError):
            self.allocate(second, [(invoice, "20")], key=key)

    def test_the_same_advance_may_settle_one_invoice_again_in_a_new_batch(self):
        invoice = self.make_invoice("INV-R4", "100")
        payment = self.make_payment("100")

        self.allocate(payment, [(invoice, "30")])
        self.allocate(payment, [(invoice, "45")])

        invoice.refresh_from_db()
        self.assertEqual(invoice.allocated_txn, Decimal("75.0000"))
        self.assertEqual(invoice.status, DocumentStatus.PARTIAL)


# ---------------------------------------------------------------------------
# Server-side accounting controls
# ---------------------------------------------------------------------------


class AllocationGuardTests(AllocationFixtureMixin, TestCase):
    def test_cannot_allocate_more_than_the_payment_has_left(self):
        invoice = self.make_invoice("INV-G1", "500")
        payment = self.make_payment("100")
        with self.assertRaises(ValidationError) as ctx:
            self.allocate(payment, [(invoice, "150")])
        self.assertIn("only", str(ctx.exception))

    def test_cannot_allocate_more_than_an_invoice_has_open(self):
        invoice = self.make_invoice("INV-G2", "40")
        payment = self.make_payment("200")
        with self.assertRaises(ValidationError):
            self.allocate(payment, [(invoice, "60")])

    def test_batch_total_is_checked_not_just_each_line(self):
        first = self.make_invoice("INV-G3", "100")
        second = self.make_invoice("INV-G4", "100")
        payment = self.make_payment("100")
        with self.assertRaises(ValidationError):
            self.allocate(payment, [(first, "60"), (second, "60")])
        self.assertEqual(Allocation.objects.count(), 0)

    def test_another_customers_invoice_is_refused(self):
        invoice = self.make_invoice("INV-G5", "100", customer=self.other_customer)
        payment = self.make_payment("100")
        with self.assertRaises(ValidationError) as ctx:
            self.allocate(payment, [(invoice, "10")])
        self.assertIn("different party", str(ctx.exception))

    def test_a_different_currency_is_refused(self):
        invoice = self.make_invoice("INV-G6", "100", currency=self.other_currency)
        payment = self.make_payment("100")
        with self.assertRaises(ValidationError) as ctx:
            self.allocate(payment, [(invoice, "10")])
        self.assertIn("different currency", str(ctx.exception))

    def test_a_different_stored_rate_is_allowed_and_realises_fx(self):
        """Superseded by FX settlement: a moved rate settles, it does not refuse."""
        invoice = self.make_invoice("INV-G7", "100", rate=Decimal("1.30"))
        payment = self.make_payment("100")
        result = self.allocate(payment, [(invoice, "10")])
        invoice.refresh_from_db()
        self.assertEqual(invoice.allocated_txn, Decimal("10.0000"))
        # 10 at 1.25 is worth 12.50; the invoice booked it at 1.30, so 13.00.
        self.assertEqual(result.allocations[0].fx_gain_loss_base, Decimal("-0.5000"))

    def test_an_unposted_payment_cannot_be_allocated(self):
        invoice = self.make_invoice("INV-G8", "100")
        payment = self.make_payment("100", post=False)
        with self.assertRaises(ValidationError):
            self.allocate(payment, [(invoice, "10")])

    def test_a_draft_invoice_cannot_be_settled(self):
        invoice = self.make_invoice("INV-G9", "100", status=DocumentStatus.DRAFT)
        payment = self.make_payment("100")
        with self.assertRaises(ValidationError) as ctx:
            self.allocate(payment, [(invoice, "10")])
        self.assertIn("no longer an open posted document", str(ctx.exception))

    def test_a_reversed_invoice_cannot_be_settled(self):
        invoice = self.make_invoice("INV-G10", "100", is_reversed=True)
        payment = self.make_payment("100")
        with self.assertRaises(ValidationError):
            self.allocate(payment, [(invoice, "10")])

    def test_a_posted_invoice_cannot_exist_without_a_journal(self):
        """The engine also guards this, but the row shape is unreachable.

        ``si_posted_has_journal`` means a posted invoice always carries its
        journal, so the engine's ``journal_entry_id is None`` check is a second
        line of defence rather than the only one. Assert the first line here.
        """
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_invoice("INV-G11", "100", journal_entry=None)

    def test_allocation_date_cannot_precede_the_payment(self):
        invoice = self.make_invoice("INV-G12", "100")
        payment = self.make_payment("100")
        with self.assertRaises(ValidationError) as ctx:
            self.allocate(payment, [(invoice, "10")], when=date(2091, 9, 1))
        self.assertIn("earlier than the payment date", str(ctx.exception))

    def test_allocation_date_cannot_precede_the_invoice(self):
        invoice = self.make_invoice("INV-G13", "100", document_date=date(2091, 9, 20))
        payment = self.make_payment("100")
        with self.assertRaises(ValidationError) as ctx:
            self.allocate(payment, [(invoice, "10")], when=date(2091, 9, 15))
        self.assertIn("document date", str(ctx.exception))

    def test_the_same_invoice_twice_in_one_batch_is_refused(self):
        invoice = self.make_invoice("INV-G14", "100")
        payment = self.make_payment("100")
        with self.assertRaises(ValidationError) as ctx:
            self.allocate(payment, [(invoice, "10"), (invoice, "10")])
        self.assertIn("only once", str(ctx.exception))

    def test_zero_and_negative_amounts_are_refused(self):
        invoice = self.make_invoice("INV-G15", "100")
        payment = self.make_payment("100")
        for bad in ("0", "-5"):
            with self.assertRaises(ValidationError):
                self.allocate(payment, [(invoice, bad)])

    def test_sub_quantum_precision_is_refused_rather_than_rounded(self):
        invoice = self.make_invoice("INV-G16", "100")
        payment = self.make_payment("100")
        with self.assertRaises(ValidationError) as ctx:
            self.allocate(payment, [(invoice, "10.00005")])
        self.assertIn("four decimal places", str(ctx.exception))

    def test_an_empty_batch_is_refused(self):
        payment = self.make_payment("100")
        with self.assertRaises(ValidationError):
            self.allocate(payment, [])

    def test_a_failed_batch_writes_nothing(self):
        good = self.make_invoice("INV-G17", "100")
        bad = self.make_invoice("INV-G18", "100", customer=self.other_customer)
        payment = self.make_payment("200")
        with self.assertRaises(ValidationError):
            self.allocate(payment, [(good, "50"), (bad, "50")])
        good.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual(good.allocated_txn, Decimal("0.0000"))
        self.assertEqual(payment.allocated_txn, Decimal("0.0000"))
        self.assertEqual(Allocation.objects.count(), 0)


# ---------------------------------------------------------------------------
# The accounting behind an allocation
# ---------------------------------------------------------------------------


class AllocationJournalTests(AllocationFixtureMixin, TestCase):
    def test_allocation_reclassifies_the_advance_without_touching_cash(self):
        invoice = self.make_invoice("INV-J1", "100")
        payment = self.make_payment("100")
        posting_journal = payment.journal_entry_id

        result = self.allocate(payment, [(invoice, "40")])

        entry = result.allocations[0].journal_entry
        self.assertIsNotNone(entry)
        self.assertNotEqual(entry.pk, posting_journal)
        self.assertEqual(entry.source_doc_type, "ALOC")
        self.assertEqual(entry.total_debit_base, entry.total_credit_base)

        accounts = {line.account.code: line for line in entry.lines.select_related("account")}
        self.assertIn("A4-CADV", accounts)  # customer advance, debited
        self.assertIn("A4-AR", accounts)  # receivable, credited
        self.assertNotIn("A4-CASH", accounts, "allocation must not move cash again")
        self.assertEqual(accounts["A4-CADV"].debit_txn, Decimal("40.0000"))
        self.assertEqual(accounts["A4-AR"].credit_txn, Decimal("40.0000"))
        self.assertEqual(accounts["A4-CADV"].debit_base, Decimal("50.0000"))  # 40 * 1.25

    def test_vendor_allocation_debits_payables(self):
        bill = self.make_bill("BILL-J1", "100")
        payment = self.make_payment("100", direction=PaymentDirection.PAYMENT)

        result = self.allocate(payment, [(bill, "60")])

        entry = result.allocations[0].journal_entry
        accounts = {line.account.code: line for line in entry.lines.select_related("account")}
        self.assertEqual(accounts["A4-AP"].debit_txn, Decimal("60.0000"))
        self.assertEqual(accounts["A4-VADV"].credit_txn, Decimal("60.0000"))
        self.assertNotIn("A4-CASH", accounts)

    def test_one_batch_posts_exactly_one_journal(self):
        first = self.make_invoice("INV-J2", "50")
        second = self.make_invoice("INV-J3", "50")
        payment = self.make_payment("100")

        result = self.allocate(payment, [(first, "50"), (second, "50")])

        entries = {item.journal_entry_id for item in result.allocations}
        self.assertEqual(len(entries), 1, "a batch is one accounting event")
        entry = result.allocations[0].journal_entry
        self.assertEqual(entry.total_debit_base, Decimal("125.0000"))  # 100 * 1.25

    def test_allocation_rows_record_amounts_and_rate(self):
        invoice = self.make_invoice("INV-J4", "100")
        payment = self.make_payment("100")

        row = self.allocate(payment, [(invoice, "40")]).allocations[0]

        self.assertEqual(row.source_amount_txn, Decimal("40.0000"))
        self.assertEqual(row.target_amount_txn, Decimal("40.0000"))
        self.assertEqual(row.amount_base, Decimal("50.0000"))
        self.assertEqual(row.settlement_rate, RATE)
        self.assertEqual(row.source_type, "PAYMENT")
        self.assertEqual(row.target_type, "SALES_INVOICE")
        self.assertEqual(row.source, payment)
        self.assertEqual(row.target, invoice)


# ---------------------------------------------------------------------------
# Credit notes settle without cash
# ---------------------------------------------------------------------------


class CreditAllocationTests(AllocationFixtureMixin, TestCase):
    def _credit_note(self, number, total):
        total = Decimal(total)
        return SalesCreditNote.objects.create(
            number=number,
            document_date=date(2091, 9, 1),
            posting_date=date(2091, 9, 1),
            customer=self.customer,
            currency=self.currency,
            exchange_rate=RATE,
            reason="Goods returned",
            total_txn=total,
            open_txn=total,
            open_base=(total * RATE).quantize(Decimal("0.0001")),
            status=DocumentStatus.POSTED,
            journal_entry=self._journal(f"J-{number}"),
        )

    def _debit_note(self, number, total):
        total = Decimal(total)
        return VendorDebitNote.objects.create(
            number=number,
            document_date=date(2091, 9, 1),
            posting_date=date(2091, 9, 1),
            vendor=self.vendor,
            currency=self.currency,
            exchange_rate=RATE,
            total_txn=total,
            open_txn=total,
            open_base=(total * RATE).quantize(Decimal("0.0001")),
            status=DocumentStatus.POSTED,
            journal_entry=self._journal(f"J-{number}"),
        )

    def test_customer_credit_settles_an_invoice_with_no_cash_journal(self):
        invoice = self.make_invoice("INV-C1", "100")
        note = self._credit_note("CN-1", "60")

        result = allocate_sales_credit(
            note,
            lines=[AllocationLineInput(target_id=invoice.pk, amount_txn=Decimal("60"))],
            allocation_date=TODAY,
            user=self.user,
            batch_key=uuid4(),
        )

        invoice.refresh_from_db()
        note.refresh_from_db()
        self.assertEqual(invoice.credited_txn, Decimal("60.0000"))
        self.assertEqual(invoice.allocated_txn, Decimal("0.0000"))
        self.assertEqual(invoice.open_txn, Decimal("40.0000"))
        self.assertEqual(invoice.status, DocumentStatus.PARTIAL)
        self.assertEqual(note.open_txn, Decimal("0.0000"))
        self.assertEqual(note.status, DocumentStatus.COMPLETED)
        self.assertIsNone(
            result.allocations[0].journal_entry_id,
            "applying an existing credit moves no money",
        )

    def test_credit_and_payment_can_settle_the_same_invoice(self):
        invoice = self.make_invoice("INV-C2", "100")
        note = self._credit_note("CN-2", "30")
        payment = self.make_payment("70")

        allocate_sales_credit(
            note,
            lines=[AllocationLineInput(target_id=invoice.pk, amount_txn=Decimal("30"))],
            allocation_date=TODAY,
            user=self.user,
            batch_key=uuid4(),
        )
        self.allocate(payment, [(invoice, "70")])

        invoice.refresh_from_db()
        self.assertEqual(invoice.credited_txn, Decimal("30.0000"))
        self.assertEqual(invoice.allocated_txn, Decimal("70.0000"))
        self.assertEqual(invoice.open_txn, Decimal("0.0000"))
        self.assertEqual(invoice.status, DocumentStatus.COMPLETED)

    def test_vendor_credit_settles_a_bill(self):
        bill = self.make_bill("BILL-C1", "100")
        note = self._debit_note("DN-1", "40")

        allocate_vendor_credit(
            note,
            lines=[AllocationLineInput(target_id=bill.pk, amount_txn=Decimal("40"))],
            allocation_date=TODAY,
            user=self.user,
            batch_key=uuid4(),
        )

        bill.refresh_from_db()
        self.assertEqual(bill.credited_txn, Decimal("40.0000"))
        self.assertEqual(bill.open_txn, Decimal("60.0000"))

    def test_a_credit_cannot_over_apply(self):
        invoice = self.make_invoice("INV-C3", "100")
        note = self._credit_note("CN-3", "20")
        with self.assertRaises(ValidationError):
            allocate_sales_credit(
                note,
                lines=[AllocationLineInput(target_id=invoice.pk, amount_txn=Decimal("50"))],
                allocation_date=TODAY,
                user=self.user,
                batch_key=uuid4(),
            )


# ---------------------------------------------------------------------------
# The database refuses to hold an inconsistent balance (BR-008)
# ---------------------------------------------------------------------------


class AllocationDatabaseGuardTests(AllocationFixtureMixin, TestCase):
    def test_trigger_rejects_a_tampered_invoice_balance(self):
        invoice = self.make_invoice("INV-D1", "100")
        payment = self.make_payment("100")
        self.allocate(payment, [(invoice, "40")])

        # The engine keeps these in step; prove the database does too.
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                SalesInvoice.objects.filter(pk=invoice.pk).update(
                    allocated_txn=Decimal("999.0000")
                )
                Allocation.objects.filter(sales_invoice=invoice).update(
                    target_amount_txn=Decimal("40.0000")
                )


# ---------------------------------------------------------------------------
# Target selection for the workspace
# ---------------------------------------------------------------------------


class AllocationTargetTests(AllocationFixtureMixin, TestCase):
    def test_only_matching_open_documents_are_offered(self):
        wanted = self.make_invoice("INV-T1", "100")
        self.make_invoice("INV-T2", "100", customer=self.other_customer)
        self.make_invoice("INV-T3", "100", currency=self.other_currency)
        # A document booked at a different rate IS offered now — it settles and
        # realises an exchange difference. Only the currency has to match.
        moved_rate = self.make_invoice("INV-T4", "100", rate=Decimal("1.30"))
        self.make_invoice("INV-T5", "100", status=DocumentStatus.DRAFT)
        self.make_invoice("INV-T6", "100", is_reversed=True)
        # Settle one for real rather than forcing the columns: the derived-open
        # check constraint and the BR-008 trigger both police this shape.
        settled = self.make_invoice("INV-T7", "100")
        self.allocate(self.make_payment("100"), [(settled, "100")])
        payment = self.make_payment("100")

        offered = list(available_payment_targets(payment))

        self.assertEqual(
            sorted(item.number for item in offered),
            sorted([wanted.number, moved_rate.number]),
        )

    def test_targets_are_ordered_by_due_date_so_the_oldest_settles_first(self):
        late = self.make_invoice("INV-T8", "10", due_date=date(2091, 12, 1))
        early = self.make_invoice("INV-T9", "10", due_date=date(2091, 10, 1))
        payment = self.make_payment("100")

        offered = list(available_payment_targets(payment))

        self.assertEqual([item.number for item in offered], [early.number, late.number])


# ---------------------------------------------------------------------------
# The workspace itself
# ---------------------------------------------------------------------------


class AllocationWorkspaceTests(AllocationFixtureMixin, TestCase):
    def setUp(self):
        self.invoice = self.make_invoice("INV-W1", "100")
        self.payment = self.make_payment("100")
        self.url = reverse("payments:payment_allocate", args=[self.payment.pk])
        self.actor = get_user_model().objects.create_user(
            username="cashier", email="cashier@example.com"
        )
        self.actor.set_password("allocation-pass")
        self.actor.save()
        self.client.force_login(self.actor)

    def _grant(self, *codenames):
        self.actor.user_permissions.add(*Permission.objects.filter(codename__in=codenames))
        self.actor = get_user_model().objects.get(pk=self.actor.pk)
        self.client.force_login(self.actor)

    def _payload(self, amount, *, key=None, when="2091-09-10"):
        return {
            "allocation_date": when,
            "batch_key": str(key or uuid4()),
            "lines-TOTAL_FORMS": "1",
            "lines-INITIAL_FORMS": "0",
            "lines-MIN_NUM_FORMS": "0",
            "lines-MAX_NUM_FORMS": "200",
            "lines-0-target_id": str(self.invoice.pk),
            "lines-0-amount_txn": amount,
        }

    def test_workspace_requires_the_allocate_permission(self):
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_permitted_user_sees_the_open_document(self):
        self._grant("allocate_payment", "view_payment")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "INV-W1")
        self.assertContains(response, self.payment.number)

    def test_posting_an_allocation_settles_and_redirects(self):
        self._grant("allocate_payment", "view_payment")
        response = self.client.post(self.url, self._payload("40"))

        self.assertEqual(response.status_code, 302)
        self.invoice.refresh_from_db()
        self.payment.refresh_from_db()
        self.assertEqual(self.invoice.allocated_txn, Decimal("40.0000"))
        self.assertEqual(self.payment.unallocated_txn, Decimal("60.0000"))

    def test_over_allocation_is_reported_not_applied(self):
        self._grant("allocate_payment", "view_payment")
        response = self.client.post(self.url, self._payload("500"))

        self.assertEqual(response.status_code, 200)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.allocated_txn, Decimal("0.0000"))
        self.assertEqual(Allocation.objects.count(), 0)

    def test_an_empty_batch_is_reported_not_applied(self):
        self._grant("allocate_payment", "view_payment")
        response = self.client.post(self.url, self._payload(""))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Allocation.objects.count(), 0)

    def test_double_submit_of_one_batch_key_allocates_once(self):
        self._grant("allocate_payment", "view_payment")
        key = uuid4()
        self.client.post(self.url, self._payload("40", key=key))
        self.client.post(self.url, self._payload("40", key=key))

        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.allocated_txn, Decimal("40.0000"))
        self.assertEqual(Allocation.objects.filter(batch_key=key).count(), 1)
