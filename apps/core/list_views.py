"""
The shared list pattern (UX-002, UX-005, UX-007, RPT-* filters).

Every module in this system needs the same screen: a table with a search box,
some filters, sortable columns, pagination, totals and a CSV export. UX-002 asks
for exactly that — "consistent list, filter, sort, pagination, detail, create,
update-draft, and print patterns … without learning different control behaviour
per module".

So it is written once, here, and every module subclasses it. Declare what your
list contains; the base class and `core/list_base.html` do the rest.

    class CustomerListView(FilteredListView):
        model = Customer
        columns = [
            Column("code", "Code", sortable=True, css="font-mono"),
            Column("name", "Name", sortable=True),
            Column("credit_limit", "Credit limit", align="right", money=True),
        ]
        search_fields = ["code", "name", "tax_id"]
        filters = [ChoiceFilter("status", "Status", [("active", "Active"), ...])]
        export_permission = EXPORT_DATA

Design notes
------------
Filter state lives in the query string, so a filtered list is a shareable URL
and the back button works (UX-002's "Filter state preserved in URL where
practical").

Sorting is restricted to columns marked `sortable`. Accepting an arbitrary
`?sort=` value would let anyone order by a related table and turn a list page
into an expensive join.

Export re-runs the same queryset with the same filters, so UX-007's "exported
rows and totals match the on-screen filtered report" holds by construction
rather than by two code paths agreeing.
"""

import csv
from dataclasses import dataclass, field
from decimal import Decimal

from django.contrib.postgres.search import TrigramSimilarity
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.db.models.functions import Upper
from django.http import HttpResponse
from django.views.generic import ListView

from apps.core import audit
from apps.core.mixins import ActionPermissionMixin


@dataclass
class Column:
    """One column of the table, and of the CSV."""

    name: str  # model field or a method on the object
    label: str
    sortable: bool = False
    align: str = "left"  # left | right | center
    css: str = ""
    money: bool = False  # right-align and use tabular numerals
    badge: bool = False  # render through the status-badge partial
    link: bool = False  # link the cell to the row's get_absolute_url
    export: bool = True  # include in CSV
    #: Order by something other than `name` — e.g. a related field.
    order_by: str = ""

    @property
    def ordering_key(self):
        return self.order_by or self.name


@dataclass
class ChoiceFilter:
    """A dropdown filter. `lookup` defaults to `name` as an exact match."""

    name: str
    label: str
    choices: list  # [(value, label), ...]
    lookup: str = ""
    blank_label: str = "All"

    def apply(self, queryset, value):
        return queryset.filter(**{self.lookup or self.name: value})


@dataclass
class BooleanFilter:
    name: str
    label: str
    lookup: str = ""
    true_label: str = "Yes"
    false_label: str = "No"
    blank_label: str = "All"

    @property
    def choices(self):
        return [("1", self.true_label), ("0", self.false_label)]

    def apply(self, queryset, value):
        return queryset.filter(**{self.lookup or self.name: value == "1"})


@dataclass
class DateRangeFilter:
    """Renders two date inputs, `<name>_from` and `<name>_to`."""

    name: str
    label: str
    lookup: str = ""
    choices: list = field(default_factory=list)

    def apply(self, queryset, value):  # handled by the view, not here
        return queryset


