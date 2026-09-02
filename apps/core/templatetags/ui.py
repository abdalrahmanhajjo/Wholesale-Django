"""
Template helpers for the shared UI.

`a11y_field` exists because `{{ field }}` renders a widget with whatever attrs
were fixed when the form class was defined, and the things a screen reader needs
— which description belongs to this input, whether it is currently invalid — are
only known once the form is bound. Rendering through this tag keeps the visible
state and the programmatic state from ever disagreeing.
"""

from django import template

register = template.Library()


@register.simple_tag
def a11y_field(field, **overrides):
    """
    Render one bound field with its help text and errors attached.

    Adds `aria-describedby` pointing at the ids `_form_field.html` gives the
    help and error paragraphs, plus `aria-invalid` when the field is in error.
    Extra keyword arguments become widget attributes, so a caller can pass
    `aria_label="Quantity, line 3"` for controls whose only visible label is a
    table column header.
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
