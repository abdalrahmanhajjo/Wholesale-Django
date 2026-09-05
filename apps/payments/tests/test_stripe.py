"""Stripe settlement and the processor-fee posting it depends on (PAY-013).

Split deliberately. The pure-arithmetic cases run as SimpleTestCase because the
money conversions and the fee-currency rules are where a silent error costs
real money, and they should stay checkable without a database in front of them.
"""

from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.core.models import (
    Currency,
    DocumentSequence,
    DocumentStatus,
    DocumentType,
    FiscalPeriod,
    FiscalYear,
    PeriodStatus,
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
from apps.payments import stripe_gateway, stripe_service
from apps.payments.models import (
    MoneyAccount,
    Payment,
    PaymentDirection,
    PaymentMethod,
    StripeCheckout,
    StripeCheckoutStatus,
)
from apps.payments.posting import build_payment_journal, payment_required_mappings
from apps.payments.services import create_payment
from apps.sales.models import SalesInvoice


# ---------------------------------------------------------------------------
# Money conversion, without a database
# ---------------------------------------------------------------------------
class MinorUnitTests(SimpleTestCase):
    def test_two_decimal_currency_round_trips(self):
        self.assertEqual(stripe_gateway.to_minor(Decimal("10.00"), decimal_places=2), 1000)
        self.assertEqual(stripe_gateway.from_minor(1000, decimal_places=2), Decimal("10.00"))

    def test_zero_decimal_currency_is_not_multiplied(self):
        """Yen is quoted in whole units; Stripe wants 1000 for ¥1000, not 100000."""
        self.assertEqual(stripe_gateway.to_minor(Decimal("1000"), decimal_places=0), 1000)
        self.assertEqual(stripe_gateway.from_minor(1000, decimal_places=0), Decimal("1000"))

    def test_half_a_cent_rounds_rather_than_disappearing(self):
        self.assertEqual(stripe_gateway.to_minor(Decimal("10.005"), decimal_places=2), 1001)


class FeeCurrencyTests(SimpleTestCase):
    """Stripe states its fee in the settlement currency, which is not always ours."""

    def test_fee_in_the_same_currency_is_taken_as_given(self):
        fee, note = stripe_gateway._fee_in_charge_currency(
            {"fee": 320, "currency": "usd"}, currency_code="USD", decimal_places=2
        )
        self.assertEqual(fee, Decimal("3.20"))
        self.assertEqual(note, "")

    def test_fee_settled_in_another_currency_is_converted_back(self):
        # Charged in USD, settled in EUR at 0.9. A EUR 3.60 fee is USD 4.00.
        fee, note = stripe_gateway._fee_in_charge_currency(
            {"fee": 360, "currency": "eur", "exchange_rate": 0.9},
            currency_code="USD",
            decimal_places=2,
        )
        self.assertEqual(fee, Decimal("4.00"))
        self.assertEqual(note, "")

    def test_unconvertible_fee_is_zero_and_says_so(self):
        """A guessed fee is worse than a missing one: it cannot be spotted later."""
        fee, note = stripe_gateway._fee_in_charge_currency(
            {"fee": 360, "currency": "eur"}, currency_code="USD", decimal_places=2
        )
        self.assertEqual(fee, Decimal("0"))
        self.assertIn("could not be converted", note)

    def test_missing_balance_transaction_is_reported_not_invented(self):
        fee, note = stripe_gateway._fee_in_charge_currency(
            {}, currency_code="USD", decimal_places=2
        )
        self.assertEqual(fee, Decimal("0"))
        self.assertIn("not recorded", note)


class WebhookConfigurationTests(SimpleTestCase):
    @override_settings(STRIPE_WEBHOOK_SECRET="", STRIPE_SECRET_KEY="sk_test_x")
    def test_no_signing_secret_refuses_rather_than_skipping_the_check(self):
        with self.assertRaises(stripe_gateway.StripeUnavailable):
            stripe_gateway.verify_webhook(b"{}", "sig")

    @override_settings(STRIPE_SECRET_KEY="")
    def test_no_api_key_is_a_clear_message_not_an_import_error(self):
        with self.assertRaises(stripe_gateway.StripeUnavailable):
            stripe_gateway.fetch_settlement("cs_test_x", decimal_places=2)


# ---------------------------------------------------------------------------
# The fee, in the ledger
# ---------------------------------------------------------------------------
class FeeFixtureMixin:
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(id=920_001, username="stripe-tests")
        cls.currency = Currency.objects.create(
            code="S1T", name="Stripe Test Currency", symbol="S", is_active=True
        )
        cls.customer = Customer.objects.create(
            code="S1-CUST", name="Stripe Customer", currency=cls.currency
        )
        cls.vendor = Vendor.objects.create(
            code="S1-VEND", name="Stripe Vendor", currency=cls.currency
        )
        cls.cash_account = cls._account("S1-CASH", "Stripe clearing", AccountType.ASSET)
        cls.fee_account = cls._account("S1-FEE", "Processor fees", AccountType.EXPENSE)
        cls.advance_account = cls._account(
            "S1-ADV", "Customer advances", AccountType.LIABILITY
        )
        cls.vendor_advance_account = cls._account(
            "S1-VADV", "Vendor advances", AccountType.ASSET
        )
        AccountMapping.objects.update_or_create(
            key=MappingKey.MERCHANT_FEE, defaults={"account": cls.fee_account}
        )
        AccountMapping.objects.update_or_create(
            key=MappingKey.CUSTOMER_ADVANCE, defaults={"account": cls.advance_account}
        )
        AccountMapping.objects.update_or_create(
            key=MappingKey.VENDOR_ADVANCE,
            defaults={"account": cls.vendor_advance_account},
        )
        cls.money_account = MoneyAccount.objects.create(
            code="S1-STRIPE",
            name="Stripe clearing",
            account_type="CARD",
            currency=cls.currency,
            gl_account=cls.cash_account,
        )
        cls.method = PaymentMethod.objects.create(
            code="S1-STRIPE",
            name="Stripe",
            requires_reference=True,
            default_money_account=cls.money_account,
        )
        year = FiscalYear.objects.create(
            code="S1-FY92", start_date=date(2092, 1, 1), end_date=date(2092, 12, 31)
        )
        cls.period = FiscalPeriod.objects.create(
            fiscal_year=year,
            period_no=3,
            name="Stripe March 2092",
            start_date=date(2092, 3, 1),
            end_date=date(2092, 3, 31),
        )
        for document_type, prefix in (
            (DocumentType.CUSTOMER_RECEIPT, "SRC-"),
            (DocumentType.VENDOR_PAYMENT, "SPV-"),
        ):
            DocumentSequence.objects.update_or_create(
                document_type=document_type,
                series="DEFAULT",
                defaults={
                    "prefix": prefix,
                    "padding": 4,
                    "next_number": 1,
                    "period_key": "",
                },
            )

    @classmethod
    def _posted_invoice(cls, number, amount):
        """A posted invoice, with the journal SAL-009 requires one to have.

        The journal is a stub, but it cannot be an empty one: a deferred trigger
        enforces GL-001 at commit, so it needs balanced lines whatever the test
        is actually about.
        """
        entry = JournalEntry.objects.create(
            number=f"JE-{number}",
            entry_date=date(2092, 3, 10),
            fiscal_period=cls.period,
            journal_type=JournalType.SALES,
            narration=f"Stub journal for {number}",
            currency=cls.currency,
            exchange_rate=Decimal("1"),
            total_debit_base=amount,
            total_credit_base=amount,
        )
        for line_no, (account, side) in enumerate(
            ((cls.cash_account, "debit"), (cls.advance_account, "credit")), start=1
        ):
            JournalLine.objects.create(
                entry=entry,
                line_no=line_no,
                account=account,
                currency=cls.currency,
                exchange_rate=Decimal("1"),
                **{f"{side}_base": amount, f"{side}_txn": amount},
            )
        return SalesInvoice.objects.create(
            number=number,
            document_date=date(2092, 3, 10),
            posting_date=date(2092, 3, 10),
            customer=cls.customer,
            currency=cls.currency,
            exchange_rate=Decimal("1"),
            total_txn=amount,
            open_txn=amount,
            status=DocumentStatus.POSTED,
            journal_entry=entry,
        )

    @classmethod
    def _account(cls, code, name, account_type):
        debit = account_type in {AccountType.ASSET, AccountType.EXPENSE}
        subtypes = {
            AccountType.ASSET: AccountSubtype.CURRENT_ASSET,
            AccountType.EXPENSE: AccountSubtype.OPERATING_EXPENSE,
            AccountType.LIABILITY: AccountSubtype.CURRENT_LIABILITY,
        }
        return Account.objects.create(
            code=code,
            name=name,
            account_type=account_type,
            subtype=subtypes[account_type],
            normal_balance=NormalBalance.DEBIT if debit else NormalBalance.CREDIT,
        )

    def receipt(self, **overrides):
        values = {
            "direction": PaymentDirection.RECEIPT,
            "payment_date": date(2092, 3, 10),
            "posting_date": date(2092, 3, 10),
            "customer": self.customer,
            "vendor": None,
            "currency": self.currency,
            "exchange_rate": Decimal("1"),
            "amount_txn": Decimal("1000"),
            "fee_txn": Decimal("29.30"),
            "method": self.method,
            "money_account": self.money_account,
            "reference": "pi_test_123",
            "narration": "Stripe receipt",
        }
        values.update(overrides)
        return create_payment(user=self.user, **values)


class FeeJournalTests(FeeFixtureMixin, TestCase):
    def test_receipt_splits_cash_from_fee_and_settles_the_gross(self):
        """The customer owes 1000 and pays 1000. Only 970.70 of it arrives."""
        payment = self.receipt()
        draft = build_payment_journal(payment, user=self.user)

        by_account = {line.account.code: line for line in draft.lines}
        self.assertEqual(by_account["S1-CASH"].debit_base, Decimal("970.7000"))
        self.assertEqual(by_account["S1-FEE"].debit_base, Decimal("29.3000"))
        # What clears the customer's balance is the full amount, not the net.
        self.assertEqual(by_account["S1-ADV"].credit_base, Decimal("1000.0000"))

    def test_receipt_journal_balances(self):
        draft = build_payment_journal(self.receipt(), user=self.user)
        debit = sum(line.debit_base for line in draft.lines)
        credit = sum(line.credit_base for line in draft.lines)
        self.assertEqual(debit, credit)

    def test_vendor_payment_adds_the_fee_on_top(self):
        """A wire charge is not deducted from the vendor; it is charged to us."""
        payment = self.receipt(
            direction=PaymentDirection.PAYMENT,
            customer=None,
            vendor=self.vendor,
            amount_txn=Decimal("500"),
            fee_txn=Decimal("15"),
        )
        draft = build_payment_journal(payment, user=self.user)
        by_account = {line.account.code: line for line in draft.lines}

        self.assertEqual(by_account["S1-VADV"].debit_base, Decimal("500.0000"))
        self.assertEqual(by_account["S1-FEE"].debit_base, Decimal("15.0000"))
        # More money left the bank than the vendor received.
        self.assertEqual(by_account["S1-CASH"].credit_base, Decimal("515.0000"))

    def test_no_fee_produces_the_original_two_line_journal(self):
        draft = build_payment_journal(self.receipt(fee_txn=Decimal("0")), user=self.user)
        self.assertEqual(len(draft.lines), 2)
        self.assertNotIn("S1-FEE", {line.account.code for line in draft.lines})

    def test_fee_mapping_is_only_required_when_there_is_a_fee(self):
        """The engine rejects a required mapping the journal never uses."""
        self.assertNotIn(
            MappingKey.MERCHANT_FEE,
            payment_required_mappings(self.receipt(fee_txn=Decimal("0"))),
        )
        self.assertIn(MappingKey.MERCHANT_FEE, payment_required_mappings(self.receipt()))

    def test_fee_base_is_converted_at_the_payments_own_rate(self):
        payment = self.receipt(exchange_rate=Decimal("1.25"))
        self.assertEqual(payment.amount_base, Decimal("1250.0000"))
        self.assertEqual(payment.fee_base, Decimal("36.6250"))

    def test_foreign_currency_journal_still_balances_exactly(self):
        """The net is derived by subtraction, never by re-converting the net.

        Converting 970.70 at 1.25 separately can land a quantum away from
        1250.0000 - 36.6250, and a journal that is out by 0.0001 does not post.
        """
        draft = build_payment_journal(
            self.receipt(exchange_rate=Decimal("1.25")), user=self.user
        )
        self.assertEqual(
            sum(line.debit_base for line in draft.lines),
            sum(line.credit_base for line in draft.lines),
        )

    def test_a_fee_that_swallows_the_receipt_is_refused(self):
        with self.assertRaises(ValidationError):
            self.receipt(amount_txn=Decimal("25"), fee_txn=Decimal("25"))


# ---------------------------------------------------------------------------
# The webhook
# ---------------------------------------------------------------------------
class StripeWebhookViewTests(TestCase):
    url = "/payments/stripe/webhook/"

    def test_the_endpoint_is_reachable_without_logging_in(self):
        """It has to be: Stripe has no session. The signature is the auth."""
        response = self.client.post(self.url, data=b"{}", content_type="application/json")
        self.assertNotIn(response.status_code, (302, 403))

    @override_settings(STRIPE_SECRET_KEY="sk_test_x", STRIPE_WEBHOOK_SECRET="whsec_x")
    def test_an_unsigned_body_is_rejected(self):
        response = self.client.post(
            self.url,
            data=b'{"type":"checkout.session.completed"}',
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    @override_settings(STRIPE_SECRET_KEY="sk_test_x", STRIPE_WEBHOOK_SECRET="")
    def test_without_a_signing_secret_nothing_is_accepted(self):
        response = self.client.post(self.url, data=b"{}", content_type="application/json")
        self.assertEqual(response.status_code, 503)

    def test_get_is_not_allowed(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)

    @override_settings(STRIPE_SECRET_KEY="sk_test_x", STRIPE_WEBHOOK_SECRET="whsec_x")
    def test_an_event_type_we_do_not_handle_is_acknowledged(self):
        """Answering 4xx to these would push the endpoint towards being disabled."""
        event = {"type": "customer.created", "data": {"object": {"id": "cus_1"}}}
        with mock.patch.object(stripe_gateway, "verify_webhook", return_value=event):
            response = self.client.post(self.url, data=b"{}", content_type="application/json")
        self.assertEqual(response.status_code, 200)

    @override_settings(STRIPE_SECRET_KEY="sk_test_x", STRIPE_WEBHOOK_SECRET="whsec_x")
    def test_a_session_we_never_created_is_acknowledged_not_retried(self):
        event = {
            "type": "checkout.session.completed",
            "data": {"object": {"id": "cs_not_ours"}},
        }
        with mock.patch.object(stripe_gateway, "verify_webhook", return_value=event):
            response = self.client.post(self.url, data=b"{}", content_type="application/json")
        self.assertEqual(response.status_code, 200)


class StripeChargeRouteTests(TestCase):
    def test_creating_a_payment_link_requires_a_login(self):
        response = self.client.post(reverse("payments:stripe_charge", args=[1]))
        self.assertIn(response.status_code, (302, 403))

    def test_retrying_a_settlement_requires_a_login(self):
        response = self.client.post(reverse("payments:stripe_settle", args=[1]))
        self.assertIn(response.status_code, (302, 403))


# ---------------------------------------------------------------------------
# Settlement, when the ledger will not cooperate
# ---------------------------------------------------------------------------
class SettlementFailureTests(FeeFixtureMixin, TestCase):
    """The money arrived. Whether the books can accept it yet is a separate question."""

    def setUp(self):
        self.invoice = self._posted_invoice("S1-INV-0001", Decimal("1000"))
        self.checkout = StripeCheckout.objects.create(
            invoice=self.invoice,
            session_id="cs_test_settlement",
            payment_intent_id="pi_test_settlement",
            currency=self.currency,
            amount_txn=Decimal("1000"),
            fee_txn=Decimal("29.30"),
            status=StripeCheckoutStatus.PAID,
            created_by=self.user,
        )

    def test_a_missing_exchange_rate_is_recorded_not_raised(self):
        """S1T is not the base currency and has no rate on file.

        Posting must stop - inventing a rate would misstate the receipt in base
        currency - but it must stop *quietly*, or Stripe retries for hours and
        then gives up on an endpoint that was working fine.
        """
        checkout = stripe_service.post_settled_checkout(self.checkout)

        self.assertIsNone(checkout.payment_id)
        self.assertIn("exchange rate", checkout.settlement_error)
        # And the fact of payment survives, which is the whole point.
        self.assertEqual(checkout.status, StripeCheckoutStatus.PAID)
        self.assertTrue(checkout.needs_attention)

    def test_the_failure_is_visible_to_whoever_has_to_fix_it(self):
        stripe_service.post_settled_checkout(self.checkout)
        self.checkout.refresh_from_db()
        self.assertTrue(self.checkout.needs_attention)
        self.assertNotEqual(self.checkout.settlement_error, "")

    def test_a_checkout_already_posted_is_left_alone(self):
        """Stripe delivers at least once, so this path runs more than once."""
        payment = self.receipt()
        self.checkout.payment = payment
        self.checkout.save(update_fields=["payment"])

        before = Payment.objects.count()
        result = stripe_service.post_settled_checkout(self.checkout)

        self.assertEqual(result.payment_id, payment.pk)
        self.assertEqual(Payment.objects.count(), before)

    def test_an_unpaid_checkout_posts_nothing(self):
        self.checkout.status = StripeCheckoutStatus.PENDING
        self.checkout.save(update_fields=["status"])

        result = stripe_service.post_settled_checkout(self.checkout)

        self.assertIsNone(result.payment_id)


class LiveLinkTests(FeeFixtureMixin, TestCase):
    def setUp(self):
        self.invoice = self._posted_invoice("S1-INV-0002", Decimal("400"))

    def _checkout(self, amount, **overrides):
        values = {
            "invoice": self.invoice,
            "session_id": f"cs_live_{amount}",
            "currency": self.currency,
            "amount_txn": amount,
            "status": StripeCheckoutStatus.PENDING,
            "created_by": self.user,
        }
        values.update(overrides)
        return StripeCheckout.objects.create(**values)

    def test_a_pending_link_counts_as_live(self):
        checkout = self._checkout(Decimal("400"))
        self.assertEqual(stripe_service.live_checkout(self.invoice), checkout)

    def test_an_expired_link_does_not(self):
        self._checkout(Decimal("400"), expires_at=timezone.now() - timedelta(hours=1))
        self.assertIsNone(stripe_service.live_checkout(self.invoice))

    def test_a_paid_link_is_not_offered_again(self):
        self._checkout(Decimal("400"), status=StripeCheckoutStatus.PAID)
        self.assertIsNone(stripe_service.live_checkout(self.invoice))

    @override_settings(STRIPE_SECRET_KEY="")
    def test_charging_with_stripe_switched_off_is_a_clear_refusal(self):
        with self.assertRaises(ValidationError):
            stripe_service.start_checkout(self.invoice, user=self.user)

    @override_settings(STRIPE_SECRET_KEY="sk_test_x")
    def test_an_unchanged_open_amount_reuses_the_existing_link(self):
        """Two payable links for one invoice is how a customer pays twice."""
        checkout = self._checkout(Decimal("400"))
        self.assertEqual(stripe_service.start_checkout(self.invoice, user=self.user), checkout)

    @override_settings(STRIPE_SECRET_KEY="sk_test_x")
    def test_a_fully_paid_invoice_cannot_be_charged(self):
        # si_open_is_derived: the open amount follows from what has been
        # settled, so it has to be closed by allocating, not by assignment.
        self.invoice.allocated_txn = self.invoice.total_txn
        self.invoice.open_txn = Decimal("0")
        self.invoice.save(update_fields=["allocated_txn", "open_txn"])
        with self.assertRaises(ValidationError):
            stripe_service.start_checkout(self.invoice, user=self.user)

    @override_settings(STRIPE_SECRET_KEY="sk_test_x")
    def test_a_draft_invoice_cannot_be_charged(self):
        self.invoice.status = DocumentStatus.DRAFT
        self.invoice.save(update_fields=["status"])
        with self.assertRaises(ValidationError):
            stripe_service.start_checkout(self.invoice, user=self.user)


# ---------------------------------------------------------------------------
# The happy path, end to end
# ---------------------------------------------------------------------------
class StripeHappyPathTests(TestCase):
    """Customer pays -> receipt posted, fee expensed, invoice settled.

    Everything else in this file exercises a branch where something goes wrong.
    This one walks the path that is supposed to happen, against the reference
    data the migrations actually seed, because that is the combination a
    deployment will meet on its first real payment and none of the failure
    tests would notice if it were broken.

    Only the network is faked. The payment method, money account, clearing
    account and MERCHANT_FEE mapping are the seeded ones.
    """

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            id=930_001, username="stripe-happy-path"
        )
        cls.currency = Currency.objects.get(is_base=True)
        cls.customer = Customer.objects.create(
            code="S2-CUST", name="Happy Path Customer", currency=cls.currency
        )
        cls.today = timezone.localdate()
        # The seeded fiscal calendar, not one invented here: settlement posts
        # into whatever period covers today, so the test has to use the same one.
        cls.period = FiscalPeriod.objects.filter(
            start_date__lte=cls.today, end_date__gte=cls.today
        ).first()

        entry = JournalEntry.objects.create(
            number="JE-S2-INV-0001",
            entry_date=cls.today,
            fiscal_period=cls.period,
            journal_type=JournalType.SALES,
            narration="Stub journal for the invoice under test",
            currency=cls.currency,
            exchange_rate=Decimal("1"),
            total_debit_base=Decimal("1000"),
            total_credit_base=Decimal("1000"),
        )
        receivable = AccountMapping.objects.get(key=MappingKey.ACCOUNTS_RECEIVABLE).account
        revenue = AccountMapping.objects.get(key=MappingKey.SALES_REVENUE).account
        JournalLine.objects.create(
            entry=entry,
            line_no=1,
            account=receivable,
            currency=cls.currency,
            exchange_rate=Decimal("1"),
            debit_base=Decimal("1000"),
            debit_txn=Decimal("1000"),
            customer=cls.customer,
        )
        JournalLine.objects.create(
            entry=entry,
            line_no=2,
            account=revenue,
            currency=cls.currency,
            exchange_rate=Decimal("1"),
            credit_base=Decimal("1000"),
            credit_txn=Decimal("1000"),
        )
        cls.invoice = SalesInvoice.objects.create(
            number="S2-INV-0001",
            document_date=cls.today,
            posting_date=cls.today,
            customer=cls.customer,
            currency=cls.currency,
            exchange_rate=Decimal("1"),
            total_txn=Decimal("1000"),
            total_base=Decimal("1000"),
            open_txn=Decimal("1000"),
            status=DocumentStatus.POSTED,
            journal_entry=entry,
        )

    def setUp(self):
        if self.period is None or self.period.status != PeriodStatus.OPEN:
            self.skipTest(
                "No open fiscal period covers today in the seeded calendar, so a "
                "receipt cannot post. Extend the calendar in core.0004."
            )

    def _checkout(self):
        return StripeCheckout.objects.create(
            invoice=self.invoice,
            session_id="cs_happy_path",
            currency=self.currency,
            amount_txn=Decimal("1000"),
            status=StripeCheckoutStatus.PENDING,
            created_by=self.user,
        )

    def _settlement(self):
        return stripe_gateway.Settlement(
            session_id="cs_happy_path",
            paid=True,
            payment_intent_id="pi_happy_path",
            charge_id="ch_happy_path",
            amount=Decimal("1000"),
            currency_code=self.currency.code,
            fee=Decimal("29.30"),
        )

    def _settle(self):
        checkout = self._checkout()
        with mock.patch.object(
            stripe_gateway, "fetch_settlement", return_value=self._settlement()
        ):
            return stripe_service.settle_session(checkout.session_id)

    def test_a_paid_session_posts_a_receipt_and_settles_the_invoice(self):
        checkout = self._settle()

        self.assertEqual(checkout.status, StripeCheckoutStatus.PAID)
        self.assertIsNotNone(checkout.payment_id, checkout.settlement_error)
        self.assertEqual(checkout.settlement_error, "")

        payment = checkout.payment
        self.assertEqual(payment.amount_txn, Decimal("1000.0000"))
        self.assertEqual(payment.fee_txn, Decimal("29.3000"))
        self.assertEqual(payment.status, DocumentStatus.POSTED)
        self.assertIsNotNone(payment.journal_entry_id)
        # The Stripe id is what ties this row back to the dashboard.
        self.assertEqual(payment.reference, "pi_happy_path")

        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.open_txn, Decimal("0.0000"))

    def test_the_journal_expenses_the_fee_and_banks_only_what_arrived(self):
        checkout = self._settle()
        lines = checkout.payment.journal_entry.lines.select_related("account")
        by_code = {line.account.code: line for line in lines}

        # 1140 Stripe Clearing takes the net, 6510 takes the fee, and the
        # customer is credited the gross.
        self.assertEqual(by_code["1140"].debit_base, Decimal("970.7000"))
        self.assertEqual(by_code["6510"].debit_base, Decimal("29.3000"))
        self.assertEqual(
            sum(line.debit_base for line in lines),
            sum(line.credit_base for line in lines),
        )

    def test_a_redelivered_webhook_does_not_pay_the_invoice_twice(self):
        """Stripe delivers at least once, so this happens in production."""
        first = self._settle()
        before = Payment.objects.count()

        with mock.patch.object(
            stripe_gateway, "fetch_settlement", return_value=self._settlement()
        ):
            second = stripe_service.settle_session("cs_happy_path")

        self.assertEqual(second.payment_id, first.payment_id)
        self.assertEqual(Payment.objects.count(), before)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.open_txn, Decimal("0.0000"))
