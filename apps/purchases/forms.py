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
from apps.core.models import Company, DocumentStatus, TaxCode
from apps.inventory.models import Warehouse
from apps.ledger.models import Account
from apps.parties.models import Vendor
from apps.purchases.models import (
    PurchaseBill,
    PurchaseBillLine,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseReturn,
    PurchaseReturnLine,
    VendorDebitNote,
    VendorDebitNoteLine,
)


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


# ---------------------------------------------------------------------------
# Purchase bill (PUR-005..PUR-008)
# ---------------------------------------------------------------------------
class PurchaseBillForm(forms.ModelForm):
    class Meta:
        model = PurchaseBill
        fields = [
            "vendor",
            "purchase_order",
            "goods_receipt",
            "warehouse",
            "vendor_invoice_number",
            "vendor_invoice_date",
            "document_date",
            "due_date",
            "currency",
            "exchange_rate",
            "payment_term",
            "billing_address_text",
            "document_discount_kind",
            "document_discount_value",
            "notes",
        ]
        widgets = {
            "document_date": forms.DateInput(attrs={"type": "date"}),
            "vendor_invoice_date": forms.DateInput(attrs={"type": "date"}),
            "due_date": forms.DateInput(attrs={"type": "date"}),
            "billing_address_text": forms.Textarea(attrs={"rows": 3}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["vendor"].queryset = Vendor.objects.filter(is_active=True)
        self.fields["warehouse"].queryset = Warehouse.objects.filter(is_active=True)
        self.fields["warehouse"].required = False
        self.fields["purchase_order"].queryset = PurchaseOrder.objects.filter(
            status__in=["APPROVED", "PARTIAL", "COMPLETED"]
        ).order_by("-document_date")
        self.fields["purchase_order"].required = False
        self.fields["goods_receipt"].required = False
        self.fields["document_discount_value"].required = False
        _style(self.fields)

    def clean_document_discount_value(self):
        return self.cleaned_data.get("document_discount_value") or Decimal("0")

    def clean_vendor_invoice_number(self):
        return (self.cleaned_data.get("vendor_invoice_number") or "").strip()

    def clean(self):
        # PUR-006: a vendor invoice number cannot be billed twice. The database
        # also enforces this (pb_vendor_invoice_unique) — this check exists so
        # the clerk gets a clear message on the form instead of a server error.
        cleaned = super().clean()
        vendor = cleaned.get("vendor")
        invoice_number = cleaned.get("vendor_invoice_number")
        if vendor and invoice_number:
            company = Company.objects.first()
            if company is None or company.block_duplicate_vendor_invoice:
                duplicate = PurchaseBill.objects.filter(
                    vendor=vendor, vendor_invoice_number=invoice_number
                )
                if self.instance.pk:
                    duplicate = duplicate.exclude(pk=self.instance.pk)
                existing = duplicate.first()
                if existing is not None:
                    self.add_error(
                        "vendor_invoice_number",
                        f"Invoice {invoice_number} for {vendor} was already billed as "
                        f"{existing.number} (PUR-006). Check for a duplicate entry.",
                    )
        return cleaned

    def save(self, commit=True):
        bill = super().save(commit=False)
        if bill.purchase_order_id and not bill.payment_term_id:
            bill.payment_term = bill.purchase_order.payment_term
        # The bill posts on its own document date; an accountant can move the
        # journal to a different open period later by editing posting_date
        # directly (out of scope here — this is the DRAFT-time default).
        bill.posting_date = bill.document_date
        if commit:
            bill.save()
        return bill


class PurchaseBillLineForm(forms.ModelForm):
    class Meta:
        model = PurchaseBillLine
        fields = [
            "purchase_order_line",
            "receipt_line",
            "is_stock_line",
            "product",
            "expense_account",
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
        self.fields["product"].required = False
        self.fields["unit"].queryset = UnitOfMeasure.objects.filter(is_active=True)
        self.fields["unit"].required = False
        self.fields["warehouse"].queryset = Warehouse.objects.filter(is_active=True)
        self.fields["warehouse"].required = False
        self.fields["tax_code"].queryset = TaxCode.objects.filter(
            is_active=True, applies_to__in=["PURCHASE", "BOTH"]
        )
        self.fields["tax_code"].required = False
        self.fields["discount_percent"].required = False
        self.fields["description"].required = False
        self.fields["expense_account"].queryset = Account.objects.filter(
            is_postable=True, is_active=True, account_type="EXPENSE"
        )
        self.fields["expense_account"].required = False
        self.fields["purchase_order_line"].required = False
        self.fields["purchase_order_line"].widget = forms.HiddenInput()
        self.fields["receipt_line"].required = False
        self.fields["receipt_line"].widget = forms.HiddenInput()
        _style(self.fields)
        for name in (
            "is_stock_line",
            "product",
            "quantity",
            "unit_price",
            "discount_percent",
            "tax_code",
        ):
            self.fields[name].widget.attrs["data-role"] = name

    def clean_discount_percent(self):
        return self.cleaned_data.get("discount_percent") or Decimal("0")

    def clean_quantity(self):
        quantity = self.cleaned_data.get("quantity")
        if quantity is not None and quantity <= 0:
            raise forms.ValidationError("Quantity must be greater than zero.")
        return quantity

    def clean(self):
        # Appendix A: a stock line hits Inventory through its product, a
        # non-stock line hits an expense account — never neither.
        cleaned = super().clean()
        if cleaned.get("DELETE"):
            return cleaned
        is_stock_line = cleaned.get("is_stock_line", True)
        if is_stock_line and not cleaned.get("product"):
            self.add_error("product", "A stock line needs a product.")
        if not is_stock_line and not cleaned.get("expense_account"):
            self.add_error("expense_account", "A non-stock line needs an expense account.")
        return cleaned


#: One bill, many lines. `min_num=1` mirrors the purchase order — a bill with
#: no lines has nothing to post (PUR-008).
PurchaseBillLineFormSet = inlineformset_factory(
    PurchaseBill,
    PurchaseBillLine,
    form=PurchaseBillLineForm,
    fk_name="bill",
    extra=1,
    can_delete=True,
    min_num=1,
    validate_min=True,
)


# ---------------------------------------------------------------------------
# Purchase return (RET-005, RET-008)
# ---------------------------------------------------------------------------
class PurchaseReturnForm(forms.ModelForm):
    class Meta:
        model = PurchaseReturn
        fields = [
            "vendor",
            "warehouse",
            "original_bill",
            "original_receipt",
            "document_date",
            "reason",
        ]
        widgets = {
            "document_date": forms.DateInput(attrs={"type": "date"}),
            "reason": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["vendor"].queryset = Vendor.objects.filter(is_active=True)
        self.fields["warehouse"].queryset = Warehouse.objects.filter(is_active=True)
        self.fields["original_bill"].queryset = PurchaseBill.objects.filter(
            status__in=["POSTED", "PARTIAL", "COMPLETED"]
        ).order_by("-document_date")
        self.fields["original_bill"].required = False
        self.fields["original_receipt"].required = False
        _style(self.fields)

    def clean_reason(self):
        # purchase_return_reason_required (RET-008) — a clear form error beats
        # the database's blank-string CHECK constraint.
        reason = (self.cleaned_data.get("reason") or "").strip()
        if not reason:
            raise forms.ValidationError("A reason is required for a vendor return.")
        return reason

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("original_bill") and not cleaned.get("original_receipt"):
            self.add_error(
                "original_bill",
                "Pick the bill or the goods receipt this return is against.",
            )
        return cleaned


class PurchaseReturnLineForm(forms.ModelForm):
    class Meta:
        model = PurchaseReturnLine
        fields = [
            "bill_line",
            "receipt_line",
            "product",
            "quantity",
            "disposition",
            "note",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["product"].queryset = Product.objects.filter(is_active=True).order_by(
            "sku"
        )
        self.fields["note"].required = False
        self.fields["bill_line"].required = False
        self.fields["bill_line"].widget = forms.HiddenInput()
        self.fields["receipt_line"].required = False
        self.fields["receipt_line"].widget = forms.HiddenInput()
        _style(self.fields)
        for name in ("product", "quantity", "disposition"):
            self.fields[name].widget.attrs["data-role"] = name

    def clean_quantity(self):
        quantity = self.cleaned_data.get("quantity")
        if quantity is not None and quantity <= 0:
            raise forms.ValidationError("Quantity must be greater than zero.")
        return quantity


PurchaseReturnLineFormSet = inlineformset_factory(
    PurchaseReturn,
    PurchaseReturnLine,
    form=PurchaseReturnLineForm,
    fk_name="purchase_return",
    extra=1,
    can_delete=True,
    min_num=1,
    validate_min=True,
)


# ---------------------------------------------------------------------------
# Vendor debit note (RET-006, RET-007, RET-008)
# ---------------------------------------------------------------------------
class VendorDebitNoteForm(forms.ModelForm):
    class Meta:
        model = VendorDebitNote
        fields = [
            "vendor",
            "original_bill",
            "purchase_return",
            "vendor_credit_reference",
            "document_date",
            "currency",
            "exchange_rate",
            "reason",
            "notes",
        ]
        widgets = {
            "document_date": forms.DateInput(attrs={"type": "date"}),
            "reason": forms.Textarea(attrs={"rows": 3}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["vendor"].queryset = Vendor.objects.filter(is_active=True)
        self.fields["original_bill"].queryset = PurchaseBill.objects.filter(
            status__in=["POSTED", "PARTIAL", "COMPLETED"]
        ).order_by("-document_date")
        self.fields["original_bill"].required = False
        self.fields["purchase_return"].queryset = PurchaseReturn.objects.filter(
            status=DocumentStatus.POSTED
        ).order_by("-document_date")
        self.fields["purchase_return"].required = False
        self.fields["vendor_credit_reference"].required = False
        _style(self.fields)

    def clean_reason(self):
        reason = (self.cleaned_data.get("reason") or "").strip()
        if not reason:
            raise forms.ValidationError("A reason is required for a vendor debit note.")
        return reason

    def save(self, commit=True):
        note = super().save(commit=False)
        note.posting_date = note.document_date
        if commit:
            note.save()
        return note


class VendorDebitNoteLineForm(forms.ModelForm):
    class Meta:
        model = VendorDebitNoteLine
        fields = [
            "bill_line",
            "return_line",
            "is_stock_line",
            "product",
            "expense_account",
            "description",
            "unit",
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
        self.fields["product"].required = False
        self.fields["unit"].queryset = UnitOfMeasure.objects.filter(is_active=True)
        self.fields["unit"].required = False
        self.fields["tax_code"].queryset = TaxCode.objects.filter(
            is_active=True, applies_to__in=["PURCHASE", "BOTH"]
        )
        self.fields["tax_code"].required = False
        self.fields["discount_percent"].required = False
        self.fields["description"].required = False
        self.fields["expense_account"].queryset = Account.objects.filter(
            is_postable=True, is_active=True, account_type="EXPENSE"
        )
        self.fields["expense_account"].required = False
        self.fields["bill_line"].required = False
        self.fields["bill_line"].widget = forms.HiddenInput()
        self.fields["return_line"].required = False
        self.fields["return_line"].widget = forms.HiddenInput()
        _style(self.fields)
        for name in (
            "is_stock_line",
            "product",
            "quantity",
            "unit_price",
            "discount_percent",
            "tax_code",
        ):
            self.fields[name].widget.attrs["data-role"] = name

    def clean_discount_percent(self):
        return self.cleaned_data.get("discount_percent") or Decimal("0")

    def clean_quantity(self):
        quantity = self.cleaned_data.get("quantity")
        if quantity is not None and quantity <= 0:
            raise forms.ValidationError("Quantity must be greater than zero.")
        return quantity

    def clean_unit_price(self):
        # A zero-priced line has nothing to credit — post_vendor_debit_note
        # would otherwise try to write a journal line with both debit and
        # credit at zero, which the database rejects (journal_line_debit_xor_credit).
        # Catch it here with a clear message instead of that 500.
        unit_price = self.cleaned_data.get("unit_price")
        if not unit_price:
            raise forms.ValidationError("Unit price must be greater than zero.")
        return unit_price

    def clean(self):
        # Appendix A split, same as a bill line for the stock side: a stock
        # line needs a product. Unlike a bill line, a non-stock line does not
        # require picking an expense account — leaving it blank credits the
        # dedicated Purchase Returns contra account instead (services.py),
        # which is the sensible default for a credit rather than a spend.
        cleaned = super().clean()
        if cleaned.get("DELETE"):
            return cleaned
        is_stock_line = cleaned.get("is_stock_line", True)
        if is_stock_line and not cleaned.get("product"):
            self.add_error("product", "A stock line needs a product.")
        return cleaned


VendorDebitNoteLineFormSet = inlineformset_factory(
    VendorDebitNote,
    VendorDebitNoteLine,
    form=VendorDebitNoteLineForm,
    fk_name="debit_note",
    extra=1,
    can_delete=True,
    min_num=1,
    validate_min=True,
)
