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


class MoneyFormattingTests(SimpleTestCase):
    """An accounting screen that prints 1234567.00 invites a misread."""

    def money(self, value, currency=""):
        from apps.core.templatetags.ui import money

        return money(value, currency)

    def test_thousands_are_grouped(self):
        from decimal import Decimal

        self.assertEqual(self.money(Decimal("1234567"), "USD"), "USD 1,234,567.00")

    def test_two_decimal_places_always_so_a_column_aligns(self):
        from decimal import Decimal

        self.assertEqual(self.money(Decimal("5"), "EUR"), "EUR 5.00")

    def test_half_up_rounding_matches_the_ledger(self):
        from decimal import Decimal

        self.assertEqual(self.money(Decimal("1234.565"), ""), "1,234.57")

    def test_missing_value_reads_as_a_dash_not_zero(self):
        """Blank and zero mean different things on a financial record."""
        self.assertEqual(self.money(None), "—")
        self.assertEqual(self.money(""), "—")

    def test_no_currency_leaves_no_stray_space(self):
        from decimal import Decimal

        self.assertEqual(self.money(Decimal("10")), "10.00")

    def test_quantity_avoids_exponent_form(self):
        from decimal import Decimal

        from apps.core.templatetags.ui import quantity

        self.assertEqual(quantity(Decimal("1000")), "1,000")
        self.assertEqual(quantity(Decimal("2.5")), "2.5")


class FieldPresentationTests(SimpleTestCase):
    """UIFormMixin turns what the model knows into what the browser can use."""

    def form(self):
        from django import forms as f

        from apps.core.form_ui import UIFormMixin

        class Demo(UIFormMixin, f.Form):
            placeholders = {"code": "CUST-0001"}
            email = f.EmailField(label="Email")
            phone = f.CharField(label="Phone", required=False)
            amount = f.DecimalField(label="Amount", max_digits=12, decimal_places=2)
            count = f.IntegerField(label="Count")
            code = f.CharField(label="Code", max_length=16)

        return Demo()

    def test_placeholder_explains_shape_and_never_repeats_the_label(self):
        form = self.form()
        self.assertEqual(form.fields["code"].widget.attrs["placeholder"], "CUST-0001")
        self.assertEqual(form.fields["email"].widget.attrs["placeholder"], "name@company.com")
        for name, field in form.fields.items():
            placeholder = field.widget.attrs.get("placeholder", "")
            with self.subTest(field=name):
                self.assertNotEqual(placeholder.lower(), str(field.label).lower())

    def test_money_field_gets_a_decimal_keypad(self):
        self.assertEqual(self.form().fields["amount"].widget.attrs["inputmode"], "decimal")

    def test_integer_field_gets_a_numeric_keypad(self):
        self.assertEqual(self.form().fields["count"].widget.attrs["inputmode"], "numeric")

    def test_validation_bounds_come_from_the_field_not_the_template(self):
        """The client rule and the server rule cannot drift if only one is written."""
        form = self.form()
        self.assertEqual(form.fields["amount"].widget.attrs["data-rule"], "money")
        self.assertEqual(form.fields["amount"].widget.attrs["data-decimals"], "2")
        self.assertEqual(form.fields["code"].widget.attrs["maxlength"], "16")

    def test_phone_is_typed_for_the_dialpad(self):
        attrs = self.form().fields["phone"].widget.attrs
        self.assertEqual(attrs["type"], "tel")
        self.assertEqual(attrs["autocomplete"], "tel")


class BackLinkTests(SimpleTestCase):
    def test_named_route_resolves(self):
        from apps.core.mixins import BackLinkMixin

        class View(BackLinkMixin):
            back_url_name = "parties:customer_list"

        self.assertEqual(View().get_back_url(), "/parties/customers/")

    def test_no_declaration_means_no_arrow_rather_than_a_broken_one(self):
        from apps.core.mixins import BackLinkMixin

        self.assertIsNone(BackLinkMixin().get_back_url())

    def test_back_link_partial_names_its_destination(self):
        """“Back” alone is useless in a screen reader's list of links."""
        html = render_to_string(
            "core/_back_link.html",
            {"back_url": "/parties/customers/", "back_label": "Back to customers"},
        )
        self.assertIn('href="/parties/customers/"', html)
        self.assertIn("Back to customers", html)
        self.assertIn("i-arrow-left", html)


