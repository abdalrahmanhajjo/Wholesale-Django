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
