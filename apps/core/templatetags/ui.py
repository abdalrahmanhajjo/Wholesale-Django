"""
Template helpers for the shared UI.

`a11y_field` exists because `{{ field }}` renders a widget with whatever attrs
were fixed when the form class was defined, and the things a screen reader needs
— which description belongs to this input, whether it is currently invalid — are
only known once the form is bound. Rendering through this tag keeps the visible
state and the programmatic state from ever disagreeing.
"""

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django import forms, template

register = template.Library()


@register.simple_tag
def a11y_field(field, label=None, line=None, **overrides):
    """
    Render one bound field with its help text and errors attached.

    Adds `aria-describedby` pointing at the ids `_form_field.html` gives the
    help and error paragraphs, plus `aria-invalid` when the field is in error.

    `label` and `line` name a control whose only visible label is a table column
    header::

        {% a11y_field lf.quantity label="Quantity" line=forloop.counter %}

    The tag composes the name rather than the template doing it with `add`,
    because `"Quantity, line "|add:3` returns the empty string — Django's `add`
    tries int() on both sides first, falls back to `+`, and a str plus an int
    raises, so the filter swallows it and yields "". That silently produced
    controls with no accessible name at all, which is the exact failure the
    label was added to fix.

    Any other keyword becomes a widget attribute, with underscores turned into
    hyphens: `data_max=5` sets `data-max="5"`.
    """
    described_by = []
    if field.help_text:
        described_by.append(help_id(field))
    if field.errors:
        described_by.append(error_id(field))

    attrs = {}
    if described_by:
        attrs["aria-describedby"] = " ".join(described_by)
    if field.errors:
        attrs["aria-invalid"] = "true"
    if field.field.required:
        attrs["aria-required"] = "true"

    if label:
        attrs["aria-label"] = f"{label}, line {line}" if line not in (None, "") else str(label)

    for name, value in overrides.items():
        if value not in (None, ""):
            attrs[name.replace("_", "-")] = value

    return field.as_widget(attrs=attrs)


@register.simple_tag
def help_id(field):
    """The id of this field's help paragraph."""
    return f"{field.auto_id}_help"


@register.simple_tag
def error_id(field):
    """The id of this field's error paragraph."""
    return f"{field.auto_id}_error"


@register.simple_tag
def error_count(form, *formsets):
    """
    How many separate errors a submission produced, for the error summary.

    Counted the way a person would: one per failing field plus one per form-wide
    error, across the main form and any inline formsets.
    """
    # form.errors already carries non-field errors under the __all__ key, so
    # counting its values covers both kinds without double counting.
    total = sum(len(errors) for errors in form.errors.values())
    for formset in formsets:
        if not formset:
            continue
        total += len(formset.non_form_errors())
        for inline in formset.forms:
            total += sum(len(errors) for errors in inline.errors.values())
    return total


@register.filter
def money(value, currency=""):
    """
    Format an amount the way a ledger prints it.

    `{{ payment.amount_txn|money:payment.currency_id }}` → ``USD 1,234,567.00``

    Grouping is not decoration. Without it, 1234567.00 and 123456.00 are
    distinguished by counting characters, which is exactly the mistake an
    accounting screen should not invite. Two decimal places always, so a column
    of figures aligns on the point.
    """
    if value is None or value == "":
        return "—"
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return value
    quantised = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    formatted = f"{quantised:,.2f}"
    return f"{currency} {formatted}".strip() if currency else formatted


@register.filter
def quantity(value):
    """A count or a weight: grouped, but without forcing money's two decimals."""
    if value is None or value == "":
        return "—"
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return value
    normalised = amount.normalize()
    # normalize() renders a whole number in exponent form — Decimal("1000")
    # becomes 1E+3 — which formats as the literal "1E+3". Go through int.
    if normalised == normalised.to_integral_value():
        return f"{int(normalised):,}"
    return f"{normalised:,f}"


@register.filter
def is_floatable(field):
    """
    Whether a floating label suits this control.

    A date input renders its own placeholder text and a picker button, a select
    always shows a value, and a checkbox has no interior — a label floating over
    any of them collides with what the browser draws. Text-like inputs are the
    only ones where the pattern works.
    """
    widget = field.field.widget
    if isinstance(widget, forms.CheckboxInput | forms.Select | forms.RadioSelect):
        return False
    if getattr(widget, "input_type", "") in {
        "date",
        "time",
        "datetime-local",
        "color",
        "file",
    }:
        return False
    return True
