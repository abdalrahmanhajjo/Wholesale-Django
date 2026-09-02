"""
Sales-order entry forms (SAL-001..SAL-004).

The form follows the PartyFormMixin pattern: a ModelForm for the header, a
formset for lines, with the same widget-styling loop so every control looks
identical to CustomerForm.
"""

from django import forms
from django.forms import inlineformset_factory

from apps.sales.models import DiscountKind, SalesOrder, SalesOrderLine


class SalesOrderForm(forms.ModelForm):
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
        for fld in self.fields.values():
            widget = fld.widget
            if isinstance(widget, forms.CheckboxInput):
                widget.attrs.setdefault(
                    "class",
                    "h-4 w-4 rounded border-line text-brand focus:ring-brand/30",
                )
            elif isinstance(widget, forms.Textarea):
                widget.attrs.setdefault(
                    "class",
                    "block w-full rounded-xl2 border border-line bg-white px-3 py-2 text-sm "
                    "focus:border-brand focus:ring-2 focus:ring-brand/30 focus:outline-none",
                )
            else:
                widget.attrs.setdefault("class", "field")

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


class SalesOrderLineForm(forms.ModelForm):
    """One server-validated sales-order line in the inline formset."""

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
        for fld in self.fields.values():
            widget = fld.widget
            if isinstance(widget, forms.CheckboxInput):
                widget.attrs.setdefault(
                    "class",
                    "h-4 w-4 rounded border-line text-brand focus:ring-brand/30",
                )
            else:
                widget.attrs.setdefault("class", "field")
        # Only active products
        self.fields["product"].queryset = self.fields["product"].queryset.filter(
            is_active=True
        )
        self.fields["tax_code"].queryset = self.fields["tax_code"].queryset.filter(
            is_active=True
        )


# Ten lines keeps the first entry screen focused and bounds one request's work.
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