class FilteredListView(ActionPermissionMixin, ListView):
    """Search + filter + sort + paginate + export, ready to subclass."""

    template_name = "core/list_base.html"
    paginate_by = 25
    columns = []
    search_fields = []
    #: Fuzzy-match these with pg_trgm as well as the exact search above.
    trigram_search_fields = []
    trigram_threshold = 0.3
    filters = []
    default_ordering = ""
    export_permission = None
    export_filename = "export"
    #: Shown above the table; override `get_summary()` to compute them.
    show_summary = True
    create_url_name = ""
    create_label = "New"
    page_title = ""
    page_subtitle = ""
    # Lists contain commercially sensitive data. Subclasses must declare a
    # model view permission and the mixin enforces it for GET and export alike.
    enforce_on_safe_methods = True

    # -- queryset ----------------------------------------------------------
    def get_queryset(self):
        qs = super().get_queryset()
        qs = self.apply_search(qs)
        qs = self.apply_filters(qs)
        qs = self.apply_ordering(qs)
        return qs

    def apply_search(self, qs):
        term = (self.request.GET.get("q") or "").strip()
        if not term:
            return qs

        exact = Q()
        for f in self.search_fields:
            exact |= Q(**{f"{f}__icontains": term})

        if not self.trigram_search_fields:
            return qs.filter(exact)

        # PTY-007 wants likely duplicates flagged, not just exact matches, so
        # the search box does the same: substring OR trigram similarity.
        similarity = None
        for f in self.trigram_search_fields:
            s = TrigramSimilarity(Upper(f), term.upper())
            similarity = s if similarity is None else similarity + s
        return (
            qs.annotate(_similarity=similarity)
            .filter(exact | Q(_similarity__gt=self.trigram_threshold))
            .order_by("-_similarity")
        )

    def apply_filters(self, qs):
        for f in self.filters:
            if isinstance(f, DateRangeFilter):
                lookup = f.lookup or f.name
                start = self.request.GET.get(f"{f.name}_from")
                end = self.request.GET.get(f"{f.name}_to")
                if start:
                    qs = qs.filter(**{f"{lookup}__gte": start})
                if end:
                    qs = qs.filter(**{f"{lookup}__lte": end})
                continue
            value = self.request.GET.get(f.name)
            if value not in (None, ""):
                qs = f.apply(qs, value)
        return qs

    def apply_ordering(self, qs):
        requested = self.request.GET.get("sort") or ""
        key = requested.lstrip("-")
        allowed = {c.ordering_key: c for c in self.columns if c.sortable}
        if key in allowed:
            prefix = "-" if requested.startswith("-") else ""
            return qs.order_by(f"{prefix}{key}")
        if self.default_ordering:
            return qs.order_by(self.default_ordering)
        return qs

    # -- export ------------------------------------------------------------
    def get(self, request, *args, **kwargs):
        if request.GET.get("export") == "csv":
            return self.export_csv()
        return super().get(request, *args, **kwargs)

    def export_csv(self):
        # UX-007 exports are permission-checked and recorded (ACC-005).
        if self.export_permission and not self.request.user.has_perm(self.export_permission):
            raise PermissionDenied("You do not have permission to export data.")

        queryset = self.get_queryset()
        columns = [c for c in self.columns if c.export]

        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="{self.export_filename}.csv"'
        writer = csv.writer(response)
        writer.writerow([c.label for c in columns])

        count = 0
        for obj in queryset.iterator(chunk_size=500):
            writer.writerow([_csv_value(obj, c) for c in columns])
            count += 1

        audit.record_export(self.request, f"{self.export_filename} ({count} rows)", count)
        return response

    # -- template context --------------------------------------------------
    def get_summary(self):
        """Override to return [(label, value), ...] shown above the table."""
        return []

    def get_rendered_filters(self):
        """
        Filters with their current state resolved, so the template does no
        lookup gymnastics to work out which option is selected.
        """
        rendered = []
        for f in self.filters:
            if isinstance(f, DateRangeFilter):
                rendered.append(
                    {
                        "kind": "daterange",
                        "name": f.name,
                        "label": f.label,
                        "value_from": self.request.GET.get(f"{f.name}_from", ""),
                        "value_to": self.request.GET.get(f"{f.name}_to", ""),
                    }
                )
                continue
            current = self.request.GET.get(f.name, "")
            rendered.append(
                {
                    "kind": "choice",
                    "name": f.name,
                    "label": f.label,
                    "blank_label": f.blank_label,
                    "choices": [
                        {"value": v, "label": lbl, "selected": str(v) == current}
                        for v, lbl in f.choices
                    ],
                }
            )
        return rendered

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        params = self.request.GET.copy()
        params.pop("page", None)

        ctx.update(
            {
                "columns": self.columns,
                "filters": self.get_rendered_filters(),
                "search_term": self.request.GET.get("q", ""),
                "current_sort": self.request.GET.get("sort", ""),
                "querystring": params.urlencode(),
                "active_filters": {
                    k: v
                    for k, v in self.request.GET.items()
                    if k not in ("page", "sort") and v
                },
                "total_count": ctx["paginator"].count
                if ctx.get("paginator")
                else len(ctx["object_list"]),
                "summary": self.get_summary() if self.show_summary else [],
                "can_export": bool(self.export_permission)
                and self.request.user.has_perm(self.export_permission),
                "can_create": bool(self.create_url_name)
                and self.request.user.has_perm(
                    f"{self.model._meta.app_label}.add_{self.model._meta.model_name}"
                ),
                "create_url_name": self.create_url_name,
                "create_label": self.create_label,
                # When a column already links to the record, the trailing
                # "View" cell is a second link to the same place — a duplicate
                # tab stop and a second identical entry in a screen reader's
                # link list. The template drops it in that case.
                "has_linked_column": any(c.link for c in self.columns),
                "record_label": self.model._meta.verbose_name,
                "record_label_plural": self.model._meta.verbose_name_plural,
                "page_title": self.page_title or self.model._meta.verbose_name_plural.title(),
                "page_subtitle": self.page_subtitle,
                "rows": [
                    {"object": obj, "cells": [_render_cell(obj, c) for c in self.columns]}
                    for obj in ctx["object_list"]
                ],
            }
        )
        return ctx


def _cell_value(obj, column):
    """Raw value for CSV — no formatting, so the numbers stay machine-readable."""
    value = obj
    for part in column.name.split("__"):
        value = getattr(value, part, None)
        if value is None:
            return ""
    return value() if callable(value) else value


def _csv_value(obj, column):
    """Return a spreadsheet-safe CSV value without converting real numbers."""
    value = _cell_value(obj, column)
    if isinstance(value, str):
        candidate = value.lstrip(" ")
        if candidate.startswith(("=", "+", "-", "@", "\t", "\r")):
            return f"'{value}"
    return value


def _render_cell(obj, column):
    value = _cell_value(obj, column)
    if isinstance(value, Decimal) or column.money:
        display = f"{value:,.2f}" if value not in (None, "") else "—"
    elif isinstance(value, bool):
        display = "Yes" if value else "No"
    elif value in (None, ""):
        display = "—"
    else:
        display = value
    return {"column": column, "value": value, "display": display}
