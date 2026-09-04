"""Financial statement screens (RPT-001..RPT-005).

Four reports over one ledger:

  General ledger   every posted line, filterable - the audit trail
  Trial balance    every account's opening, movement and closing
  Profit and loss  income less expenditure over a window
  Balance sheet    what is owned and owed at a moment

The general ledger is a list of rows and uses the shared FilteredListView, so
it gets the same search, sort, pagination and CSV export as every other list in
the application. The other three are aggregations rather than lists, so they
are plain TemplateViews over `apps.reports.services` - but they export CSV
through the same idea: the export re-runs the report it is exporting, so what
downloads is what was on screen (UX-007) rather than a second implementation
that agrees only by luck.
"""

import csv

from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.views.generic import TemplateView

from apps.core import audit
from apps.core.list_views import ChoiceFilter, Column, DateRangeFilter, FilteredListView
from apps.core.mixins import ActionPermissionMixin
from apps.core.models import AuditAction
from apps.core.permissions import EXPORT_DATA, VIEW_FINANCIAL_REPORTS
from apps.ledger.models import Account, JournalLine, JournalType
from apps.reports import ageing, reconciliation, registers, services
from apps.reports.forms import (
    AgeingFilterForm,
    AsOfForm,
    DateRangeForm,
    MoneyRegisterFilterForm,
    open_periods,
)


# ---------------------------------------------------------------------------
# General ledger (RPT-004)
# ---------------------------------------------------------------------------
class GeneralLedgerView(FilteredListView):
    """Every posted journal line, with the document that produced it.

    The screen equivalent of `v_general_ledger`. Reads JournalLine through the
    ORM rather than the view so that it inherits the shared list behaviour;
    the SQL view remains the interface for anything querying the database
    directly.
    """

    model = JournalLine
    required_permission = VIEW_FINANCIAL_REPORTS
    page_title = "General ledger"
    page_subtitle = "Every posted line, and the document behind it."
    export_permission = EXPORT_DATA
    export_filename = "general-ledger"
    default_ordering = "-entry__entry_date"
    paginate_by = 50
    show_summary = False

    # Related paths rather than properties on the model: _cell_value walks
    # "__" the same way the ORM does, so the column, the sort and the CSV all
    # name the same thing once.
    columns = [
        Column("entry__entry_date", "Date", sortable=True),
        Column("entry__number", "Entry", sortable=True, css="font-mono text-xs"),
        Column("account__code", "Code", sortable=True, css="font-mono text-xs"),
        Column("account__name", "Account", sortable=True),
        Column("description", "Description"),
        Column("entry__source_doc_number", "Source", css="font-mono text-xs"),
        Column("debit_base", "Debit", align="right", money=True),
        Column("credit_base", "Credit", align="right", money=True),
    ]
    search_fields = [
        "entry__number",
        "entry__narration",
        "entry__source_doc_number",
        "description",
        "account__code",
        "account__name",
    ]

    @property
    def filters(self):
        accounts = [
            (account.pk, f"{account.code} {account.name}")
            for account in Account.objects.filter(is_active=True, is_postable=True)
        ]
        return [
            ChoiceFilter("account", "Account", accounts),
            ChoiceFilter(
                "journal_type", "Journal", JournalType.choices, lookup="entry__journal_type"
            ),
            DateRangeFilter("entry_date", "Entry date", lookup="entry__entry_date"),
        ]

    def get_queryset(self):
        # Four related objects are read for every row; without this the page
        # costs a query per line, which against a remote database is seconds.
        return super().get_queryset().select_related("entry", "account")


# ---------------------------------------------------------------------------
# Shared behaviour for the three aggregate statements
# ---------------------------------------------------------------------------
class StatementView(ActionPermissionMixin, TemplateView):
    """A report with a date control, an export, and an audit record.

    Exporting a financial statement is worth recording: it is the moment
    numbers leave the system and start being quoted elsewhere, and "who pulled
    the figures the board saw" is a question that gets asked (ACC-005).
    """

    required_permission = VIEW_FINANCIAL_REPORTS
    export_filename = "report"

    def get(self, request, *args, **kwargs):
        if request.GET.get("export") == "csv":
            if not request.user.has_perm(EXPORT_DATA):
                raise PermissionDenied("You do not have permission to export data.")
            return self.export_csv()
        return super().get(request, *args, **kwargs)

    def export_csv(self) -> HttpResponse:
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="{self.export_filename}.csv"'
        self.write_csv(csv.writer(response))
        audit.record_action(
            self.request, AuditAction.EXPORT, None, report=self.export_filename
        )
        return response

    def write_csv(self, writer):  # pragma: no cover - overridden by each report
        raise NotImplementedError

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["periods"] = open_periods()
        return ctx


