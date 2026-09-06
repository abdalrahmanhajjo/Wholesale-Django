"""Server-rendered payment entry and allocation forms (PAY-001..PAY-007)."""

import uuid
from decimal import Decimal

from django import forms
from django.forms import BaseFormSet, formset_factory
from django.utils import timezone

from apps.core.form_ui import UIFormMixin
from apps.payments.allocation import AllocationLineInput
from apps.payments.models import Payment, PaymentDirection


class PaymentForm(UIFormMixin, forms.ModelForm):
    placeholders = {
        "amount_txn": "0.00",
        "fee_txn": "0.00",
        "exchange_rate": "1.000000",
        "reference": "Cheque number or transfer reference",
        "narration": "What this payment settles",
    }
    autocomplete_fields = ["customer", "vendor", "money_account"]
    plain_selects = ["direction"]

    class Meta:
        model = Payment
        fields = [
            "direction",
            "payment_date",
            "posting_date",
            "customer",
            "vendor",
            "currency",
            "exchange_rate",
            "amount_txn",
            "fee_txn",
            "method",
            "money_account",
            "reference",
            "narration",
        ]
        widgets = {
            "payment_date": forms.DateInput(attrs={"type": "date"}),
            "posting_date": forms.DateInput(attrs={"type": "date"}),
            "narration": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        today = timezone.localdate()
        if not self.is_bound and not self.instance.pk:
            self.initial.setdefault("payment_date", today)
            self.initial.setdefault("posting_date", today)

        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "field")
        self.fields["customer"].queryset = self.fields["customer"].queryset.filter(
            is_active=True
        )
        self.fields["vendor"].queryset = self.fields["vendor"].queryset.filter(is_active=True)
        self.fields["method"].queryset = (
            self.fields["method"]
            .queryset.filter(is_active=True)
            .select_related("default_money_account")
        )
        self.fields["money_account"].queryset = (
            self.fields["money_account"]
            .queryset.filter(is_active=True)
            .select_related("currency")
        )
        self.fields["currency"].queryset = self.fields["currency"].queryset.filter(
            is_active=True
        )
        self.fields["customer"].required = False
        self.fields["vendor"].required = False
        self.fields["money_account"].required = False
        # Most payments carry no fee, so an empty box means zero rather than
        # forcing everyone to type a 0 on every cash receipt they enter.
        self.fields["fee_txn"].required = False
        self.fields["fee_txn"].label = "Processor fee"

    def clean(self):
        cleaned = super().clean()
        direction = cleaned.get("direction")
        customer = cleaned.get("customer")
        vendor = cleaned.get("vendor")
        method = cleaned.get("method")
        account = cleaned.get("money_account")
        reference = (cleaned.get("reference") or "").strip()
        fee = cleaned.get("fee_txn")
        amount = cleaned.get("amount_txn")

        if fee is None:
            fee = Decimal("0")
            cleaned["fee_txn"] = fee
        if fee < 0:
            self.add_error("fee_txn", "A fee cannot be negative.")
        elif (
            fee
            and amount is not None
            and direction == PaymentDirection.RECEIPT
            and fee >= amount
        ):
            self.add_error(
                "fee_txn",
                "The fee has to be smaller than the receipt - a fee equal to the "
                "whole amount would mean no money arrived at all.",
            )

        if direction == PaymentDirection.RECEIPT:
            if not customer:
                self.add_error("customer", "Select the customer who made this receipt.")
            if vendor:
                self.add_error("vendor", "Leave vendor empty for a customer receipt.")
        elif direction == PaymentDirection.PAYMENT:
            if not vendor:
                self.add_error("vendor", "Select the vendor receiving this payment.")
            if customer:
                self.add_error("customer", "Leave customer empty for a vendor payment.")

        if method and method.requires_reference and not reference:
            self.add_error(
                "reference", f"A reference is required for payment method {method.name}."
            )
        if not account and method and method.default_money_account:
            account = method.default_money_account
            cleaned["money_account"] = account
        if not account:
            self.add_error("money_account", "Select a money account.")
        return cleaned


class PaymentAllocationHeaderForm(UIFormMixin, forms.Form):
    allocation_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    batch_key = forms.UUIDField(widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.initial.setdefault("allocation_date", timezone.localdate())
            self.initial.setdefault("batch_key", uuid.uuid4())


class AllocationLineForm(UIFormMixin, forms.Form):
    target_id = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    amount_txn = forms.DecimalField(
        required=False,
        min_value=Decimal("0"),
        max_digits=18,
        decimal_places=4,
        widget=forms.NumberInput(
            attrs={
                "step": "0.0001",
                "inputmode": "decimal",
                "autocomplete": "off",
                "class": "field text-right tabular-nums",
                "aria-label": "Amount to allocate",
            }
        ),
    )


class BaseAllocationLineFormSet(BaseFormSet):
    """Collect only positive rows; blank and zero rows are intentional no-ops."""

    allocation_lines: tuple[AllocationLineInput, ...] = ()

    def clean(self):
        super().clean()
        if any(self.errors):
            return
        target_ids = set()
        lines = []
        for form in self.forms:
            target_id = form.cleaned_data["target_id"]
            if target_id in target_ids:
                raise forms.ValidationError("An open document appears more than once.")
            target_ids.add(target_id)
            amount = form.cleaned_data.get("amount_txn") or Decimal("0")
            if amount > 0:
                lines.append(AllocationLineInput(target_id=target_id, amount_txn=amount))
        if not lines:
            raise forms.ValidationError("Enter an amount for at least one open document.")
        self.allocation_lines = tuple(lines)


PaymentAllocationLineFormSet = formset_factory(
    AllocationLineForm,
    formset=BaseAllocationLineFormSet,
    extra=0,
    max_num=200,
    validate_max=True,
    absolute_max=250,
)


class ReversalForm(UIFormMixin, forms.Form):
    """Why, and when. PAY-010 makes both mandatory on the audit trail."""

    reason = forms.CharField(
        max_length=255,
        widget=forms.Textarea(
            attrs={
                "rows": 3,
                "class": "field",
                "placeholder": "Why is this being reversed?",
            }
        ),
        help_text="Recorded against the reversal and shown in the audit trail.",
    )
    reversal_date = forms.DateField(
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text="The date the reversing journal is posted into.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.initial.setdefault("reversal_date", timezone.localdate())
