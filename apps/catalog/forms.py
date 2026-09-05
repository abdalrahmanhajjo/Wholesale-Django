"""Catalog forms (CFG-011, GL-010)."""

from django import forms
from django.db.models import Q

from apps.catalog.models import (
    Product,
    ProductCategory,
    ProductPrice,
    ProductType,
    UnitOfMeasure,
)
from apps.core.form_ui import UIFormMixin
from apps.core.models import Currency, TaxApplicability, TaxCode
from apps.ledger.models import Account, AccountType
from apps.parties.models import Vendor


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


class ProductForm(UIFormMixin, forms.ModelForm):
    """
    CFG-011. Four database constraints and two business rules are mirrored here
    so a wrong combination produces a sentence rather than an IntegrityError.
    """

    #: Which account type each override must point at.
    ACCOUNT_TYPES = {
        "revenue_account": AccountType.INCOME,
        "cogs_account": AccountType.EXPENSE,
        "inventory_account": AccountType.ASSET,
        "expense_account": AccountType.EXPENSE,
    }

    autocomplete_fields = ["category", "unit", "preferred_vendor"]

    class Meta:
        model = Product
        fields = [
            "sku",
            "name",
            "description",
            "barcode",
            "category",
            "unit",
            "product_type",
            "is_inventory",
            "sales_price",
            "purchase_price",
            "default_sales_tax_code",
            "default_purchase_tax_code",
            "preferred_vendor",
            "reorder_level",
            "max_discount_percent",
            "revenue_account",
            "cogs_account",
            "inventory_account",
            "expense_account",
            "is_active",
        ]
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["category"].queryset = ProductCategory.objects.filter(
            is_active=True
        ).order_by("code")
        self.fields["unit"].queryset = UnitOfMeasure.objects.filter(is_active=True).order_by(
            "code"
        )
        self.fields["preferred_vendor"].queryset = Vendor.objects.filter(
            is_active=True
        ).order_by("code")

        # A sales tax code cannot be used on a purchase, and vice versa.
        active_tax = TaxCode.objects.filter(is_active=True).order_by("code")
        self.fields["default_sales_tax_code"].queryset = active_tax.filter(
            applies_to__in=[TaxApplicability.SALES, TaxApplicability.BOTH]
        )
        self.fields["default_purchase_tax_code"].queryset = active_tax.filter(
            applies_to__in=[TaxApplicability.PURCHASE, TaxApplicability.BOTH]
        )

        # GL-010: only a postable, active account can receive a posting.
        postable = Account.objects.filter(is_active=True, is_postable=True).order_by("code")
        for name in self.ACCOUNT_TYPES:
            self.fields[name].queryset = postable
            self.fields[name].help_text = "Optional. Overrides the category and the mapping."

    def clean_sku(self):
        sku = (self.cleaned_data["sku"] or "").strip().upper()
        clash = Product.objects.filter(sku__iexact=sku)
        if self.instance.pk:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError(
                f"SKU “{sku}” is already used by {clash.first().name}."
            )
        return sku

    def clean(self):
        cleaned = super().clean()
        product_type = cleaned.get("product_type")
        is_inventory = cleaned.get("is_inventory")

        # product_nonstock_not_inventory: only a stocked item carries inventory.
        if is_inventory and product_type and product_type != ProductType.STOCK:
            self.add_error(
                "is_inventory",
                f"A {ProductType(product_type).label.lower()} does not carry stock, "
                f"so it cannot be an inventory item (SAL-010).",
            )

        # product_prices_nonneg and product_reorder_nonneg.
        for field, label in (
            ("sales_price", "sales price"),
            ("purchase_price", "purchase price"),
            ("reorder_level", "reorder level"),
        ):
            value = cleaned.get(field)
            if value is not None and value < 0:
                self.add_error(field, f"The {label} cannot be negative.")

        # product_max_discount_range.
        discount = cleaned.get("max_discount_percent")
        if discount is not None and not 0 <= discount <= 100:
            self.add_error(
                "max_discount_percent", "The maximum discount must be between 0 and 100."
            )

        # Each account override must be of the right kind.
        for name, expected in self.ACCOUNT_TYPES.items():
            account = cleaned.get(name)
            if account and account.account_type != expected:
                self.add_error(
                    name,
                    f"{account.code} is {AccountType(account.account_type).label.lower()}. "
                    f"This must be {AccountType(expected).label.lower()}.",
                )

        return cleaned


class ProductPriceForm(UIFormMixin, forms.ModelForm):
    """
    A dated price for one product, kind, currency and minimum quantity.

    The schema allows two entries whose date ranges overlap, which would make
    "what does this cost today?" ambiguous. We refuse the overlap here so the
    lookup in `apps.catalog.pricing` always has exactly one answer.
    """

    class Meta:
        model = ProductPrice
        fields = ["kind", "currency", "price", "min_quantity", "valid_from", "valid_to"]
        widgets = {
            "valid_from": forms.DateInput(attrs={"type": "date"}),
            "valid_to": forms.DateInput(attrs={"type": "date"}),
        }

    def __init__(self, *args, product=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.product = product or self.instance.product
        self.fields["currency"].queryset = Currency.objects.filter(is_active=True)
        self.fields["valid_to"].help_text = "Leave empty for an open-ended price."
        self.fields["min_quantity"].help_text = "Applies from this quantity upwards."

    def clean(self):
        cleaned = super().clean()
        price = cleaned.get("price")
        valid_from = cleaned.get("valid_from")
        valid_to = cleaned.get("valid_to")

        # product_price_nonneg.
        if price is not None and price < 0:
            self.add_error("price", "A price cannot be negative.")

        # product_price_dates_ordered.
        if valid_from and valid_to and valid_to < valid_from:
            self.add_error("valid_to", "The end date cannot be before the start date.")

        # Not in the schema: two prices covering the same day, for the same
        # kind, currency and minimum quantity, have no defined winner.
        if valid_from and self.product:
            clash = ProductPrice.objects.filter(
                product=self.product,
                kind=cleaned.get("kind"),
                currency=cleaned.get("currency"),
                min_quantity=cleaned.get("min_quantity"),
            )
            if self.instance.pk:
                clash = clash.exclude(pk=self.instance.pk)
            # Two ranges overlap unless one ends before the other starts.
            clash = clash.filter(Q(valid_to__isnull=True) | Q(valid_to__gte=valid_from))
            if valid_to:
                clash = clash.filter(valid_from__lte=valid_to)
            existing = clash.order_by("valid_from").first()
            if existing:
                self.add_error(
                    "valid_from",
                    f"{existing.currency_id} {existing.price} already covers this period "
                    f"(from {existing.valid_from}"
                    f"{f' to {existing.valid_to}' if existing.valid_to else ', open ended'}). "
                    f"Close that price first, or use a different minimum quantity.",
                )

        return cleaned