class ComposedFieldNameTests(SimpleTestCase):
    """
    Controls whose only visible label is a column header must name themselves.

    This regressed silently once already: the template built the name with
    `"Quantity, line "|add:line`, and Django's `add` returns "" for a string
    plus an int, so every order-line control shipped with no accessible name.
    """

    def field(self):
        from django import forms as f

        class Line(f.Form):
            quantity = f.DecimalField(label="Qty")

        return Line()["quantity"]

    def render(self, **kwargs):
        from apps.core.templatetags.ui import a11y_field

        return a11y_field(self.field(), **kwargs)

    def test_integer_line_number_produces_a_real_name(self):
        self.assertIn('aria-label="Quantity, line 3"', self.render(label="Quantity", line=3))

    def test_string_line_number_works_the_same(self):
        self.assertIn('aria-label="Quantity, line 3"', self.render(label="Quantity", line="3"))

    def test_template_placeholder_survives_for_the_clone_script(self):
        html = self.render(label="Quantity", line="__LINE__")
        self.assertIn('aria-label="Quantity, line __LINE__"', html)

    def test_label_without_a_line_still_names_the_control(self):
        self.assertIn('aria-label="Quantity"', self.render(label="Quantity"))

    def test_no_label_leaves_the_attribute_off_rather_than_empty(self):
        self.assertNotIn("aria-label", self.render())

    def test_extra_keywords_become_hyphenated_data_attributes(self):
        self.assertIn('data-max="5"', self.render(data_max=5))


class FloatingLabelTests(SimpleTestCase):
    """
    The pattern suits text-like inputs only.

    A date input draws its own placeholder and a picker button, a select always
    shows a value, and a checkbox has no interior — a label floating over any of
    them collides with what the browser paints.
    """

    def fields(self):
        from django import forms as f

        class Demo(f.Form):
            name = f.CharField(label="Name")
            notes = f.CharField(label="Notes", widget=f.Textarea)
            when = f.DateField(label="When", widget=f.DateInput(attrs={"type": "date"}))
            kind = f.ChoiceField(label="Kind", choices=[("a", "A")])
            active = f.BooleanField(label="Active", required=False)

        return Demo()

    def floatable(self, name):
        from apps.core.templatetags.ui import is_floatable

        return is_floatable(self.fields()[name])

    def test_text_and_textarea_float(self):
        self.assertTrue(self.floatable("name"))
        self.assertTrue(self.floatable("notes"))

    def test_date_select_and_checkbox_do_not(self):
        self.assertFalse(self.floatable("when"))
        self.assertFalse(self.floatable("kind"))
        self.assertFalse(self.floatable("active"))

    def test_the_partial_only_floats_where_it_fits(self):
        form = self.fields()
        self.assertIn(
            "is-floating", render_to_string("core/_form_field.html", {"field": form["name"]})
        )
        self.assertNotIn(
            "is-floating", render_to_string("core/_form_field.html", {"field": form["when"]})
        )

    def test_the_label_stays_a_real_label_for_the_control(self):
        """The motion is presentation; the association must not change."""
        html = render_to_string("core/_form_field.html", {"field": self.fields()["name"]})
        self.assertIn('for="id_name"', html)


class SuggestAndCheckAttributeTests(SimpleTestCase):
    """What the mixin tells the browser about a field."""

    def form(self):
        from django import forms as f

        from apps.core.form_ui import UIFormMixin

        class Demo(UIFormMixin, f.Form):
            checks = {"code": "customer-code"}
            code = f.CharField(label="Code")
            customer = f.ChoiceField(label="Customer", choices=[("1", "One")])
            direction = f.ChoiceField(
                label="Direction", choices=[("IN", "In"), ("OUT", "Out")]
            )

        return Demo()

    def test_a_known_field_names_its_suggester(self):
        attrs = self.form().fields["customer"].widget.attrs
        self.assertEqual(attrs["data-suggest"], "customer")
        self.assertIn("data-combobox", attrs)

    def test_a_short_unknown_select_stays_a_native_control(self):
        """Two options are faster as a dropdown than as a search box."""
        self.assertNotIn("data-combobox", self.form().fields["direction"].widget.attrs)

    def test_a_declared_check_reaches_the_field(self):
        self.assertEqual(
            self.form().fields["code"].widget.attrs["data-check"], "customer-code"
        )

    def test_editing_excludes_the_record_from_its_own_duplicate_check(self):
        from django import forms as f

        from apps.core.form_ui import UIFormMixin

        class Bound(UIFormMixin, f.Form):
            checks = {"code": "customer-code"}
            code = f.CharField(label="Code")

        form = Bound()
        form.instance = type("Row", (), {"pk": 42})()
        form.__init__()
        self.assertEqual(form.fields["code"].widget.attrs.get("data-check-exclude"), "42")
