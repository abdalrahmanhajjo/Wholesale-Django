"""
The presentation layer shared by every form in the product.

Before this module, three separate classes each ran their own "set a CSS class on
each widget" loop and nothing else: no placeholders, no keyboard hints, no input
types beyond what Django infers, and no way for the browser to help. Anything
added to one form had to be remembered in the others.

`UIFormMixin` is now the single place those decisions are made. It reads what the
model already knows — the field's type, its `max_length`, its `decimal_places`,
whether it is required — and turns that into the attributes a person actually
benefits from: the right keyboard on a phone, an autofill hint, a placeholder
showing the expected shape, and the rule the live validator enforces.

A form customises it declaratively::

    class CustomerForm(UIFormMixin, forms.ModelForm):
        placeholders = {"code": "CUST-0001"}
        autocomplete_fields = ["currency"]

Nothing here changes what is valid. The server remains the authority; these
attributes only let the browser say the same thing sooner and more kindly.
"""

from django import forms

#: Selects longer than this get a type-to-filter combobox. Below it, the native
#: control is faster and already accessible, so it is left alone.
COMBOBOX_THRESHOLD = 8

FIELD_CLASS = "field"
CHECKBOX_CLASS = (
    "h-4 w-4 shrink-0 rounded border-input-line text-brand-deep "
    "focus:ring-2 focus:ring-ink/30"
)
TEXTAREA_CLASS = (
    "block w-full rounded-xl2 border border-input-line bg-white px-3 py-2 "
    "text-sm text-ink placeholder:text-muted-fg focus:border-ink"
)

#: Autofill hints keyed by the field names this domain actually uses. Getting
#: these right is the difference between a browser filling an address in one tap
#: and the user typing it again.
AUTOCOMPLETE_BY_NAME = {
    "email": "email",
    "phone": "tel",
    "website": "url",
    "name": "organization",
    "legal_name": "organization",
    "tax_id": "off",
    "code": "off",
    "reference": "off",
    "customer_reference": "off",
}

#: Which suggester backs a field, by field name. A select listed here searches
#: the server as you type and shows a second line of context per result, instead
#: of filtering the options already in the page.
SUGGEST_BY_NAME = {
    "customer": "customer",
    "vendor": "vendor",
    "product": "product",
    "warehouse": "warehouse",
    "default_warehouse": "warehouse",
    "money_account": "money_account",
    "currency": "currency",
    "payment_term": "payment_term",
    "tax_code": "tax_code",
    "default_tax_code": "tax_code",
    "sales_order": "sales_order",
}

#: Placeholders that apply wherever the field name appears. A form's own
#: `placeholders` dict overrides these.
PLACEHOLDER_BY_NAME = {
    "email": "name@company.com",
    "phone": "+961 71 234 567",
    "website": "https://company.com",
    "tax_id": "Tax registration number",
    "reference": "Cheque or transfer reference",
    "customer_reference": "The customer's own PO number",
    "narration": "What this payment is for",
    "notes": "Anything the next person should know",
    "internal_notes": "Not shown to the customer",
    "exchange_rate": "1.000000",
    "credit_hold_reason": "Why this customer is on hold",
}


def _is_money(field):
    return isinstance(field, forms.DecimalField) and (field.decimal_places or 0) >= 2


