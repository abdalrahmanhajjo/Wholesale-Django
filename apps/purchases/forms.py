"""
Purchase order forms: the header and the line formset (PUR-001).

Line amounts (tax, discount, totals) are never taken from the form — they are
derived by `apps.purchases.services.recalculate_order` after the formset is
saved, so a line can only ever show what the arithmetic contract in
apps/core/models.py actually produces (BR-010, BR-011, BR-012).
"""

from decimal import Decimal

from django import forms
from django.forms import inlineformset_factory

from apps.catalog.models import Product, UnitOfMeasure
from apps.core.models import TaxCode
from apps.inventory.models import Warehouse
from apps.parties.models import Vendor
from apps.purchases.models import PurchaseOrder, PurchaseOrderLine


class TaxCodeSelect(forms.Select):
    """
    Stamps each `<option>` with the tax code's rate so the line-total preview
    script (purchase_order_form.html) can read it without another request.
    Server-side totals never use this — they read the TaxCode row directly.
    """

    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex, attrs)
        instance = getattr(value, "instance", None)
        if instance is not None:
            option["attrs"]["data-rate"] = str(instance.rate_percent)
        return option


def _style(fields):
    for fld in fields.values():
        widget = fld.widget
        if isinstance(widget, forms.CheckboxInput):
            widget.attrs.setdefault(
                "class", "h-4 w-4 rounded border-line text-brand focus:ring-brand/30"
            )
        elif isinstance(widget, forms.Textarea):
            widget.attrs.setdefault(
                "class",
                "block w-full rounded-xl border border-line bg-white px-3 py-2 text-sm "
                "focus:border-brand focus:ring-2 focus:ring-brand/30 focus:outline-none",
            )
        else:
            widget.attrs.setdefault("class", "field")


class PurchaseOrderForm(forms.ModelForm):
    class Meta:
        model = PurchaseOrder
        fields = [
            "vendor",
            "warehouse",
            "document_date",
            "expected_date",
            "due_date",
            "currency",
            "exchange_rate",
            "payment_term",
            "buyer",
            "vendor_reference",
            "delivery_address_text",
            "document_discount_kind",
            "document_discount_value",
            "notes",
        ]
        widgets = {
            "document_date": forms.DateInput(attrs={"type": "date"}),
            "expected_date": forms.DateInput(attrs={"type": "date"}),
            "due_date": forms.DateInput(attrs={"type": "date"}),
            "delivery_address_text": forms.Textarea(attrs={"rows": 3}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # PTY-008 / CFG-*: an inactive vendor or warehouse cannot be chosen for
        # a new order — only offered here if it is already on the instance.
        self.fields["vendor"].queryset = Vendor.objects.filter(is_active=True)
        self.fields["warehouse"].queryset = Warehouse.objects.filter(is_active=True)
        self.fields["document_discount_value"].required = False
        _style(self.fields)

    def clean_document_discount_value(self):
        return self.cleaned_data.get("document_discount_value") or Decimal("0")

    def save(self, commit=True):
        order = super().save(commit=False)
        # PurchaseOrder never posts to the ledger itself (only its bill does),
        # but FinancialDocumentBase.posting_date is non-null, so it tracks the
        # document date rather than asking the user for a value that means
        # nothing until a bill exists.
        order.posting_date = order.document_date
        if commit:
            order.save()
        return order


class PurchaseOrderLineForm(forms.ModelForm):
    class Meta:
        model = PurchaseOrderLine
        fields = [
            "product",
            "description",
            "unit",
            "warehouse",
            "tax_code",
            "quantity",
            "unit_price",
            "discount_percent",
        ]
        widgets = {"tax_code": TaxCodeSelect}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["product"].queryset = Product.objects.filter(is_active=True).order_by(
            "sku"
        )
        self.fields["unit"].queryset = UnitOfMeasure.objects.filter(is_active=True)
        self.fields["warehouse"].queryset = Warehouse.objects.filter(is_active=True)
        self.fields["warehouse"].required = False
        self.fields["tax_code"].queryset = TaxCode.objects.filter(
            is_active=True, applies_to__in=["PURCHASE", "BOTH"]
        )
        self.fields["tax_code"].required = False
        self.fields["discount_percent"].required = False
        self.fields["description"].required = False
        _style(self.fields)
        # data-role hooks for the line-total preview script (purchase_order_form.html).
        for name in (
            "product",
            "unit",
            "warehouse",
            "tax_code",
            "quantity",
            "unit_price",
            "discount_percent",
        ):
            self.fields[name].widget.attrs["data-role"] = name

    def clean_discount_percent(self):
        return self.cleaned_data.get("discount_percent") or Decimal("0")

    def clean_quantity(self):
        quantity = self.cleaned_data.get("quantity")
        if quantity is not None and quantity <= 0:
            raise forms.ValidationError("Quantity must be greater than zero.")
        return quantity


#: One order, many lines. `min_num=1` means a PUR-001 order cannot be saved
#: empty — there is nothing to submit for approval otherwise (PUR-002).
PurchaseOrderLineFormSet = inlineformset_factory(
    PurchaseOrder,
    PurchaseOrderLine,
    form=PurchaseOrderLineForm,
    fk_name="order",
    extra=1,
    can_delete=True,
    min_num=1,
    validate_min=True,
)


class PurchaseOrderRejectForm(forms.Form):
    """PUR-002: a rejection must say why (mirrors ACC-008's reason requirement)."""

    reason = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 2, "class": "field"}),
        label="Reason for rejection",
    )