def _money(value) -> str:
    """CSV cells as plain decimals: no thousands separators, no currency."""
    return f"{value:.2f}"


# ---------------------------------------------------------------------------
# Trial balance (RPT-003)
# ---------------------------------------------------------------------------
class TrialBalanceView(StatementView):
    template_name = "reports/trial_balance.html"
    export_filename = "trial-balance"

    def report(self):
        form = DateRangeForm(self.request.GET or None)
        start, end = form.window()
        return form, services.trial_balance(start, end)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        form, report = self.report()
        ctx.update(
            {
                "form": form,
                "report": report,
                "page_title": "Trial balance",
                "page_subtitle": "Every account's opening balance, movement and closing balance.",
            }
        )
        return ctx

    def write_csv(self, writer):
        _, report = self.report()
        writer.writerow(["Trial balance", f"{report.date_from} to {report.date_to}"])
        writer.writerow([])
        writer.writerow(
            [
                "Code",
                "Account",
                "Type",
                "Opening debit",
                "Opening credit",
                "Period debit",
                "Period credit",
                "Closing debit",
                "Closing credit",
            ]
        )
        for row in report.rows:
            writer.writerow(
                [
                    row.code,
                    row.name,
                    row.account_type,
                    _money(row.opening_debit),
                    _money(row.opening_credit),
                    _money(row.period_debit),
                    _money(row.period_credit),
                    _money(row.closing_debit),
                    _money(row.closing_credit),
                ]
            )
        writer.writerow(
            [
                "",
                "Totals",
                "",
                _money(report.opening_debit),
                _money(report.opening_credit),
                _money(report.period_debit),
                _money(report.period_credit),
                _money(report.closing_debit),
                _money(report.closing_credit),
            ]
        )


# ---------------------------------------------------------------------------
# Profit and loss (RPT-002)
# ---------------------------------------------------------------------------
class ProfitAndLossView(StatementView):
    template_name = "reports/profit_and_loss.html"
    export_filename = "profit-and-loss"

    def report(self):
        form = DateRangeForm(self.request.GET or None)
        start, end = form.window()
        return form, services.profit_and_loss(start, end)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        form, report = self.report()
        ctx.update(
            {
                "form": form,
                "report": report,
                "page_title": "Profit and loss",
                "page_subtitle": "What was earned and what it cost, over the chosen period.",
            }
        )
        return ctx

    def write_csv(self, writer):
        _, report = self.report()
        writer.writerow(["Profit and loss", f"{report.date_from} to {report.date_to}"])
        writer.writerow([])
        sections = (
            (report.revenue, None),
            (report.cost_of_sales, ("Gross profit", report.gross_profit)),
            (report.operating_expenses, ("Operating profit", report.operating_profit)),
            (report.other_income, None),
            (report.other_expenses, ("Net profit", report.net_profit)),
        )
        for section, subtotal in sections:
            if section.is_empty and subtotal is None:
                continue
            writer.writerow([section.title])
            for row in section.rows:
                writer.writerow([row.code, row.name, _money(row.period_net)])
            writer.writerow(["", f"Total {section.title.lower()}", _money(section.total)])
            if subtotal is not None:
                writer.writerow([])
                writer.writerow(["", subtotal[0], _money(subtotal[1])])
            writer.writerow([])


