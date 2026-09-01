"""
Goods receipt forms: the header and the accept/reject line formset (PUR-003,
PUR-004).

Costs and the accepted-quantity split are never taken from the form — they
are derived by `apps.inventory.services.recalculate_receipt` after the
formset is saved, mirroring apps/purchases/forms.py.
"""

from django import forms
from django.forms import inlineformset_factory

from apps.catalog.models import Product, UnitOfMeasure
from apps.inventory.models import GoodsReceipt, GoodsReceiptLine, Warehouse
from apps.parties.models import Vendor
from apps.purchases.models import PurchaseOrder


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


class GoodsReceiptForm(forms.ModelForm):
    class Meta:
        model = GoodsReceipt
        fields = [
            "vendor",
            "purchase_order",
            "warehouse",
            "document_date",
            "vendor_delivery_note",
            "received_by",
            "reference",
            "notes",
        ]
        widgets = {
            "document_date": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["vendor"].queryset = Vendor.objects.filter(is_active=True)
        self.fields["warehouse"].queryset = Warehouse.objects.filter(is_active=True)
        # INV-006: null purchase_order means an authorised direct receipt.
        self.fields["purchase_order"].queryset = PurchaseOrder.objects.filter(
            status__in=["APPROVED", "PARTIAL", "COMPLETED"]
        ).order_by("-document_date")
        self.fields["purchase_order"].required = False
        self.fields["received_by"].required = False
        _style(self.fields)


class GoodsReceiptLineForm(forms.ModelForm):
    class Meta:
        model = GoodsReceiptLine
        fields = [
            "purchase_order_line",
            "product",
            "description",
            "unit",
            "quantity_received",
            "quantity_rejected",
            "rejection_reason",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["product"].queryset = Product.objects.filter(is_active=True).order_by(
            "sku"
        )
        self.fields["unit"].queryset = UnitOfMeasure.objects.filter(is_active=True)
        self.fields["description"].required = False
        self.fields["quantity_rejected"].required = False
        self.fields["rejection_reason"].required = False
        self.fields["purchase_order_line"].required = False
        self.fields["purchase_order_line"].widget = forms.HiddenInput()
        _style(self.fields)
        for name in ("product", "unit", "quantity_received", "quantity_rejected"):
            self.fields[name].widget.attrs["data-role"] = name

    def clean_quantity_received(self):
        quantity = self.cleaned_data.get("quantity_received")
        if quantity is not None and quantity <= 0:
            raise forms.ValidationError("Quantity received must be greater than zero.")
        return quantity

    def clean(self):
        # PUR-004: the vendor cannot be credited for rejecting more than they
        # delivered — the DB constraint (gr_line_split_sums) would also catch
        # this, but a clear form error beats a 500.
        cleaned = super().clean()
        if cleaned.get("DELETE"):
            return cleaned
        received = cleaned.get("quantity_received")
        rejected = cleaned.get("quantity_rejected") or 0
        if received is not None and rejected > received:
            self.add_error(
                "quantity_rejected", "Cannot reject more than the quantity received."
            )
        return cleaned


#: One receipt, many lines. `min_num=1` mirrors the purchase order formset.
GoodsReceiptLineFormSet = inlineformset_factory(
    GoodsReceipt,
    GoodsReceiptLine,
    form=GoodsReceiptLineForm,
    fk_name="receipt",
    extra=1,
    can_delete=True,
    min_num=1,
    validate_min=True,
)
