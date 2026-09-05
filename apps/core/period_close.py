"""Closing and reopening a fiscal period (CFG-009, ACC-008, BR-020, GL-012).

Closing a period is the accounting equivalent of signing something. After it,
nothing new can be posted into that window - the database enforces that with a
trigger on journal_entry, not merely this code - so the figures for the period
stop moving and can be reported, filed and quoted.

Two ideas shape this module.

**The checklist is advisory about judgement and absolute about arithmetic.**
Some findings are matters someone has to decide - an unposted draft invoice may
be genuinely abandoned, or may be the one that was forgotten. Those are
warnings: they are shown, and closing over them is allowed and recorded.
Others cannot be a matter of opinion. If debits do not equal credits, or an
earlier period is still open, closing would write down a number that is simply
wrong, and no reason text makes it right. Those block.

**Reopening is the dangerous direction.** Closing tightens; reopening loosens,
and it loosens something other people may already have relied on. So it needs
its own permission, its own reason, and it refuses to leave a hole in the
calendar by reopening a period that a closed one sits after.
"""

from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.core.models import DocumentStatus, FiscalPeriod, PeriodStatus

#: Document states that still expect to become a journal entry. A document in
#: one of these, dated inside the period, is work that has not landed.
UNPOSTED_STATES = (
    DocumentStatus.DRAFT,
    DocumentStatus.SUBMITTED,
    DocumentStatus.APPROVED,
)

#: Documents that must have reached the ledger before a period is closed, with
#: the date field that decides which period each one belongs to.
#:
#: Written out rather than discovered, deliberately. Asking the model registry
#: for "everything with a journal_entry field" also returns sales and purchase
#: *orders*, which inherit the field from FinancialDocumentBase and never post
#: - they are commitments, not entries - so a clever version of this list warns
#: about draft orders forever. The cost of writing it out is that a new
#: document type has to be added here; the test asserting this list is complete
#: is the reminder.
UNPOSTED_SOURCES = (
    ("sales", "SalesInvoice", "posting_date", "Sales invoices", "sales:invoice_list"),
    ("sales", "SalesCreditNote", "posting_date", "Sales credit notes", ""),
    ("sales", "SalesReturn", "document_date", "Sales returns", ""),
    ("purchases", "PurchaseBill", "posting_date", "Purchase bills", "purchases:bill_list"),
    (
        "purchases",
        "VendorDebitNote",
        "posting_date",
        "Vendor debit notes",
        "purchases:dbn_list",
    ),
    ("purchases", "PurchaseReturn", "document_date", "Purchase returns", "purchases:pr_list"),
    ("payments", "Payment", "posting_date", "Payments", "payments:payment_list"),
    ("payments", "Refund", "posting_date", "Refunds", ""),
    ("inventory", "GoodsReceipt", "document_date", "Goods receipts", "inventory:gr_list"),
    ("inventory", "DeliveryNote", "document_date", "Delivery notes", "sales:delivery_list"),
    ("inventory", "StockTransfer", "document_date", "Stock transfers", "inventory:st_list"),
    (
        "inventory",
        "StockAdjustment",
        "document_date",
        "Stock adjustments",
        "inventory:sa_list",
    ),
)

BLOCKER = "blocker"
WARNING = "warning"

#: How many unposted documents to enumerate before saying "at least". The
#: checklist needs to know *whether* there is a backlog and roughly how big;
#: it does not need every row of one.
UNPOSTED_SAMPLE_CAP = 500


@dataclass(frozen=True, slots=True)
class Check:
    """One line of the close checklist."""

    key: str
    title: str
    passed: bool
    severity: str
    detail: str
    #: Where to go and deal with it, when there is somewhere to go.
    url: str = ""

    @property
    def blocks_close(self) -> bool:
        return not self.passed and self.severity == BLOCKER

    @property
    def needs_acknowledging(self) -> bool:
        return not self.passed and self.severity == WARNING