# ---------------------------------------------------------------------------
# Balance sheet (RPT-001)
# ---------------------------------------------------------------------------
class BalanceSheetView(StatementView):
    template_name = "reports/balance_sheet.html"
    export_filename = "balance-sheet"

    def report(self):
        form = AsOfForm(self.request.GET or None)
        return form, services.balance_sheet(form.date())

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        form, report = self.report()
        ctx.update(
            {
                "form": form,
                "report": report,
                "page_title": "Balance sheet",
                "page_subtitle": "What the business owns and owes, at a single date.",
            }
        )
        return ctx

    def write_csv(self, writer):
        _, report = self.report()
        writer.writerow(["Balance sheet", f"as at {report.as_of}"])
        writer.writerow([])
        for section in (report.current_assets, report.noncurrent_assets):
            writer.writerow([section.title])
            for row in section.rows:
                writer.writerow([row.code, row.name, _money(row.closing_net)])
            writer.writerow(["", f"Total {section.title.lower()}", _money(section.total)])
            writer.writerow([])
        writer.writerow(["", "TOTAL ASSETS", _money(report.total_assets)])
        writer.writerow([])
        for section in (report.current_liabilities, report.noncurrent_liabilities):
            writer.writerow([section.title])
            for row in section.rows:
                writer.writerow([row.code, row.name, _money(row.closing_net)])
            writer.writerow(["", f"Total {section.title.lower()}", _money(section.total)])
            writer.writerow([])
        writer.writerow(["", "Total liabilities", _money(report.total_liabilities)])
        writer.writerow([])
        writer.writerow([report.equity.title])
        for row in report.equity.rows:
            writer.writerow([row.code, row.name, _money(row.closing_net)])
        if report.result_for_period:
            writer.writerow(
                [
                    "",
                    "Result for the period (not yet closed)",
                    _money(report.result_for_period),
                ]
            )
        writer.writerow(["", "Total equity", _money(report.total_equity)])
        writer.writerow([])
        writer.writerow(
            ["", "TOTAL LIABILITIES AND EQUITY", _money(report.total_liabilities_and_equity)]
        )


# ---------------------------------------------------------------------------
# Subledger reconciliation (GL-011, RPT-021)
# ---------------------------------------------------------------------------
class ReconciliationView(StatementView):
    """Control accounts beside the subledgers that are supposed to explain them.

    No date control: the underlying view compares the ledger as it stands with
    the documents as they stand, and those two should agree at every instant
    rather than only at a period end.
    """

    template_name = "reports/reconciliation.html"
    export_filename = "subledger-reconciliation"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        checks = reconciliation.subledger_reconciliation()
        ctx.update(
            {
                "checks": checks,
                "unevaluated": [
                    reconciliation.CONTROL_LABELS[key]
                    for key in reconciliation.unevaluated_control_types(checks)
                ],
                "tolerance": reconciliation.TOLERANCE,
                "page_title": "Subledger reconciliation",
                "page_subtitle": (
                    "Whether each control account still agrees with the documents behind it."
                ),
            }
        )
        return ctx

    def write_csv(self, writer):
        writer.writerow(["Subledger reconciliation"])
        writer.writerow([])
        writer.writerow(
            ["Control", "Account", "GL balance", "Subledger", "Difference", "Reconciles"]
        )
        checks = reconciliation.subledger_reconciliation()
        for check in checks:
            writer.writerow(
                [
                    check.label,
                    check.account_code,
                    _money(check.gl_balance),
                    _money(check.subledger_balance),
                    _money(check.difference),
                    "yes" if check.reconciles else "NO",
                ]
            )
        for key in reconciliation.unevaluated_control_types(checks):
            writer.writerow(
                [
                    reconciliation.CONTROL_LABELS[key],
                    "",
                    "",
                    "",
                    "",
                    "not examined - no ledger activity",
                ]
            )


# ---------------------------------------------------------------------------
# Ageing (RPT-006, RPT-007)
# ---------------------------------------------------------------------------
class AgeingView(StatementView):
    """Shared by both sides; the subclasses only choose which one."""

    template_name = "reports/ageing.html"
    side = ageing.AR

    def report(self):
        form = AgeingFilterForm(self.request.GET or None)
        as_of, currency, overdue_only = form.chosen()
        report = ageing.ageing(self.side, as_of, currency)
        return form, report, overdue_only

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        form, report, overdue_only = self.report()
        parties = report.parties
        if overdue_only:
            parties = tuple(party for party in parties if party.overdue)
        ctx.update(
            {
                "form": form,
                "report": report,
                "parties": parties,
                "overdue_only": overdue_only,
                "buckets": ageing.BUCKETS,
                "bucket_labels": ageing.BUCKET_LABELS,
                "page_title": report.title,
                "page_subtitle": (
                    f"Open documents at {report.as_of}, by how overdue they are."
                ),
            }
        )
        return ctx

    def write_csv(self, writer):
        _, report, overdue_only = self.report()
        writer.writerow([report.title, f"as at {report.as_of}", report.currency_code])
        writer.writerow([])
        writer.writerow(
            [
                report.party_label,
                "Document",
                "Document date",
                "Due date",
                "Days overdue",
                "Total",
                "Settled",
                "Open",
                "Bucket",
            ]
        )
        for party in report.parties:
            if overdue_only and not party.overdue:
                continue
            for item in party.items:
                writer.writerow(
                    [
                        item.party_name,
                        item.document_number,
                        item.document_date,
                        item.due_date or "",
                        item.days_overdue,
                        _money(item.total),
                        _money(item.settled),
                        _money(item.open_amount),
                        item.bucket,
                    ]
                )
            writer.writerow(
                [
                    "",
                    f"Total for {party.party_name}",
                    "",
                    "",
                    "",
                    "",
                    "",
                    _money(party.total),
                    "",
                ]
            )
        writer.writerow([])
        writer.writerow(["Bucket totals"])
        for bucket in ageing.BUCKETS:
            writer.writerow([ageing.BUCKET_LABELS[bucket], _money(report.buckets[bucket])])
        writer.writerow(["Total", _money(report.total)])
        for code, count in report.other_currencies:
            writer.writerow([f"Excluded: {count} open document(s) in {code}"])


