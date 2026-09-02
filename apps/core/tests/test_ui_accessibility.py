"""
Accessibility guarantees of the shared UI partials.

These are the wiring rules a screen reader depends on and that a careless edit
to a template silently breaks: an input that no longer points at its error, a
required field that stops announcing itself, an error summary that disappears.
No database is touched — this is template behaviour only.
"""

from django import forms
from django.template.loader import get_template, render_to_string
from django.test import SimpleTestCase


class DemoForm(forms.Form):
    code = forms.CharField(label="Code", help_text="Short unique identifier.")
    note = forms.CharField(label="Note", required=False)
    active = forms.BooleanField(label="Active", required=False)


def render_field(form, name):
    return render_to_string("core/_form_field.html", {"field": form[name]})


class TemplatesCompileTests(SimpleTestCase):
    def test_every_template_compiles(self):
        """A template with a bad tag fails at render time, in production."""
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[3] / "templates"
        names = sorted(str(p.relative_to(root)) for p in root.rglob("*.html"))
        self.assertGreater(len(names), 15)
        for name in names:
            with self.subTest(template=name):
                get_template(name)


class FormFieldAccessibilityTests(SimpleTestCase):
    def setUp(self):
        self.invalid = DemoForm(data={"code": ""})
        self.invalid.is_valid()

    def test_error_and_help_are_announced_with_the_input(self):
        html = render_field(self.invalid, "code")
        self.assertIn('id="id_code_help"', html)
        self.assertIn('id="id_code_error"', html)
        self.assertIn('aria-describedby="id_code_help id_code_error"', html)

    def test_invalid_field_is_marked_invalid(self):
        self.assertIn('aria-invalid="true"', render_field(self.invalid, "code"))

    def test_valid_field_is_not_marked_invalid(self):
        self.assertNotIn("aria-invalid", render_field(self.invalid, "note"))

    def test_required_state_is_programmatic_not_only_an_asterisk(self):
        """A `*` hidden from assistive technology tells a screen reader nothing."""
        self.assertIn('aria-required="true"', render_field(self.invalid, "code"))

    def test_optional_fields_say_so_in_the_label(self):
        html = render_field(self.invalid, "note")
        self.assertIn("(optional)", html)
        self.assertNotIn("aria-required", html)

    def test_checkbox_label_points_at_its_input(self):
        self.assertIn('for="id_active"', render_field(self.invalid, "active"))

    def test_error_text_carries_a_textual_prefix(self):
        """Colour alone must not be what marks the message as an error."""
        self.assertIn("Error: ", render_field(self.invalid, "code"))


class ErrorSummaryTests(SimpleTestCase):
    def summary(self, form):
        return render_to_string("core/_form_errors.html", {"form": form})

    def test_nothing_renders_for_an_unbound_form(self):
        self.assertEqual(self.summary(DemoForm()).strip(), "")

    def test_summary_is_an_alert_and_can_take_focus(self):
        form = DemoForm(data={"code": ""})
        form.is_valid()
        html = self.summary(form)
        self.assertIn('role="alert"', html)
        self.assertIn('tabindex="-1"', html)
        self.assertIn('id="form-error-summary"', html)

    def test_summary_counts_and_links_to_each_failing_field(self):
        form = DemoForm(data={"code": ""})
        form.is_valid()
        html = self.summary(form)
        self.assertIn("There is 1 problem", html)
        self.assertIn('href="#id_code"', html)

    def test_summary_says_the_entry_was_not_lost(self):
        form = DemoForm(data={"code": ""})
        form.is_valid()
        self.assertIn("still here", self.summary(form))


class FormsetErrorSummaryTests(SimpleTestCase):
    """A bound formset reports empty error dicts, which are truthy."""

    def formset_class(self):
        from django.forms import formset_factory

        class LineForm(forms.Form):
            qty = forms.IntegerField(label="Qty")

        return formset_factory(LineForm, extra=1, max_num=2, validate_max=True)

    def test_valid_formset_produces_no_summary(self):
        formset = self.formset_class()(
            data={
                "form-TOTAL_FORMS": "1",
                "form-INITIAL_FORMS": "0",
                "form-MIN_NUM_FORMS": "0",
                "form-MAX_NUM_FORMS": "2",
                "form-0-qty": "3",
            }
        )
        self.assertTrue(formset.is_valid())
        html = render_to_string(
            "core/_form_errors.html", {"form": DemoForm(), "formset": formset}
        )
        self.assertEqual(html.strip(), "")

    def test_line_errors_are_listed_with_their_line_number(self):
        formset = self.formset_class()(
            data={
                "form-TOTAL_FORMS": "1",
                "form-INITIAL_FORMS": "0",
                "form-MIN_NUM_FORMS": "0",
                "form-MAX_NUM_FORMS": "2",
                "form-0-qty": "not a number",
            }
        )
        self.assertFalse(formset.is_valid())
        html = render_to_string(
            "core/_form_errors.html", {"form": DemoForm(), "formset": formset}
        )
        self.assertIn("Line 1, Qty", html)
        self.assertIn('href="#id_form-0-qty"', html)
