from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
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
from apps.ledger.models import Account, AccountSubtype, AccountType, NormalBalance
from apps.parties.models import Customer, Vendor
from apps.payments.forms import PaymentForm
from apps.payments.models import MoneyAccount, PaymentDirection, PaymentMethod
from apps.payments.services import create_payment


class PaymentFixtureMixin:
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            id=910_001, username="payments-member4"
        )
        cls.currency = Currency.objects.create(
            code="P4T", name="Payment Test Currency", symbol="P", is_active=True
        )
        cls.customer = Customer.objects.create(
            code="P4-CUST", name="Payment Customer", currency=cls.currency
        )
        cls.vendor = Vendor.objects.create(
            code="P4-VEND", name="Payment Vendor", currency=cls.currency
        )
        cls.gl_account = Account.objects.create(
            code="P4-CASH",
            name="Payment cash account",
            account_type=AccountType.ASSET,
            subtype=AccountSubtype.CURRENT_ASSET,
            normal_balance=NormalBalance.DEBIT,
        )
        cls.money_account = MoneyAccount.objects.create(
            code="P4-BANK",
            name="Payment test bank",
            currency=cls.currency,
            gl_account=cls.gl_account,
        )
        cls.method = PaymentMethod.objects.create(
            code="P4-WIRE",
            name="Wire transfer",
            requires_reference=True,
            default_money_account=cls.money_account,
        )
        year = FiscalYear.objects.create(
            code="P4-FY91", start_date=date(2091, 1, 1), end_date=date(2091, 12, 31)
        )
        cls.period = FiscalPeriod.objects.create(
            fiscal_year=year,
            period_no=9,
            name="Payment September 2026",
            start_date=date(2091, 9, 1),
            end_date=date(2091, 9, 30),
        )
        DocumentSequence.objects.update_or_create(
            document_type=DocumentType.CUSTOMER_RECEIPT,
            series="DEFAULT",
            defaults={"prefix": "RC-", "padding": 4, "next_number": 1, "period_key": ""},
        )
        DocumentSequence.objects.update_or_create(
            document_type=DocumentType.VENDOR_PAYMENT,
            series="DEFAULT",
            defaults={"prefix": "PV-", "padding": 4, "next_number": 1, "period_key": ""},
        )

    def valid_data(self, **overrides):
        values = {
            "direction": PaymentDirection.RECEIPT,
            "payment_date": date(2091, 9, 1),
            "posting_date": date(2091, 9, 1),
            "customer": self.customer,
            "vendor": None,
            "currency": self.currency,
            "exchange_rate": Decimal("1.25"),
            "amount_txn": Decimal("80"),
            "method": self.method,
            "money_account": self.money_account,
            "reference": "WIRE-2026-001",
            "narration": "Receipt against future allocation",
        }
        values.update(overrides)
        return values


class PaymentFormTests(PaymentFixtureMixin, TestCase):
    def test_reference_is_required_by_configured_method(self):
        data = self.valid_data(reference="")
        form = PaymentForm(
            data={key: getattr(value, "pk", value) for key, value in data.items()}
        )
        self.assertFalse(form.is_valid())
        self.assertIn("reference", form.errors)

    def test_direction_requires_matching_party(self):
        data = self.valid_data(customer=None, vendor=self.vendor)
        form = PaymentForm(
            data={key: getattr(value, "pk", value) for key, value in data.items()}
        )
        self.assertFalse(form.is_valid())
        self.assertIn("customer", form.errors)
        self.assertIn("vendor", form.errors)


class PaymentServiceTests(PaymentFixtureMixin, TestCase):
    def test_create_receipt_generates_number_and_derived_amounts(self):
        payment = create_payment(user=self.user, **self.valid_data())
        self.assertEqual(payment.number, "RC-0001")
        self.assertEqual(payment.fiscal_period, self.period)
        self.assertEqual(payment.amount_base, Decimal("100.0000"))
        self.assertEqual(payment.allocated_txn, Decimal("0"))
        self.assertEqual(payment.unallocated_txn, Decimal("80"))
        self.assertEqual(payment.status, "DRAFT")

    def test_closed_period_is_rejected_without_consuming_number(self):
        self.period.status = "CLOSED"
        self.period.closed_by = self.user
        self.period.save(update_fields=["status", "closed_by"])
        with self.assertRaises(ValidationError):
            create_payment(user=self.user, **self.valid_data())
        sequence = DocumentSequence.objects.get(
            document_type=DocumentType.CUSTOMER_RECEIPT, series="DEFAULT"
        )
        self.assertEqual(sequence.next_number, 1)

    def test_vendor_payment_uses_its_own_number_series(self):
        payment = create_payment(
            user=self.user,
            **self.valid_data(
                direction=PaymentDirection.PAYMENT, customer=None, vendor=self.vendor
            ),
        )
        self.assertEqual(payment.number, "PV-0001")
        self.assertEqual(payment.party, self.vendor)

    def test_inactive_party_is_rejected_without_consuming_number(self):
        self.customer.is_active = False
        self.customer.save(update_fields=["is_active"])

        with self.assertRaises(ValidationError):
            create_payment(user=self.user, **self.valid_data())

        sequence = DocumentSequence.objects.get(
            document_type=DocumentType.CUSTOMER_RECEIPT, series="DEFAULT"
        )
        self.assertEqual(sequence.next_number, 1)


class PaymentViewPermissionTests(PaymentFixtureMixin, TestCase):
    def _grant(self, *codenames):
        self.user.user_permissions.add(
            *Permission.objects.filter(
                content_type__app_label="payments", codename__in=codenames
            )
        )

    def test_register_and_entry_require_declared_permissions(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("payments:payment_list")).status_code, 403)
        self.assertEqual(self.client.get(reverse("payments:payment_create")).status_code, 403)

    def test_detail_requires_view_permission(self):
        payment = create_payment(user=self.user, **self.valid_data())
        self.client.force_login(self.user)
        response = self.client.get(reverse("payments:payment_detail", args=[payment.pk]))
        self.assertEqual(response.status_code, 403)

    def test_user_with_view_permission_can_open_register_and_detail(self):
        payment = create_payment(user=self.user, **self.valid_data())
        self._grant("view_payment")
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("payments:payment_list")).status_code, 200)
        self.assertEqual(
            self.client.get(reverse("payments:payment_detail", args=[payment.pk])).status_code,
            200,
        )

    def test_non_draft_payment_cannot_be_opened_in_update_view(self):
        payment = create_payment(user=self.user, **self.valid_data())
        payment.status = DocumentStatus.APPROVED
        payment.save(update_fields=["status"])
        self._grant("change_payment")
        self.client.force_login(self.user)
        response = self.client.get(reverse("payments:payment_edit", args=[payment.pk]))
        self.assertEqual(response.status_code, 404)
