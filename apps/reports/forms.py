"""Date-range controls for the financial statements.

The defaults matter more than they look. A financial report with no dates
chosen should not be empty and should not be everything - it should be the
period the person almost certainly wants, which is the open one. So the forms
default to the current fiscal period and say which period that was, rather than
silently picking dates the reader has to reverse-engineer from the heading.
"""

from datetime import date

from django import forms
from django.utils import timezone

from apps.core.form_ui import UIFormMixin
from apps.core.models import FiscalPeriod, FiscalYear, PeriodStatus


def default_window() -> tuple[date, date]:
    """The fiscal period covering today, else the fiscal year, else this year.

    Falls back rather than failing: a report is still useful on an installation
    whose calendar has not been set up, and refusing to render one because of a
    missing period would be a strange way to find that out.
    """
    today = timezone.localdate()
    period = FiscalPeriod.objects.filter(start_date__lte=today, end_date__gte=today).first()
    if period is not None:
        return period.start_date, period.end_date
    year = FiscalYear.objects.filter(start_date__lte=today, end_date__gte=today).first()
    if year is not None:
        return year.start_date, year.end_date
    return date(today.year, 1, 1), today


class DateRangeForm(UIFormMixin, forms.Form):
    """From and to, for the statements that cover a span."""

    date_from = forms.DateField(
        label="From", widget=forms.DateInput(attrs={"type": "date", "class": "field"})
    )
    date_to = forms.DateField(
        label="To", widget=forms.DateInput(attrs={"type": "date", "class": "field"})
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            start, end = default_window()
            self.initial.setdefault("date_from", start)
            self.initial.setdefault("date_to", end)

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("date_from"), cleaned.get("date_to")
        if start and end and start > end:
            self.add_error("date_to", "The end of the range comes before its start.")
        return cleaned

    def window(self) -> tuple[date, date]:
        """The chosen range, or the default when nothing valid was submitted."""
        if self.is_valid():
            return self.cleaned_data["date_from"], self.cleaned_data["date_to"]
        return default_window()


class AsOfForm(UIFormMixin, forms.Form):
    """A single date, for the statements that describe a moment."""

    as_of = forms.DateField(
        label="As at", widget=forms.DateInput(attrs={"type": "date", "class": "field"})
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.initial.setdefault("as_of", default_window()[1])

    def date(self) -> date:
        if self.is_valid():
            return self.cleaned_data["as_of"]
        return default_window()[1]


def open_periods():
    """Shortcut buttons: the periods someone is most likely to want."""
    return (
        FiscalPeriod.objects.filter(status=PeriodStatus.OPEN)
        .select_related("fiscal_year")
        .order_by("-start_date")[:12]
    )


class AgeingFilterForm(UIFormMixin, forms.Form):
    """As at when, and in which currency.

    Currency is a filter rather than a column because the ageing functions
    return each document in its own currency with no base equivalent, and a
    total that adds dollars to euros is worse than no total at all.
    """

    as_of = forms.DateField(
        label="As at", widget=forms.DateInput(attrs={"type": "date", "class": "field"})
    )
    currency = forms.ChoiceField(label="Currency", choices=(), required=False)
    overdue_only = forms.BooleanField(label="Overdue only", required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.core.models import Currency

        currencies = list(Currency.objects.filter(is_active=True).values_list("code", "name"))
        self.fields["currency"].choices = [
            (code, f"{code} — {name}") for code, name in currencies
        ]
        self.fields["currency"].widget.attrs.setdefault("class", "field")
        base = next(
            (
                code
                for code, _ in currencies
                if Currency.objects.filter(code=code, is_base=True).exists()
            ),
            currencies[0][0] if currencies else "",
        )
        if not self.is_bound:
            self.initial.setdefault("as_of", timezone.localdate())
            self.initial.setdefault("currency", base)
        self._fallback_currency = base

    def chosen(self):
        """The filters actually in force, defaults included."""
        if self.is_valid():
            return (
                self.cleaned_data["as_of"],
                self.cleaned_data.get("currency") or self._fallback_currency,
                bool(self.cleaned_data.get("overdue_only")),
            )
        return timezone.localdate(), self._fallback_currency, False


class MoneyRegisterFilterForm(UIFormMixin, forms.Form):
    """Which account, and over what span."""

    money_account = forms.ChoiceField(label="Money account", choices=())
    date_from = forms.DateField(
        label="From", widget=forms.DateInput(attrs={"type": "date", "class": "field"})
    )
    date_to = forms.DateField(
        label="To", widget=forms.DateInput(attrs={"type": "date", "class": "field"})
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.payments.models import MoneyAccount

        accounts = list(
            MoneyAccount.objects.filter(is_active=True)
            .order_by("code")
            .values_list("pk", "code", "name")
        )
        self.fields["money_account"].choices = [
            (pk, f"{code} — {name}") for pk, code, name in accounts
        ]
        self.fields["money_account"].widget.attrs.setdefault("class", "field")
        self._first_account = accounts[0][0] if accounts else None
        if not self.is_bound:
            start, end = default_window()
            self.initial.setdefault("date_from", start)
            self.initial.setdefault("date_to", end)
            if self._first_account is not None:
                self.initial.setdefault("money_account", self._first_account)

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("date_from"), cleaned.get("date_to")
        if start and end and start > end:
            self.add_error("date_to", "The end of the range comes before its start.")
        return cleaned

    def chosen(self):
        if self.is_valid():
            return (
                int(self.cleaned_data["money_account"]),
                self.cleaned_data["date_from"],
                self.cleaned_data["date_to"],
            )
        start, end = default_window()
        return self._first_account, start, end
