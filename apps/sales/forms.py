"""
Sales-order entry forms (SAL-001..SAL-004).

The form follows the PartyFormMixin pattern: a ModelForm for the header, a
formset for lines, with the same widget-styling loop so every control looks
identical to CustomerForm.
"""

from django import forms
from django.core.exceptions import ValidationError
from django.forms import formset_factory, inlineformset_factory

from apps.catalog.models import Product, UnitOfMeasure
from apps.core.form_ui import UIFormMixin
from apps.core.models import DocumentStatus, TaxCode
from apps.inventory.models import DeliveryNote, DeliveryNoteLine
from apps.sales.models import DiscountKind, SalesInvoice, SalesOrder, SalesOrderLine
from apps.sales.services import (
    remaining_to_deliver,
    remaining_to_invoice,
)


class SalesOrderForm(UIFormMixin, forms.ModelForm):
    placeholders = {
        "customer_reference": "The customer's own PO number",
        "exchange_rate": "1.000000",
        "document_discount_value": "0.00",
        "billing_address_text": "Street, city, country",
        "shipping_address_text": "Leave blank to use the billing address",
        "notes": "Shown on the printed order",
        "internal_notes": "Not shown to the customer",
    }
    autocomplete_fields = ["customer", "warehouse", "salesperson", "payment_term"]
    plain_selects = ["document_discount_kind", "currency"]

    class Meta:
        model = SalesOrder
        fields = [
            "customer",
            "warehouse",
            "document_date",
            "posting_date",
            "due_date",
            "currency",
            "exchange_rate",
            "payment_term",
            "expected_date",
            "customer_reference",
            "billing_address_text",
            "shipping_address_text",
            "salesperson",
            "document_discount_kind",
            "document_discount_value",
            "notes",
            "internal_notes",
        ]
        widgets = {
            "document_date": forms.DateInput(attrs={"type": "date"}),
            "posting_date": forms.DateInput(attrs={"type": "date"}),
            "due_date": forms.DateInput(attrs={"type": "date"}),
            "expected_date": forms.DateInput(attrs={"type": "date"}),
            "billing_address_text": forms.Textarea(attrs={"rows": 2}),
            "shipping_address_text": forms.Textarea(attrs={"rows": 2}),
            "notes": forms.Textarea(attrs={"rows": 2}),
            "internal_notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Same widget-styling loop as CustomerForm (UX-002)

        # Only active, non-archived records
        self.fields["customer"].queryset = self.fields["customer"].queryset.filter(
            is_active=True
        )
        self.fields["warehouse"].queryset = self.fields["warehouse"].queryset.filter(
            is_active=True
        )
        self.fields["payment_term"].queryset = self.fields["payment_term"].queryset.filter(
            is_active=True
        )

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("document_discount_kind") == DiscountKind.PERCENT:
            val = cleaned.get("document_discount_value") or 0
            if val < 0 or val > 100:
                self.add_error(
                    "document_discount_value",
                    "Percentage discount must be between 0 and 100.",
                )
        return cleaned


class SalesOrderLineForm(UIFormMixin, forms.ModelForm):
    """One line in the formset. Product is chosen; price/tax auto-populate via JS."""

    placeholders = {
        "description": "Overrides the product name",
        "quantity": "0",
        "unit_price": "0.00",
        "discount_percent": "0",
    }

    class Meta:
        model = SalesOrderLine
        fields = [
            "line_no",
            "product",
            "description",
            "unit",
            "quantity",
            "unit_price",
            "discount_percent",
            "tax_code",
            "warehouse",
        ]
        widgets = {
            "line_no": forms.HiddenInput(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # line_no is assigned automatically by the view at save time, so the
        # user never types it and it must not block validation.
        self.fields["line_no"].required = False
        # Only active products
        self.fields["product"].queryset = self.fields["product"].queryset.filter(
            is_active=True
        )
        self.fields["tax_code"].queryset = self.fields["tax_code"].queryset.filter(
            is_active=True
        )

    def clean(self):
        """
        Say what is wrong in words, before the database says it in SQL.

        These bounds are enforced by check constraints — so_line_qty_positive,
        so_line_price_nonneg, so_line_discount_range — which is right, because a
        constraint is the only guarantee that holds for every writer. But when a
        person trips one through this form, Django reports it as
        'Constraint "so_line_discount_range" is violated', which names an
        implementation detail and says nothing about what to do. Validating here
        means the constraint stays as the backstop it should be, and the person
        gets a sentence.
        """
        cleaned = super().clean()
        quantity = cleaned.get("quantity")
        price = cleaned.get("unit_price")
        discount = cleaned.get("discount_percent")

        if quantity is not None and quantity <= 0:
            self.add_error("quantity", "Enter a quantity greater than zero.")
        if price is not None and price < 0:
            self.add_error("unit_price", "A unit price cannot be negative.")
        if discount is not None and not (0 <= discount <= 100):
            self.add_error(
                "discount_percent",
                "A line discount is a percentage, so it has to be between 0 and 100.",
            )
        return cleaned


# 10 lines max for SO (can adjust)
#: The screen says "Up to 10 lines per order" and the add-line script stops at
#: ten, but a limit the browser alone enforces is not a limit — the form is a
#: plain POST and TOTAL_FORMS is whatever the client sends. validate_max makes
#: the server the authority, which is where it has to be.
SalesOrderLineFormSet = inlineformset_factory(
    SalesOrder,
    SalesOrderLine,
    form=SalesOrderLineForm,
    extra=1,
    can_delete=True,
    max_num=10,
    validate_max=True,
    min_num=0,
    validate_min=False,
)


# ---------------------------------------------------------------------------
# Delivery notes (SAL-005, INV-007)
# ---------------------------------------------------------------------------
class DeliveryNoteForm(UIFormMixin, forms.ModelForm):
    """
    Header of a delivery note. `customer` and `sales_order` arrive from the
    order selected on the create flow, so they are hidden here. The note is
    created as DRAFT and posted separately (warehouse double-check).

    """

    placeholders = {
        "reference": "Your own delivery reference",
        "carrier": "Courier or driver",
        "tracking_reference": "Consignment or waybill number",
    }
    autocomplete_fields = ["customer", "sales_order", "warehouse"]

    class Meta:
        model = DeliveryNote
        fields = [
            "customer",
            "sales_order",
            "warehouse",
            "document_date",
            "reference",
            "carrier",
            "tracking_reference",
            "shipping_address_text",
            "notes",
        ]
        widgets = {
            "customer": forms.HiddenInput(),
            "sales_order": forms.HiddenInput(),
            "document_date": forms.DateInput(attrs={"type": "date"}),
            "shipping_address_text": forms.Textarea(attrs={"rows": 2}),
            "notes": forms.Textarea(attrs={"rows": 2}),
            "carrier": forms.TextInput(attrs={"placeholder": "Carrier (optional)"}),
            "tracking_reference": forms.TextInput(
                attrs={"placeholder": "Tracking (optional)"}
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["warehouse"].queryset = self.fields["warehouse"].queryset.filter(
            is_active=True
        )


class DeliveryLineForm(UIFormMixin, forms.Form):
    """One editable row in the create-from-order flow.

    Only the quantity is entered here; product and order-line are carried
    hidden and rendered read-only from the order line the view attaches.
    """

    sales_order_line = forms.IntegerField(widget=forms.HiddenInput())
    quantity = forms.DecimalField(
        max_digits=18,
        decimal_places=4,
        min_value=0,
        widget=forms.NumberInput(
            attrs={
                "min": "0",
                "step": "0.0001",
                "class": "field block w-28 text-right tabular-nums",
            }
        ),
    )

    def clean_quantity(self):
        qty = self.cleaned_data["quantity"]
        so_line = getattr(self, "so_line", None)
        if so_line is not None and qty > remaining_to_deliver(so_line):
            raise ValidationError(
                f"Only {remaining_to_deliver(so_line)} remains to deliver "
                f"({so_line.product}) — over-delivery is blocked (SAL-005)."
            )
        return qty


# Rows are added by the view for each order line with remaining quantity.
DeliveryLineFormSet = formset_factory(
    DeliveryLineForm,
    formset=forms.BaseFormSet,
    extra=0,
)


class InvoiceSourceForm(forms.Form):
    """Step 1 of create: where does this invoice come from?"""

    SOURCE_ORDER = "ORDER"
    SOURCE_DELIVERY = "DELIVERY"
    SOURCE_DIRECT = "DIRECT"
    source = forms.ChoiceField(
        choices=[
            (SOURCE_DELIVERY, "From a delivery note"),
            (SOURCE_ORDER, "From a sales order"),
            (SOURCE_DIRECT, "Direct invoice"),
        ],
        widget=forms.Select(attrs={"class": "field"}),
    )


class DeliverySelectForm(forms.Form):
    """Pick a POSTED delivery (like your DeliveryOrderSelectForm)."""

    delivery = forms.ModelChoiceField(
        queryset=DeliveryNote.objects.filter(status=DocumentStatus.POSTED),
        empty_label="Select a posted delivery…",
        widget=forms.Select(attrs={"class": "field"}),
    )


class SalesInvoiceForm(UIFormMixin, forms.ModelForm):
    placeholders = {
        "reference": "Your own invoice reference",
        "notes": "Shown on the printed invoice",
    }
    autocomplete_fields = ["customer", "sales_order", "payment_term"]

    class Meta:
        model = SalesInvoice
        fields = [
            "customer",
            "warehouse",
            "sales_order",
            "payment_term",
            "document_date",
            "posting_date",
            "due_date",
            "customer_reference",
            "billing_address_text",
            "shipping_address_text",
            "notes",
        ]
        widgets = {
            "customer": forms.HiddenInput(),
            "sales_order": forms.HiddenInput(),
            "document_date": forms.DateInput(attrs={"type": "date"}),
            "posting_date": forms.DateInput(attrs={"type": "date"}),
            "due_date": forms.DateInput(attrs={"type": "date"}),
            "billing_address_text": forms.Textarea(attrs={"rows": 2}),
            "shipping_address_text": forms.Textarea(attrs={"rows": 2}),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for fld in self.fields.values():
            fld.widget.attrs.setdefault(
                "class",
                "block w-full rounded-xl2 border border-line bg-white px-3 py-2 "
                "text-sm focus:border-brand focus:ring-2 focus:ring-brand/30 "
                "focus:outline-none",
            )
        self.fields["payment_term"].queryset = self.fields["payment_term"].queryset.filter(
            is_active=True
        )


class SalesInvoiceLineForm(UIFormMixin, forms.Form):
    """One editable line; product/qty/price are entered, delivery_line hidden."""

    delivery_line = forms.IntegerField(widget=forms.HiddenInput(), required=False)
    product = forms.ModelChoiceField(
        queryset=Product.objects.filter(is_active=True),
        widget=forms.Select(attrs={"class": "field"}),
    )
    unit = forms.ModelChoiceField(
        queryset=UnitOfMeasure.objects.all(), widget=forms.HiddenInput(), required=False
    )
    quantity = forms.DecimalField(
        min_value=0,
        max_digits=18,
        decimal_places=4,
        widget=forms.NumberInput(attrs={"step": "0.0001", "class": "field"}),
    )
    unit_price = forms.DecimalField(
        min_value=0,
        max_digits=18,
        decimal_places=4,
        widget=forms.NumberInput(attrs={"step": "0.0001", "class": "field"}),
    )
    discount_percent = forms.DecimalField(
        required=False,
        min_value=0,
        max_value=100,
        initial=0,
        max_digits=9,
        decimal_places=4,
        widget=forms.NumberInput(attrs={"class": "field"}),
    )
    tax_code = forms.ModelChoiceField(
        queryset=TaxCode.objects.filter(is_active=True),
        required=False,
        widget=forms.Select(attrs={"class": "field"}),
    )

    def clean_quantity(self):
        qty = self.cleaned_data["quantity"]
        dl_id = self.cleaned_data.get("delivery_line")
        if dl_id:
            dl = DeliveryNoteLine.objects.get(pk=dl_id)
            if qty > remaining_to_invoice(dl):
                raise ValidationError(
                    f"Only {remaining_to_invoice(dl)} remains to invoice for "
                    f"{dl.product} (SAL-006)."
                )
        return qty


SalesInvoiceLineFormSet = formset_factory(SalesInvoiceLineForm, extra=1, can_delete=True)