class ReceivablesAgeingView(AgeingView):
    side = ageing.AR
    export_filename = "receivables-ageing"


class PayablesAgeingView(AgeingView):
    side = ageing.AP
    export_filename = "payables-ageing"


# ---------------------------------------------------------------------------
# Tax (RPT-008)
# ---------------------------------------------------------------------------
class TaxReportView(StatementView):
    template_name = "reports/tax.html"
    export_filename = "tax-report"

    def report(self):
        form = DateRangeForm(self.request.GET or None)
        start, end = form.window()
        return form, registers.tax_report(start, end)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        form, report = self.report()
        ctx.update(
            {
                "form": form,
                "report": report,
                "page_title": "Tax report",
                "page_subtitle": "Tax charged on sales and incurred on purchases.",
            }
        )
        return ctx

    def write_csv(self, writer):
        _, report = self.report()
        writer.writerow(["Tax report", f"{report.date_from} to {report.date_to}"])
        writer.writerow([])
        for side in (report.sales, report.purchases):
            writer.writerow([side.label])
            writer.writerow(["Tax code", "Treatment", "Rate %", "Taxable base", "Tax"])
            for group in side.groups:
                writer.writerow(
                    [
                        group.tax_code,
                        group.treatment,
                        group.rate_percent,
                        _money(group.taxable_base),
                        _money(group.tax_amount),
                    ]
                )
            writer.writerow(
                ["", "Total", "", _money(side.taxable_base), _money(side.tax_amount)]
            )
            if side.non_recoverable:
                writer.writerow(
                    ["", "of which non-recoverable", "", "", _money(side.non_recoverable)]
                )
            writer.writerow([])
        writer.writerow(["Net payable", _money(report.net_payable)])


# ---------------------------------------------------------------------------
# Money account register (RPT-013)
# ---------------------------------------------------------------------------
class MoneyRegisterView(StatementView):
    template_name = "reports/money_register.html"
    export_filename = "money-register"

    def report(self):
        form = MoneyRegisterFilterForm(self.request.GET or None)
        account_id, start, end = form.chosen()
        if account_id is None:
            return form, None
        return form, registers.money_register(
            money_account_id=account_id, date_from=start, date_to=end
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        form, report = self.report()
        ctx.update(
            {
                "form": form,
                "report": report,
                "page_title": "Money account register",
                "page_subtitle": "Every movement through one cash or bank account, in order.",
            }
        )
        return ctx

    def write_csv(self, writer):
        _, report = self.report()
        if report is None:
            writer.writerow(["No money accounts are configured."])
            return
        writer.writerow(["Money account register", f"{report.date_from} to {report.date_to}"])
        writer.writerow([])
        writer.writerow(["Date", "Entry", "Journal", "Description", "In", "Out", "Balance"])
        writer.writerow(
            ["", "", "", "Opening balance", "", "", _money(report.opening_balance)]
        )
        for entry in report.entries:
            writer.writerow(
                [
                    entry.entry_date,
                    entry.entry_number,
                    entry.journal_type,
                    entry.description,
                    _money(entry.money_in),
                    _money(entry.money_out),
                    _money(entry.balance),
                ]
            )
        writer.writerow(
            [
                "",
                "",
                "",
                "Closing balance",
                _money(report.total_in),
                _money(report.total_out),
                _money(report.closing_balance),
            ]
        )