@dataclass(frozen=True, slots=True)
class Checklist:
    period: FiscalPeriod
    checks: tuple[Check, ...]

    @property
    def blockers(self) -> tuple[Check, ...]:
        return tuple(check for check in self.checks if check.blocks_close)

    @property
    def warnings(self) -> tuple[Check, ...]:
        return tuple(check for check in self.checks if check.needs_acknowledging)

    @property
    def can_close(self) -> bool:
        return not self.blockers


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------
def _earlier_periods_closed(period: FiscalPeriod) -> Check:
    """Periods close in order, or the closed ones stop meaning anything.

    Closing March while February is open leaves February postable, and every
    March opening balance is then a number that can still change. A closing
    balance that moves is not a closing balance.
    """
    still_open = list(
        FiscalPeriod.objects.filter(
            end_date__lt=period.start_date, status=PeriodStatus.OPEN
        ).order_by("start_date")[:5]
    )
    if not still_open:
        return Check(
            key="earlier_periods",
            title="Earlier periods are closed",
            passed=True,
            severity=BLOCKER,
            detail="Nothing before this period is still open.",
        )
    names = ", ".join(p.name for p in still_open)
    return Check(
        key="earlier_periods",
        title="Earlier periods are closed",
        passed=False,
        severity=BLOCKER,
        detail=(
            f"{names} {'is' if len(still_open) == 1 else 'are'} still open. Close "
            "in order, or this period's opening balances can still change after it "
            "is closed."
        ),
    )


def _trial_balance_balances(period: FiscalPeriod) -> Check:
    """Debits equal credits, in the period and cumulatively."""
    from apps.reports.services import trial_balance

    report = trial_balance(period.start_date, period.end_date)
    if report.is_balanced:
        return Check(
            key="trial_balance",
            title="The trial balance balances",
            passed=True,
            severity=BLOCKER,
            detail=f"{len(report.rows)} accounts moved or carried a balance.",
            url="reports:trial_balance",
        )
    return Check(
        key="trial_balance",
        title="The trial balance balances",
        passed=False,
        severity=BLOCKER,
        detail=(
            f"Debits and credits differ by {report.out_of_balance_by}. Every journal "
            "is checked twice before it is written, so this means the ledger has been "
            "changed by something other than the posting engine."
        ),
        url="reports:trial_balance",
    )


def _subledger_reconciles(period: FiscalPeriod) -> Check:
    """Control accounts agree with the subledgers behind them.

    A warning rather than a blocker, and the distinction is deliberate. A
    difference is serious, but it is rarely created by the period being closed -
    it is usually older - and making it a blocker means one historical mistake
    freezes the calendar until somebody unpicks it. So it is shown prominently,
    it has to be acknowledged in writing, and the acknowledgement is kept.
    """
    from apps.reports.reconciliation import (
        CONTROL_LABELS,
        subledger_reconciliation,
        unevaluated_control_types,
    )

    checks = subledger_reconciliation()
    broken = [check for check in checks if not check.reconciles]
    unevaluated = unevaluated_control_types(checks)
    if not broken and not unevaluated:
        return Check(
            key="subledger",
            title="Control accounts agree with their subledgers",
            passed=True,
            severity=WARNING,
            detail=f"All {len(checks)} control accounts reconcile.",
            url="reports:reconciliation",
        )
    parts = [
        f"{check.label} differs by {check.difference} from {check.source}" for check in broken
    ]
    if unevaluated:
        # "Not examined" must never be presented as "agrees".
        names = ", ".join(CONTROL_LABELS[key] for key in unevaluated)
        parts.append(
            f"{names} could not be checked - the control account has no ledger activity"
        )
    detail = "; ".join(parts)
    return Check(
        key="subledger",
        title="Control accounts agree with their subledgers",
        passed=False,
        severity=WARNING,
        detail=detail,
        url="reports:reconciliation",
    )


def _unposted_documents(period: FiscalPeriod) -> Check:
    """Nothing dated inside the period is still waiting to be posted.

    A warning, because a draft can legitimately be abandoned - but an
    unnoticed one is revenue or cost that silently lands in a later period.
    """
    from collections import Counter

    from django.apps import apps as django_apps
    from django.db.models import CharField, Value

    # One query rather than twelve. Counting each document type separately is
    # the obvious way to write this and costs twelve round trips - about four
    # seconds against a database in another region, on a screen someone opens
    # to decide something. A UNION of the twelve gives one.
    parts = []
    urls = {}
    for app_label, model_name, date_field, label, url_name in UNPOSTED_SOURCES:
        try:
            model = django_apps.get_model(app_label, model_name)
        except LookupError:  # pragma: no cover - a module removed from the build
            continue
        urls[label] = url_name
        parts.append(
            model.objects.filter(
                status__in=UNPOSTED_STATES,
                **{
                    f"{date_field}__gte": period.start_date,
                    f"{date_field}__lte": period.end_date,
                },
            )
            .annotate(source=Value(label, output_field=CharField()))
            .values("source")
        )

    rows = []
    if parts:
        # Bounded: this returns a row per unposted document, and a period with a
        # genuine backlog should not pull all of it across the wire to say so.
        combined = parts[0].union(*parts[1:], all=True)
        rows = list(combined[: UNPOSTED_SAMPLE_CAP + 1])
    capped = len(rows) > UNPOSTED_SAMPLE_CAP
    counts = Counter(row["source"] for row in rows[:UNPOSTED_SAMPLE_CAP])
    outstanding = [(count, label, urls.get(label, "")) for label, count in counts.items()]

    if not outstanding:
        return Check(
            key="unposted",
            title="Everything dated in the period is posted",
            passed=True,
            severity=WARNING,
            detail="No drafts, submissions or approvals are waiting.",
        )
    # "sales invoices (3)" rather than "3 sales invoices", so a count of one
    # does not read as "1 payments". Every label here is plural; pluralising
    # them back down would need a singular form per row for no gain.
    detail = ", ".join(f"{label.lower()} ({count})" for count, label, _ in outstanding)
    if capped:
        detail = f"at least {detail}"
    return Check(
        key="unposted",
        title="Everything dated in the period is posted",
        passed=False,
        severity=WARNING,
        detail=(
            f"Still unposted: {detail}. Post them, move their date, or cancel them - "
            "otherwise they will have to be posted into a later period."
        ),
        url=next((url for _, _, url in outstanding if url), ""),
    )