class UIFormMixin:
    """
    Applies the project's field presentation to every widget on the form.

    Subclasses may set:

    ``placeholders``
        ``{field_name: text}``. Explains the expected *shape*; it never repeats
        the label, because a placeholder that repeats the label disappears the
        moment someone types and takes the label with it.
    ``autocomplete_fields``
        Select fields to render as type-to-filter comboboxes regardless of how
        many options they hold.
    ``plain_selects``
        Select fields to leave as native controls regardless of size.
    ``checks``
        ``{field_name: rule}``. The rule is asked of /settings/check/ while the
        user types — a code already taken, a name close to an existing one.
        Only the database can answer these, and only the server enforces them;
        this just says it sooner.
    ``suggest_kinds``
        Override or extend SUGGEST_BY_NAME for this form.
    """

    placeholders = {}
    autocomplete_fields = ()
    plain_selects = ()
    checks = {}
    suggest_kinds = {}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            self._style(name, field)

    # -- one field ---------------------------------------------------------
    def _style(self, name, field):
        widget = field.widget
        attrs = widget.attrs

        if isinstance(widget, forms.CheckboxInput):
            attrs.setdefault("class", CHECKBOX_CLASS)
            return
        if isinstance(widget, forms.Textarea):
            attrs.setdefault("class", TEXTAREA_CLASS)
            attrs.setdefault("rows", 3)
            self._placeholder(name, attrs)
            return

        attrs.setdefault("class", FIELD_CLASS)

        if isinstance(widget, forms.Select):
            self._select(name, field, attrs)
            return

        self._placeholder(name, attrs)
        self._keyboard(name, field, attrs)
        self._validation(name, field, attrs)
        self._check(name, attrs)

        autocomplete = AUTOCOMPLETE_BY_NAME.get(name)
        if autocomplete:
            attrs.setdefault("autocomplete", autocomplete)

    # -- placeholder -------------------------------------------------------
    def _placeholder(self, name, attrs):
        text = self.placeholders.get(name, PLACEHOLDER_BY_NAME.get(name))
        if text:
            attrs.setdefault("placeholder", text)

    # -- keyboard and input mode ------------------------------------------
    def _keyboard(self, name, field, attrs):
        """
        The right keyboard on a phone, and the right parsing on the desktop.

        `inputmode="decimal"` matters more than it looks: a numeric keypad
        without a decimal separator makes a money field unusable on a phone.
        """
        if isinstance(field, forms.EmailField):
            attrs.setdefault("inputmode", "email")
        elif isinstance(field, forms.DecimalField | forms.FloatField):
            attrs.setdefault("inputmode", "decimal")
        elif isinstance(field, forms.IntegerField):
            attrs.setdefault("inputmode", "numeric")
        elif name == "phone":
            attrs.setdefault("inputmode", "tel")
            attrs.setdefault("type", "tel")

    # -- live validation contract -----------------------------------------
    def _validation(self, name, field, attrs):
        """
        Describe the rule to the browser, matching what the server enforces.

        The rule name is what `forms.js` looks up to produce a message; the
        bounds come from the model so the two can never disagree.
        """
        if isinstance(field, forms.EmailField):
            rule = "email"
        elif isinstance(field, forms.URLField):
            rule = "url"
        elif name == "phone":
            rule = "phone"
        elif isinstance(field, forms.DateField):
            rule = "date"
        elif _is_money(field):
            rule = "money"
        elif isinstance(field, forms.DecimalField):
            rule = "decimal"
        elif isinstance(field, forms.IntegerField):
            rule = "integer"
        else:
            rule = "text"
        attrs.setdefault("data-rule", rule)

        if getattr(field, "max_digits", None) and getattr(field, "decimal_places", None):
            attrs.setdefault("data-decimals", str(field.decimal_places))
        for source, target in (("min_value", "data-min"), ("max_value", "data-max")):
            bound = getattr(field, source, None)
            if bound is not None:
                attrs.setdefault(target, str(bound))
        if getattr(field, "max_length", None):
            attrs.setdefault("maxlength", str(field.max_length))

    # -- server-side checks -------------------------------------------------
    def _check(self, name, attrs):
        rule = self.checks.get(name)
        if not rule:
            return
        attrs.setdefault("data-check", rule)
        # Editing a record must not report that record as its own duplicate.
        instance_pk = getattr(getattr(self, "instance", None), "pk", None)
        if instance_pk:
            attrs.setdefault("data-check-exclude", str(instance_pk))

    # -- selects -----------------------------------------------------------
    def _select(self, name, field, attrs):
        """
        Turn long option lists into something searchable.

        A warehouse or account select can run to hundreds of rows; scrolling a
        native dropdown to find one is the slowest interaction in the product.
        Short lists keep the native control, which is faster and already
        accessible on every platform.
        """
        if name in self.plain_selects:
            return
        try:
            option_count = len(field.choices)
        except TypeError:  # a queryset that is not sized without evaluating
            option_count = COMBOBOX_THRESHOLD + 1
        kind = self.suggest_kinds.get(name, SUGGEST_BY_NAME.get(name))
        if name in self.autocomplete_fields or option_count > COMBOBOX_THRESHOLD or kind:
            attrs.setdefault("data-combobox", "")
            attrs.setdefault(
                "data-combobox-placeholder",
                self.placeholders.get(name, f"Search {field.label or name}…".lower()),
            )
            if kind:
                attrs.setdefault("data-suggest", kind)
