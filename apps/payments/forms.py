"""Server-rendered payment and receipt entry form (PAY-001, PAY-002)."""

from django import forms
from django.utils import timezone

from apps.payments.models import Payment, PaymentDirection


class PaymentForm(forms.ModelForm):
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

    def clean(self):
        cleaned = super().clean()
        direction = cleaned.get("direction")
        customer = cleaned.get("customer")
        vendor = cleaned.get("vendor")
        method = cleaned.get("method")
        account = cleaned.get("money_account")
        reference = (cleaned.get("reference") or "").strip()

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
