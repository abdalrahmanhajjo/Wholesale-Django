"""
Customer and vendor forms.

PTY-007 is the interesting requirement: "Prevent duplicate party codes and flag
likely duplicate tax IDs/contact values. Exact codes are blocked; potential
duplicates produce a review warning."

That is two different behaviours, and the form implements both:
  * the code is a hard error — the database has a case-insensitive unique index
    behind it, so this form check is the friendly version of a constraint that
    would fire anyway;
  * a similar name or a matching tax ID is a *warning*, surfaced for review and
    overridable, because two genuinely different companies can share a trading
    name.
"""

from django import forms
from django.contrib.postgres.search import TrigramSimilarity
from django.db.models.functions import Upper

from apps.core.form_ui import UIFormMixin
from apps.parties.models import Customer, Vendor

SIMILARITY_THRESHOLD = 0.45


class PartyFormMixin:
    """Shared duplicate detection for customers and vendors (PTY-007)."""

    model_class = None

    def clean_code(self):
        code = (self.cleaned_data["code"] or "").strip()
        clash = self.model_class.objects.filter(code__iexact=code)
        if self.instance.pk:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError(
                f"Code “{code}” is already in use by {clash.first().name}. "
                "Codes must be unique, and case does not make them different."
            )
        return code

    def duplicate_warnings(self):
        """
        Soft warnings for the template. Not validation errors: the user may
        legitimately have two customers with similar names.
        """
        warnings = []
        name = (self.cleaned_data.get("name") or "").strip()
        tax_id = (self.cleaned_data.get("tax_id") or "").strip()

        if tax_id:
            same_tax = self.model_class.objects.filter(tax_id__iexact=tax_id)
            if self.instance.pk:
                same_tax = same_tax.exclude(pk=self.instance.pk)
            for other in same_tax[:3]:
                warnings.append(f"{other.code} — {other.name} has the same tax ID.")

        if len(name) >= 4:
            similar = self.model_class.objects.annotate(
                similarity=TrigramSimilarity(Upper("name"), name.upper())
            ).filter(similarity__gt=SIMILARITY_THRESHOLD)
            if self.instance.pk:
                similar = similar.exclude(pk=self.instance.pk)
            for other in similar.order_by("-similarity")[:3]:
                warnings.append(
                    f"{other.code} — {other.name} has a similar name "
                    f"({other.similarity:.0%} match)."
                )
        return warnings


class CustomerForm(PartyFormMixin, UIFormMixin, forms.ModelForm):
    model_class = Customer
    placeholders = {
        "code": "CUST-0001",
        "name": "The trading name you invoice under",
        "legal_name": "Registered name, if it differs",
        "credit_limit": "0.00 for no limit",
    }
    autocomplete_fields = ["currency", "payment_term", "default_tax_code"]
    checks = {"code": "customer-code", "name": "similar-customer-name"}

    class Meta:
        model = Customer
        fields = [
            "code",
            "name",
            "legal_name",
            "tax_id",
            "email",
            "phone",
            "website",
            "currency",
            "payment_term",
            "credit_limit",
            "credit_hold",
            "credit_hold_reason",
            "default_warehouse",
            "salesperson",
            "default_tax_code",
            "notes",
            "is_active",
        ]
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 3}),
            "credit_hold_reason": forms.TextInput(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # PTY-008: an inactive party cannot be chosen for new transactions, so
        # inactive records are not offered here either.
        self.fields["default_warehouse"].queryset = self.fields[
            "default_warehouse"
        ].queryset.filter(is_active=True)

    def clean(self):
        cleaned = super().clean()
        # PTY-004: a hold without a reason is not reviewable later.
        if (
            cleaned.get("credit_hold")
            and not (cleaned.get("credit_hold_reason") or "").strip()
        ):
            self.add_error(
                "credit_hold_reason", "Give a reason when placing a customer on credit hold."
            )
        return cleaned


class VendorForm(PartyFormMixin, UIFormMixin, forms.ModelForm):
    model_class = Vendor
    placeholders = {
        "code": "VEND-0001",
        "name": "The trading name on their invoices",
        "legal_name": "Registered name, if it differs",
    }
    autocomplete_fields = ["currency", "payment_term", "default_tax_code"]
    checks = {"code": "vendor-code", "name": "similar-vendor-name"}

    class Meta:
        model = Vendor
        fields = [
            "code",
            "name",
            "legal_name",
            "tax_id",
            "email",
            "phone",
            "website",
            "currency",
            "payment_term",
            "default_expense_account",
            "default_tax_code",
            "notes",
            "is_active",
        ]
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