def checklist(period: FiscalPeriod) -> Checklist:
    """Everything worth knowing before this period is signed off."""
    return Checklist(
        period=period,
        checks=(
            _earlier_periods_closed(period),
            _trial_balance_balances(period),
            _subledger_reconciles(period),
            _unposted_documents(period),
        ),
    )


# ---------------------------------------------------------------------------
# Closing and reopening
# ---------------------------------------------------------------------------
@transaction.atomic
def close_period(period: FiscalPeriod, *, user, reason: str) -> FiscalPeriod:
    """Close one period, refusing if the arithmetic says it is not ready.

    The checklist is recomputed here rather than trusted from the screen that
    displayed it. Between rendering the page and pressing the button somebody
    else can post an entry, and the whole value of a close is that it is true at
    the moment it happens.
    """
    locked = FiscalPeriod.objects.select_for_update().get(pk=period.pk)
    reason = (reason or "").strip()

    if locked.status == PeriodStatus.LOCKED:
        raise ValidationError(f"{locked.name} is permanently locked.")
    if locked.status == PeriodStatus.CLOSED:
        raise ValidationError(f"{locked.name} is already closed.")
    if not reason:
        raise ValidationError(
            "Give a reason. It is the only record of who signed this period off "
            "and on what basis."
        )

    report = checklist(locked)
    if report.blockers:
        raise ValidationError([f"{check.title}: {check.detail}" for check in report.blockers])

    locked.status = PeriodStatus.CLOSED
    locked.closed_at = timezone.now()
    locked.closed_by = user
    locked.close_reason = reason
    locked.save(update_fields=["status", "closed_at", "closed_by", "close_reason"])
    return locked


@transaction.atomic
def reopen_period(period: FiscalPeriod, *, user, reason: str) -> FiscalPeriod:
    """Reopen a closed period, if doing so does not leave a hole behind it."""
    locked = FiscalPeriod.objects.select_for_update().get(pk=period.pk)
    reason = (reason or "").strip()

    if locked.status == PeriodStatus.OPEN:
        raise ValidationError(f"{locked.name} is already open.")
    if locked.status == PeriodStatus.LOCKED:
        raise ValidationError(
            f"{locked.name} is permanently locked and cannot be reopened. That is "
            "what locking is for; a correction now belongs in an open period."
        )
    if not reason:
        raise ValidationError(
            "Give a reason. Reopening a closed period changes figures other people "
            "may already have reported, so the record of why matters more here than "
            "anywhere else."
        )

    # Reopening March while April is closed would let a new March entry change
    # April's opening balances after April was signed off.
    later_closed = list(
        FiscalPeriod.objects.filter(start_date__gt=locked.end_date)
        .exclude(status=PeriodStatus.OPEN)
        .order_by("start_date")[:5]
    )
    if later_closed:
        names = ", ".join(p.name for p in later_closed)
        raise ValidationError(
            f"{names} {'is' if len(later_closed) == 1 else 'are'} closed after this "
            "period. Reopen in reverse order, or a new entry here would change an "
            "opening balance that has already been signed off."
        )

    locked.status = PeriodStatus.OPEN
    locked.reopened_at = timezone.now()
    locked.reopened_by = user
    locked.reopen_reason = reason
    locked.save(update_fields=["status", "reopened_at", "reopened_by", "reopen_reason"])
    return locked
