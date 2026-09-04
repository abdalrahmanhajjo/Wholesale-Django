"""Catalog forms (CFG-011, GL-010)."""

from django import forms

from apps.catalog.models import ProductCategory, UnitOfMeasure
from apps.core.form_ui import UIFormMixin
from apps.ledger.models import Account, AccountType


class UnitOfMeasureForm(UIFormMixin, forms.ModelForm):
    """A unit either is a base unit, or converts to one."""

    class Meta:
        model = UnitOfMeasure
        fields = ["code", "name", "decimal_places", "base_unit", "ratio_to_base", "is_active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        units = UnitOfMeasure.objects.filter(is_active=True).order_by("code")
        if self.instance.pk:
            units = units.exclude(pk=self.instance.pk)
        self.fields["base_unit"].queryset = units
        self.fields["base_unit"].help_text = "Leave empty if this is itself a base unit."

    def clean_code(self):
        code = (self.cleaned_data["code"] or "").strip().upper()
        clash = UnitOfMeasure.objects.filter(code__iexact=code)
        if self.instance.pk:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError(f"Unit “{code}” already exists.")
        return code

    def clean(self):
        cleaned = super().clean()
        base_unit = cleaned.get("base_unit")
        ratio = cleaned.get("ratio_to_base")

        # Mirrors uom_ratio_positive.
        if ratio is not None and ratio <= 0:
            self.add_error("ratio_to_base", "The conversion ratio must be greater than zero.")

        # Not in the schema, but implied by it: a unit with no base unit is its
        # own base, so a ratio other than 1 would be meaningless.
        if base_unit is None and ratio is not None and ratio != 1:
            self.add_error(
                "ratio_to_base",
                "A base unit converts to itself, so its ratio must be 1. "
                "Choose a base unit if this one converts to another.",
            )

        # The database blocks a unit being its own base; a longer loop
        # (BOX → CASE → BOX) would slip past it and hang any conversion.
        node = base_unit
        seen = set()
        while node is not None:
            if self.instance.pk and node.pk == self.instance.pk:
                self.add_error("base_unit", "That would make the unit convert to itself.")
                break
            if node.pk in seen:
                break
            seen.add(node.pk)
            node = node.base_unit

        return cleaned


class ProductCategoryForm(UIFormMixin, forms.ModelForm):
    """CFG-007: a category can steer postings for the products under it."""

    #: Which account type each override must point at, so revenue cannot be
    #: mapped to an expense account by accident.
    ACCOUNT_TYPES = {
        "revenue_account": AccountType.INCOME,
        "cogs_account": AccountType.EXPENSE,
        "inventory_account": AccountType.ASSET,
    }

    class Meta:
        model = ProductCategory
        fields = [
            "code",
            "name",
            "parent",
            "revenue_account",
            "cogs_account",
            "inventory_account",
            "is_active",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        parents = ProductCategory.objects.filter(is_active=True).order_by("code")
        if self.instance.pk:
            parents = parents.exclude(pk=self.instance.pk)
        self.fields["parent"].queryset = parents

        # GL-010: only a postable, active account can receive a posting.
        postable = Account.objects.filter(is_active=True, is_postable=True).order_by("code")
        for name in self.ACCOUNT_TYPES:
            self.fields[name].queryset = postable
            self.fields[name].help_text = "Optional. Overrides the account mapping."

    def clean_code(self):
        code = (self.cleaned_data["code"] or "").strip().upper()
        clash = ProductCategory.objects.filter(code__iexact=code)
        if self.instance.pk:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError(f"Category “{code}” already exists.")
        return code

    def clean(self):
        cleaned = super().clean()

        # No cycles. The database blocks self-parenting only.
        node = cleaned.get("parent")
        seen = set()
        while node is not None:
            if self.instance.pk and node.pk == self.instance.pk:
                self.add_error("parent", "That would make the category its own ancestor.")
                break
            if node.pk in seen:
                break
            seen.add(node.pk)
            node = node.parent

        # An account override must be of the right kind.
        for name, expected in self.ACCOUNT_TYPES.items():
            account = cleaned.get(name)
            if account and account.account_type != expected:
                self.add_error(
                    name,
                    f"{account.code} is {AccountType(account.account_type).label.lower()}. "
                    f"This must be {AccountType(expected).label.lower()}.",
                )

        return cleaned
