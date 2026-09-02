"""Configuration forms (CFG-001..CFG-008)."""

from django import forms

from apps.core.form_ui import UIFormMixin
from apps.core.models import (
    Company,
    Currency,
    DocumentSequence,
    PaymentTerm,
    TaxCode,
    TaxTreatment,
)
from apps.ledger.models import Account


class StyledModelForm(UIFormMixin, forms.ModelForm):
    """
    A ModelForm carrying the project's field presentation.

    The styling loop that used to live here — and in a second copy in
    CustomerForm and a third in VendorForm — is now `UIFormMixin`, which also
    supplies placeholders, keyboard hints, autofill and the live-validation
    contract. Settings forms get all of it by subclassing this.
    """


class CurrencyForm(StyledModelForm):
    """CFG-003. BR-002: exactly one base currency."""

    class Meta:
        model = Currency
        fields = ["code", "name", "symbol", "decimal_places", "is_base", "is_active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            # `code` is the primary key. Changing it would INSERT a new row
            # rather than rename this one, so it is fixed once created.
            self.fields["code"].disabled = True
            self.fields["code"].help_text = "The ISO code cannot be changed after creation."

    def clean_code(self):
        code = (self.cleaned_data["code"] or "").strip().upper()
        if len(code) != 3:
            raise forms.ValidationError("Use the three-letter ISO 4217 code, e.g. USD.")
        return code

    def clean(self):
        cleaned = super().clean()

        if cleaned.get("is_base"):
            others = Currency.objects.filter(is_base=True)
            if self.instance.pk:
                others = others.exclude(pk=self.instance.pk)
            existing = others.first()
            if existing:
                self.add_error(
                    "is_base",
                    f"{existing.code} is already the base currency. There can only be "
                    f"one (BR-002), and it cannot change once anything has been posted.",
                )
            if not cleaned.get("is_active"):
                self.add_error("is_active", "The base currency must stay active.")

        return cleaned


class TaxCodeForm(StyledModelForm):
    """CFG-004. FTD-007: only a standard-rated code may carry a non-zero rate."""

    class Meta:
        model = TaxCode
        fields = [
            "code",
            "name",
            "rate_percent",
            "treatment",
            "applies_to",
            "is_inclusive",
            "is_recoverable",
            "output_tax_account",
            "input_tax_account",
            "is_active",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Only a postable, active account can receive a posting (GL-010), so
        # offering any other account here would be offering a broken choice.
        postable = Account.objects.filter(is_active=True, is_postable=True)
        self.fields["output_tax_account"].queryset = postable
        self.fields["input_tax_account"].queryset = postable

    def clean_code(self):
        code = (self.cleaned_data["code"] or "").strip().upper()
        clash = TaxCode.objects.filter(code__iexact=code)
        if self.instance.pk:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError(f"Tax code “{code}” already exists.")
        return code

    def clean(self):
        cleaned = super().clean()
        treatment = cleaned.get("treatment")
        rate = cleaned.get("rate_percent")

        # FTD-007, mirrored from the database constraint so the user gets a
        # sentence instead of an IntegrityError page.
        if treatment and treatment != TaxTreatment.STANDARD and rate:
            self.add_error(
                "rate_percent",
                f"A {TaxTreatment(treatment).label.lower()} code must have a rate of 0. "
                f"Use “Standard rated” if this code charges tax.",
            )

        return cleaned


class PaymentTermForm(StyledModelForm):
    """CFG-005."""

    class Meta:
        model = PaymentTerm
        fields = [
            "code",
            "name",
            "net_days",
            "end_of_month",
            "discount_percent",
            "discount_days",
            "is_active",
        ]

    def clean_code(self):
        code = (self.cleaned_data["code"] or "").strip().upper()
        clash = PaymentTerm.objects.filter(code__iexact=code)
        if self.instance.pk:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError(f"Payment term “{code}” already exists.")
        return code

    def clean(self):
        cleaned = super().clean()
        discount_days = cleaned.get("discount_days") or 0
        net_days = cleaned.get("net_days") or 0
        discount = cleaned.get("discount_percent") or 0

        # A discount window longer than the term itself can never be earned.
        if discount and discount_days > net_days:
            self.add_error(
                "discount_days",
                f"The discount window ({discount_days} days) cannot be longer than "
                f"the payment term ({net_days} days).",
            )
        return cleaned


class CompanyForm(StyledModelForm):
    """CFG-001, CFG-010. One legal entity, edited in place — never created here."""

    class Meta:
        model = Company
        fields = [
            "name",
            "legal_name",
            "tax_id",
            "registration_no",
            "email",
            "phone",
            "address_line1",
            "address_line2",
            "city",
            "state",
            "postal_code",
            "country",
            "base_currency",
            "fiscal_year_start_month",
            "timezone",
            "language",
            "allow_negative_stock",
            "rounding_tolerance",
            "price_decimal_places",
            "qty_decimal_places",
            "require_po_approval",
            "require_so_approval",
            "block_duplicate_vendor_invoice",
            "warn_duplicate_customer_ref",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["base_currency"].queryset = Currency.objects.filter(is_active=True)
        self.fields[
            "base_currency"
        ].help_text = "OD-02: cannot be changed once a transaction has been posted."

    def clean_fiscal_year_start_month(self):
        month = self.cleaned_data["fiscal_year_start_month"]
        if not 1 <= month <= 12:
            raise forms.ValidationError("Use a month number between 1 and 12.")
        return month


class DocumentSequenceForm(StyledModelForm):
    """CFG-008. Number series behind numbering.next_number()."""

    class Meta:
        model = DocumentSequence
        fields = [
            "document_type",
            "series",
            "prefix",
            "suffix",
            "padding",
            "next_number",
            "reset_policy",
            "is_active",
        ]

    def clean_next_number(self):
        value = self.cleaned_data["next_number"]
        # Lowering the counter would re-issue numbers already on real documents.
        if self.instance.pk and value < self.instance.next_number:
            raise forms.ValidationError(
                f"The counter is already at {self.instance.next_number}. Lowering it "
                f"would issue document numbers that are already in use."
            )
        return value
