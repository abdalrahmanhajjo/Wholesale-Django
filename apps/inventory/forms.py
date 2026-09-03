"""
Goods receipt, stock transfer and stock adjustment forms (PUR-003, PUR-004,
INV-008, INV-009).

Costs are never taken from the form as final figures — they are derived by
the matching `apps.inventory.services.recalculate_*` after the formset is
saved (goods receipts also derive the accept/reject split there), mirroring
apps/purchases/forms.py.
"""

from decimal import Decimal

from django import forms
from django.forms import inlineformset_factory

from apps.catalog.models import Product, UnitOfMeasure
from apps.inventory.models import (
    AdjustmentReason,
    GoodsReceipt,
    GoodsReceiptLine,
    StockAdjustment,
    StockAdjustmentLine,
    StockTransfer,
    StockTransferLine,
    Warehouse,
)
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


# ---------------------------------------------------------------------------
# Stock transfer (INV-008)
# ---------------------------------------------------------------------------
class StockTransferForm(forms.ModelForm):
    class Meta:
        model = StockTransfer
        fields = ["from_warehouse", "to_warehouse", "document_date", "reason", "notes"]
        widgets = {
            "document_date": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["from_warehouse"].queryset = Warehouse.objects.filter(is_active=True)
        self.fields["to_warehouse"].queryset = Warehouse.objects.filter(is_active=True)
        self.fields["reason"].required = False
        _style(self.fields)

    def clean(self):
        cleaned = super().clean()
        from_warehouse = cleaned.get("from_warehouse")
        to_warehouse = cleaned.get("to_warehouse")
        if from_warehouse and to_warehouse and from_warehouse == to_warehouse:
            self.add_error("to_warehouse", "Choose a different warehouse to transfer into.")
        return cleaned


class StockTransferLineForm(forms.ModelForm):
    class Meta:
        model = StockTransferLine
        fields = ["product", "quantity"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["product"].queryset = Product.objects.filter(is_active=True).order_by(
            "sku"
        )
        _style(self.fields)
        for name in ("product", "quantity"):
            self.fields[name].widget.attrs["data-role"] = name

    def clean_quantity(self):
        quantity = self.cleaned_data.get("quantity")
        if quantity is not None and quantity <= 0:
            raise forms.ValidationError("Quantity must be greater than zero.")
        return quantity


StockTransferLineFormSet = inlineformset_factory(
    StockTransfer,
    StockTransferLine,
    form=StockTransferLineForm,
    fk_name="transfer",
    extra=1,
    can_delete=True,
    min_num=1,
    validate_min=True,
)


# ---------------------------------------------------------------------------
# Stock adjustment (INV-009)
# ---------------------------------------------------------------------------
class StockAdjustmentForm(forms.ModelForm):
    class Meta:
        model = StockAdjustment
        fields = ["warehouse", "reason", "document_date", "narration", "attachment_reference"]
        widgets = {
            "document_date": forms.DateInput(attrs={"type": "date"}),
            "narration": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["warehouse"].queryset = Warehouse.objects.filter(is_active=True)
        self.fields["reason"].queryset = AdjustmentReason.objects.filter(is_active=True)
        self.fields["attachment_reference"].required = False
        _style(self.fields)


class StockAdjustmentLineForm(forms.ModelForm):
    class Meta:
        model = StockAdjustmentLine
        fields = ["product", "quantity_delta", "unit_cost", "note"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["product"].queryset = Product.objects.filter(is_active=True).order_by(
            "sku"
        )
        self.fields["unit_cost"].required = False
        self.fields["note"].required = False
        _style(self.fields)
        for name in ("product", "quantity_delta"):
            self.fields[name].widget.attrs["data-role"] = name

    def clean_quantity_delta(self):
        delta = self.cleaned_data.get("quantity_delta")
        if delta == 0:
            raise forms.ValidationError("Quantity change cannot be zero.")
        return delta

    def clean_unit_cost(self):
        # unit_cost isn't required (a decrease line doesn't need one — the
        # engine costs it from the live average at posting time), but the
        # column itself isn't nullable, so a blank input becomes zero rather
        # than None.
        return self.cleaned_data.get("unit_cost") or Decimal("0")


StockAdjustmentLineFormSet = inlineformset_factory(
    StockAdjustment,
    StockAdjustmentLine,
    form=StockAdjustmentLineForm,
    fk_name="adjustment",
    extra=1,
    can_delete=True,
    min_num=1,
    validate_min=True,
)


class StockAdjustmentRejectForm(forms.Form):
    """Mirrors PurchaseOrderRejectForm — a rejection must say why (ACC-008)."""

    reason = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 2, "class": "field"}),
        label="Reason for rejection",
    )
