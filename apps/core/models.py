"""
Core configuration: company identity, currencies, exchange rates, tax codes,
fiscal calendar, document numbering and the audit trail.

BRD coverage: CFG-001..CFG-010, BR-001, BR-002, BR-003, BR-012, BR-013,
BR-020, ACC-005, NFR-004, NFR-006, NFR-008.
"""

from decimal import Decimal

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateRangeField, RangeBoundary, RangeOperators
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import F, Func, Q
from django.urls import reverse
from django.utils import timezone

from apps.core.permissions import ACTION_PERMISSIONS


class DateRangeInclusive(Func):
    """
    daterange(start, end, '[]') — an inclusive-on-both-ends date range.

    Used with ExclusionConstraint so PostgreSQL itself refuses overlapping
    fiscal years and periods. Adopted from the colleague's schema; the earlier
    Django-only version had no overlap guard, which would let a month be
    double-counted by every period-based report.
    """

    function = "daterange"
    output_field = DateRangeField()


# ---------------------------------------------------------------------------
# Precision policy (BR-001, NFR-004)
#
# Money is always Decimal, never float. Two scales are used deliberately:
#   MONEY  - 18,4  document and ledger amounts (4dp absorbs tax/FX rounding)
#   QTY    - 18,4  quantities
#   COST   - 18,6  unit costs; weighted-average needs more scale than money
#   RATE   - 18,8  exchange rates
#   PCT    - 9,4   percentages
# ---------------------------------------------------------------------------
MONEY = {"max_digits": 18, "decimal_places": 4}
QTY = {"max_digits": 18, "decimal_places": 4}
COST = {"max_digits": 18, "decimal_places": 6}
RATE = {"max_digits": 18, "decimal_places": 8}
PCT = {"max_digits": 9, "decimal_places": 4}

ZERO = Decimal("0")


class DocumentStatus(models.TextChoices):
    """Common document lifecycle (BRD 6.1). Not every document uses every state."""

    DRAFT = "DRAFT", "Draft"
    SUBMITTED = "SUBMITTED", "Submitted"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"
    POSTED = "POSTED", "Posted"
    PARTIAL = "PARTIAL", "Partially fulfilled / partially paid"
    COMPLETED = "COMPLETED", "Completed / paid"
    CANCELLED = "CANCELLED", "Cancelled"
    REVERSED = "REVERSED", "Reversed / credited"


#: States in which a document's financial or stock effects exist.
POSTED_STATES = ["POSTED", "PARTIAL", "COMPLETED", "REVERSED"]

#: States in which a document is still freely editable (BR-004).
EDITABLE_STATES = ["DRAFT", "SUBMITTED", "REJECTED"]


class TimeStampedModel(models.Model):
    """Created/updated attribution for every business table (NFR-006)."""

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )

    class Meta:
        abstract = True


class FinancialDocumentBase(TimeStampedModel):
    """
    Shared shape of every document that produces a journal entry: sales invoice,
    purchase bill, credit note, debit note.

    Every monetary column exists twice — `*_txn` in the document currency and
    `*_base` in the company base currency — because FTD-001 requires the original
    amounts to survive and FTD-003 requires the ledger to be base currency. The
    rate is snapshotted here and never re-derived (BR-013).
    """

    number = models.CharField(max_length=32, unique=True)
    document_date = models.DateField(db_index=True)
    due_date = models.DateField(null=True, blank=True, db_index=True)
    posting_date = models.DateField(  # BR-020
        help_text=(
            "The date this document hits the ledger. It must fall inside an open "
            "fiscal period, and it can differ from the document date."
        )
    )
    fiscal_period = models.ForeignKey(
        "core.FiscalPeriod", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    currency = models.ForeignKey("core.Currency", on_delete=models.PROTECT, related_name="+")
    exchange_rate = models.DecimalField(**RATE, default=Decimal("1"))

    # Totals in transaction currency
    subtotal_txn = models.DecimalField(**MONEY, default=ZERO)
    line_discount_txn = models.DecimalField(**MONEY, default=ZERO)
    document_discount_txn = models.DecimalField(**MONEY, default=ZERO)
    taxable_base_txn = models.DecimalField(**MONEY, default=ZERO)
    tax_txn = models.DecimalField(**MONEY, default=ZERO)
    rounding_txn = models.DecimalField(**MONEY, default=ZERO)
    total_txn = models.DecimalField(**MONEY, default=ZERO)

    # Same figures converted at the snapshotted rate
    subtotal_base = models.DecimalField(**MONEY, default=ZERO)
    line_discount_base = models.DecimalField(**MONEY, default=ZERO)
    document_discount_base = models.DecimalField(**MONEY, default=ZERO)
    taxable_base_base = models.DecimalField(**MONEY, default=ZERO)
    tax_base = models.DecimalField(**MONEY, default=ZERO)
    rounding_base = models.DecimalField(**MONEY, default=ZERO)
    total_base = models.DecimalField(**MONEY, default=ZERO)

    # Settlement position (BR-007, PAY-006)
    allocated_txn = models.DecimalField(**MONEY, default=ZERO)
    credited_txn = models.DecimalField(**MONEY, default=ZERO)
    open_txn = models.DecimalField(**MONEY, default=ZERO)
    open_base = models.DecimalField(**MONEY, default=ZERO)

    status = models.CharField(
        max_length=10, choices=DocumentStatus.choices, default=DocumentStatus.DRAFT
    )
    notes = models.TextField(blank=True)
    internal_notes = models.TextField(blank=True)

    journal_entry = models.ForeignKey(
        "ledger.JournalEntry",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    posted_at = models.DateTimeField(null=True, blank=True)
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    approval_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        abstract = True

    def __str__(self):
        return self.number


class DocumentLineBase(models.Model):
    """
    Shared shape of a priced document line.

    Everything the posting and reporting layers need is snapshotted here at
    posting time — description, unit price, discount, tax code, tax rate,
    inclusivity, recoverability (BR-012) — so a later change to the product or
    tax master cannot alter history.

    Arithmetic contract (BR-010, BR-011, FTD-006):
        gross_txn        = quantity * unit_price
        net_txn          = gross_txn - line_discount_txn - allocated_document_discount_txn
        taxable_base_txn = net_txn                        (exclusive tax)
                         = net_txn / (1 + rate/100)       (inclusive tax)
        tax_txn          = taxable_base_txn * rate / 100
        total_txn        = taxable_base_txn + tax_txn
    """

    line_no = models.PositiveSmallIntegerField()
    description = models.CharField(max_length=255, blank=True)
    quantity = models.DecimalField(**QTY)
    unit_price = models.DecimalField(**MONEY, default=ZERO)

    discount_percent = models.DecimalField(**PCT, default=ZERO)
    line_discount_txn = models.DecimalField(**MONEY, default=ZERO)
    # BR-011: the header discount's share of this line, stored so tax, returns
    # and rounding all reproduce from persisted numbers.
    allocated_document_discount_txn = models.DecimalField(**MONEY, default=ZERO)

    # BR-012 tax snapshot
    tax_rate_percent = models.DecimalField(**PCT, default=ZERO)
    tax_is_inclusive = models.BooleanField(default=False)
    tax_is_recoverable = models.BooleanField(default=True)

    gross_txn = models.DecimalField(**MONEY, default=ZERO)
    net_txn = models.DecimalField(**MONEY, default=ZERO)
    taxable_base_txn = models.DecimalField(**MONEY, default=ZERO)
    tax_txn = models.DecimalField(**MONEY, default=ZERO)
    total_txn = models.DecimalField(**MONEY, default=ZERO)

    net_base = models.DecimalField(**MONEY, default=ZERO)
    taxable_base_base = models.DecimalField(**MONEY, default=ZERO)
    tax_base = models.DecimalField(**MONEY, default=ZERO)
    total_base = models.DecimalField(**MONEY, default=ZERO)

    class Meta:
        abstract = True


# ---------------------------------------------------------------------------
# Currency and exchange rates (CFG-003, FTD-001..FTD-005, BR-002, BR-013)
# ---------------------------------------------------------------------------
class Currency(models.Model):
    code = models.CharField(max_length=3, primary_key=True, help_text="ISO 4217")
    name = models.CharField(max_length=64)
    symbol = models.CharField(max_length=8, blank=True)
    decimal_places = models.PositiveSmallIntegerField(default=2)
    is_base = models.BooleanField(  # BR-002
        default=False,
        help_text=(
            "The currency your accounts are reported in. Exactly one currency "
            "can be the base, and changing it re-values every open balance."
        ),
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "currency"
        ordering = ["code"]
        constraints = [
            # BR-002: one and only one base currency.
            models.UniqueConstraint(
                fields=["is_base"],
                condition=Q(is_base=True),
                name="currency_single_base",
            ),
            models.CheckConstraint(
                condition=Q(decimal_places__lte=6),
                name="currency_decimal_places_sane",
            ),
        ]

    def __str__(self):
        return self.code

    def get_absolute_url(self):
        return reverse("core:currency_edit", args=[self.pk])


class ExchangeRate(models.Model):
    """Dated rate to base currency. Snapshotted onto documents at posting (BR-013)."""

    currency = models.ForeignKey(Currency, on_delete=models.PROTECT, related_name="rates")
    rate_date = models.DateField()
    rate = models.DecimalField(**RATE, validators=[MinValueValidator(Decimal("0.00000001"))])
    source = models.CharField(max_length=64, blank=True)

    class Meta:
        db_table = "exchange_rate"
        ordering = ["currency", "-rate_date"]
        constraints = [
            models.UniqueConstraint(
                fields=["currency", "rate_date"], name="exchange_rate_unique_per_day"
            ),
            # FTD-002: a rate must be strictly positive.
            models.CheckConstraint(condition=Q(rate__gt=0), name="exchange_rate_positive"),
        ]
        indexes = [models.Index(fields=["currency", "-rate_date"], name="ix_fx_lookup")]

    def __str__(self):
        return f"{self.currency_id} @ {self.rate_date}: {self.rate}"


# ---------------------------------------------------------------------------
# Tax codes (CFG-004, BR-012, FTD-006, FTD-007, FTD-010)
# ---------------------------------------------------------------------------
class TaxApplicability(models.TextChoices):
    SALES = "SALES", "Sales only"
    PURCHASE = "PURCHASE", "Purchase only"
    BOTH = "BOTH", "Sales and purchase"


class TaxTreatment(models.TextChoices):
    STANDARD = "STANDARD", "Standard rated"
    ZERO_RATED = "ZERO_RATED", "Zero rated"
    EXEMPT = "EXEMPT", "Exempt"
    NO_TAX = "NO_TAX", "Out of scope / no tax"


class TaxCode(models.Model):
    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=100)
    rate_percent = models.DecimalField(**PCT, default=ZERO)
    is_inclusive = models.BooleanField(  # FTD-006
        default=False,
        help_text=(
            "Tick when prices are quoted with this tax already in them, so the "
            "tax is worked back out of the price rather than added to it."
        ),
    )
    is_recoverable = models.BooleanField(
        default=True, help_text="Input tax may be reclaimed (drives input-tax reporting)."
    )
    treatment = models.CharField(
        max_length=12, choices=TaxTreatment.choices, default=TaxTreatment.STANDARD
    )
    applies_to = models.CharField(
        max_length=8, choices=TaxApplicability.choices, default=TaxApplicability.BOTH
    )
    output_tax_account = models.ForeignKey(
        "ledger.Account", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    input_tax_account = models.ForeignKey(
        "ledger.Account", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "tax_code"
        ordering = ["code"]
        constraints = [
            models.CheckConstraint(
                condition=Q(rate_percent__gte=0) & Q(rate_percent__lte=100),
                name="tax_code_rate_range",
            ),
            # A non-standard treatment must carry a zero rate (FTD-007).
            models.CheckConstraint(
                condition=Q(treatment="STANDARD") | Q(rate_percent=0),
                name="tax_code_nonstandard_is_zero",
            ),
        ]

    def __str__(self):
        return f"{self.code} ({self.rate_percent}%)"

    def get_absolute_url(self):
        return reverse("core:taxcode_edit", args=[self.pk])


# ---------------------------------------------------------------------------
# Payment terms (CFG-005)
# ---------------------------------------------------------------------------
class PaymentTerm(models.Model):
    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=100)
    net_days = models.PositiveSmallIntegerField(default=0)
    end_of_month = models.BooleanField(default=False)
    discount_percent = models.DecimalField(**PCT, default=ZERO)
    discount_days = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "payment_term"
        ordering = ["code"]
        constraints = [
            models.CheckConstraint(
                condition=Q(discount_percent__gte=0) & Q(discount_percent__lte=100),
                name="payment_term_discount_range",
            )
        ]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("core:paymentterm_edit", args=[self.pk])


# ---------------------------------------------------------------------------
# Company (CFG-001, CFG-010, BR-017, BR-022)
# ---------------------------------------------------------------------------
class Company(TimeStampedModel):
    """Single legal entity (BRD 3.1). Enforced as a singleton row."""

    singleton = models.BooleanField(default=True, editable=False)

    name = models.CharField(max_length=200)
    legal_name = models.CharField(max_length=200, blank=True)
    tax_id = models.CharField(max_length=50, blank=True)
    registration_no = models.CharField(max_length=50, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=50, blank=True)
    address_line1 = models.CharField(max_length=200, blank=True)
    address_line2 = models.CharField(max_length=200, blank=True)
    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=100, blank=True)
    postal_code = models.CharField(max_length=20, blank=True)
    country = models.CharField(max_length=100, blank=True)
    logo = models.ImageField(upload_to="company/", null=True, blank=True)

    base_currency = models.ForeignKey(Currency, on_delete=models.PROTECT, related_name="+")
    fiscal_year_start_month = models.PositiveSmallIntegerField(default=1)
    timezone = models.CharField(max_length=64, default="UTC")
    language = models.CharField(max_length=10, default="en")

    # CFG-010 operating policy
    allow_negative_stock = models.BooleanField(default=False)  # BR-017, INV-010
    rounding_tolerance = models.DecimalField(**MONEY, default=Decimal("0.05"))  # BR-022
    price_decimal_places = models.PositiveSmallIntegerField(default=2)
    qty_decimal_places = models.PositiveSmallIntegerField(default=2)
    require_po_approval = models.BooleanField(default=True)
    require_so_approval = models.BooleanField(default=True)
    block_duplicate_vendor_invoice = models.BooleanField(default=True)  # PUR-006
    warn_duplicate_customer_ref = models.BooleanField(default=True)  # SAL-014

    class Meta:
        db_table = "company"
        verbose_name_plural = "company"
        constraints = [
            models.UniqueConstraint(fields=["singleton"], name="company_singleton"),
            models.CheckConstraint(
                condition=Q(fiscal_year_start_month__gte=1)
                & Q(fiscal_year_start_month__lte=12),
                name="company_fy_start_month_range",
            ),
            models.CheckConstraint(
                condition=Q(rounding_tolerance__gte=0),
                name="company_rounding_tolerance_nonneg",
            ),
        ]

    def __str__(self):
        return self.name


# ---------------------------------------------------------------------------
# Fiscal calendar (CFG-009, BR-020, GL-012)
# ---------------------------------------------------------------------------
class PeriodStatus(models.TextChoices):
    OPEN = "OPEN", "Open"
    CLOSED = "CLOSED", "Closed"
    LOCKED = "LOCKED", "Permanently locked"


class FiscalYear(models.Model):
    code = models.CharField(max_length=20, unique=True)
    start_date = models.DateField()
    end_date = models.DateField()
    status = models.CharField(
        max_length=8, choices=PeriodStatus.choices, default=PeriodStatus.OPEN
    )

    class Meta:
        db_table = "fiscal_year"
        ordering = ["start_date"]
        constraints = [
            models.CheckConstraint(
                condition=Q(end_date__gt=F("start_date")), name="fiscal_year_dates_ordered"
            ),
            # BR-020: fiscal years cannot overlap. One legal entity in the MVP,
            # so the constraint is global rather than scoped to a company.
            ExclusionConstraint(
                name="fiscal_year_no_overlap",
                expressions=[
                    (
                        DateRangeInclusive(
                            "start_date",
                            "end_date",
                            RangeBoundary(inclusive_lower=True, inclusive_upper=True),
                        ),
                        RangeOperators.OVERLAPS,
                    )
                ],
            ),
        ]

    def __str__(self):
        return self.code


class FiscalPeriod(models.Model):
    """Posting window. Every journal entry points at exactly one (BR-020)."""

    fiscal_year = models.ForeignKey(
        FiscalYear, on_delete=models.PROTECT, related_name="periods"
    )
    period_no = models.PositiveSmallIntegerField()
    name = models.CharField(max_length=50)
    start_date = models.DateField()
    end_date = models.DateField()
    status = models.CharField(
        max_length=8, choices=PeriodStatus.choices, default=PeriodStatus.OPEN
    )
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    close_reason = models.TextField(blank=True)
    reopened_at = models.DateTimeField(null=True, blank=True)
    reopened_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    reopen_reason = models.TextField(blank=True)

    class Meta:
        db_table = "fiscal_period"
        ordering = ["start_date"]
        constraints = [
            models.UniqueConstraint(
                fields=["fiscal_year", "period_no"], name="fiscal_period_unique_no"
            ),
            models.CheckConstraint(
                condition=Q(end_date__gte=F("start_date")), name="fiscal_period_dates_ordered"
            ),
            # ACC-008 / CFG-009: closing requires an attributable actor.
            models.CheckConstraint(
                condition=Q(status="OPEN") | Q(closed_by__isnull=False),
                name="fiscal_period_closed_has_actor",
            ),
            # BR-020: two periods in the same fiscal year cannot overlap. Without
            # this, a posting date can fall in two periods and every opening /
            # movement / closing report double-counts it.
            ExclusionConstraint(
                name="fiscal_period_no_overlap",
                expressions=[
                    ("fiscal_year", RangeOperators.EQUAL),
                    (
                        DateRangeInclusive(
                            "start_date",
                            "end_date",
                            RangeBoundary(inclusive_lower=True, inclusive_upper=True),
                        ),
                        RangeOperators.OVERLAPS,
                    ),
                ],
            ),
        ]
        indexes = [
            models.Index(fields=["start_date", "end_date"], name="ix_period_daterange"),
            models.Index(fields=["status"], name="ix_period_status"),
        ]

    def __str__(self):
        return self.name

    def contains(self, d):
        return self.start_date <= d <= self.end_date


# ---------------------------------------------------------------------------
# Document numbering (CFG-008, BR-003, NFR-008)
# ---------------------------------------------------------------------------
class DocumentType(models.TextChoices):
    SALES_ORDER = "SO", "Sales order"
    DELIVERY_NOTE = "DN", "Delivery note"
    SALES_INVOICE = "SI", "Sales invoice"
    SALES_RETURN = "SR", "Sales return"
    CREDIT_NOTE = "CN", "Sales credit note"
    PURCHASE_ORDER = "PO", "Purchase order"
    GOODS_RECEIPT = "GR", "Goods receipt"
    PURCHASE_BILL = "PB", "Purchase bill"
    PURCHASE_RETURN = "PR", "Purchase return"
    DEBIT_NOTE = "DBN", "Vendor debit note"
    CUSTOMER_RECEIPT = "RC", "Customer receipt"
    VENDOR_PAYMENT = "PV", "Vendor payment"
    REFUND = "RF", "Refund"
    JOURNAL_ENTRY = "JE", "Journal entry"
    STOCK_TRANSFER = "ST", "Stock transfer"
    STOCK_ADJUSTMENT = "SA", "Stock adjustment"


class SequenceReset(models.TextChoices):
    NEVER = "NEVER", "Never"
    YEARLY = "YEARLY", "Each fiscal year"
    MONTHLY = "MONTHLY", "Each month"


class DocumentSequence(models.Model):
    """
    Number generator. Allocation must use SELECT ... FOR UPDATE on this row
    inside the posting transaction so concurrent posts cannot collide (NFR-008).
    """

    document_type = models.CharField(max_length=4, choices=DocumentType.choices)
    series = models.CharField(max_length=20, default="DEFAULT")
    prefix = models.CharField(max_length=20, blank=True)
    suffix = models.CharField(max_length=20, blank=True)
    padding = models.PositiveSmallIntegerField(default=5)
    next_number = models.BigIntegerField(default=1)
    reset_policy = models.CharField(
        max_length=8, choices=SequenceReset.choices, default=SequenceReset.YEARLY
    )
    period_key = models.CharField(
        max_length=10, blank=True, help_text="Marks which year/month next_number belongs to."
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "document_sequence"
        constraints = [
            models.UniqueConstraint(
                fields=["document_type", "series"], name="document_sequence_unique"
            ),
            models.CheckConstraint(
                condition=Q(next_number__gte=1), name="document_sequence_next_positive"
            ),
        ]

    def __str__(self):
        return f"{self.get_document_type_display()} / {self.series}"

    def get_absolute_url(self):
        return reverse("core:sequence_edit", args=[self.pk])


# ---------------------------------------------------------------------------
# Audit trail (ACC-005, RPT-020, NFR-006)
# ---------------------------------------------------------------------------
class AuditAction(models.TextChoices):
    CREATE = "CREATE", "Create"
    UPDATE = "UPDATE", "Update"
    DELETE = "DELETE", "Delete"
    SUBMIT = "SUBMIT", "Submit"
    APPROVE = "APPROVE", "Approve"
    REJECT = "REJECT", "Reject"
    POST = "POST", "Post"
    REVERSE = "REVERSE", "Reverse"
    ALLOCATE = "ALLOCATE", "Allocate"
    UNALLOCATE = "UNALLOCATE", "Unallocate"
    CLOSE_PERIOD = "CLOSE_PERIOD", "Close period"
    REOPEN_PERIOD = "REOPEN_PERIOD", "Reopen period"
    EXPORT = "EXPORT", "Export"
    LOGIN = "LOGIN", "Login"


class AuditEvent(models.Model):
    """Append-only. Never updated or deleted through the application."""

    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="audit_events",
    )
    action = models.CharField(max_length=16, choices=AuditAction.choices)
    content_type = models.ForeignKey(
        ContentType, null=True, blank=True, on_delete=models.PROTECT
    )
    object_id = models.BigIntegerField(null=True, blank=True)
    target = GenericForeignKey("content_type", "object_id")
    object_repr = models.CharField(max_length=255, blank=True)
    changes = models.JSONField(  # ACC-005
        null=True,
        blank=True,
        help_text="Which fields changed, with the value before and after each one.",
    )
    reason = models.TextField(blank=True)
    correlation_id = models.UUIDField(null=True, blank=True, db_index=True)  # NFR-016
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        db_table = "audit_event"
        ordering = ["-occurred_at", "-id"]
        indexes = [
            models.Index(fields=["content_type", "object_id"], name="ix_audit_target"),
            models.Index(fields=["user", "-occurred_at"], name="ix_audit_user_time"),
            models.Index(fields=["action", "-occurred_at"], name="ix_audit_action_time"),
        ]

    def __str__(self):
        return f"{self.action} {self.object_repr} @ {self.occurred_at:%Y-%m-%d %H:%M}"


# ---------------------------------------------------------------------------
# Action permissions (ACC-003, ACC-004)
# ---------------------------------------------------------------------------
class SystemPermission(models.Model):
    """
    A permission carrier, not a table.

    `managed = False` means Django creates no table for it, but still registers
    a Permission row for every entry in Meta.permissions. That gives one place
    to declare and audit every cross-cutting action permission without editing
    models owned by other team members.

    `default_permissions = ()` suppresses the automatic add/change/delete/view,
    which would be meaningless here.

    See apps/core/permissions.py for the codenames and the role matrix.
    """

    class Meta:
        managed = False
        default_permissions = ()
        permissions = ACTION_PERMISSIONS
        verbose_name = "system permission"
        verbose_name_plural = "system permissions"

    def __str__(self):
        return "System permissions"
