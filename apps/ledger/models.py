"""
General ledger: chart of accounts, account mappings, journal entries and lines,
opening balances.

BRD coverage: CFG-006, CFG-007, GL-001..GL-012, BR-005, BR-006, BR-013,
BR-021, RPT-001..RPT-005, RPT-021, Appendix A posting matrix.
"""

import uuid
from decimal import Decimal

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Upper
from django.urls import reverse

from apps.core.expressions import at_most_one, exactly_one
from apps.core.models import MONEY, RATE, TimeStampedModel

ZERO = Decimal("0")


# ---------------------------------------------------------------------------
# Chart of accounts (CFG-006, GL-010)
# ---------------------------------------------------------------------------
class AccountType(models.TextChoices):
    ASSET = "ASSET", "Asset"
    LIABILITY = "LIABILITY", "Liability"
    EQUITY = "EQUITY", "Equity"
    INCOME = "INCOME", "Income"
    EXPENSE = "EXPENSE", "Expense"


class NormalBalance(models.TextChoices):
    DEBIT = "DEBIT", "Debit"
    CREDIT = "CREDIT", "Credit"


class AccountSubtype(models.TextChoices):
    """Drives Balance Sheet / P&L grouping (RPT-001, RPT-002)."""

    CURRENT_ASSET = "CURRENT_ASSET", "Current asset"
    NONCURRENT_ASSET = "NONCURRENT_ASSET", "Non-current asset"
    CURRENT_LIABILITY = "CURRENT_LIABILITY", "Current liability"
    NONCURRENT_LIABILITY = "NONCURRENT_LIABILITY", "Non-current liability"
    EQUITY = "EQUITY", "Equity"
    REVENUE = "REVENUE", "Revenue"
    OTHER_INCOME = "OTHER_INCOME", "Other income"
    COGS = "COGS", "Cost of goods sold"
    OPERATING_EXPENSE = "OPERATING_EXPENSE", "Operating expense"
    OTHER_EXPENSE = "OTHER_EXPENSE", "Other expense"


class Account(TimeStampedModel):
    code = models.CharField(max_length=20)
    name = models.CharField(max_length=150)
    account_type = models.CharField(max_length=12, choices=AccountType.choices)
    subtype = models.CharField(max_length=24, choices=AccountSubtype.choices)
    normal_balance = models.CharField(max_length=6, choices=NormalBalance.choices)
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="children"
    )
    # GL-010: only leaf/postable accounts may receive journal lines.
    is_postable = models.BooleanField(default=True)
    is_control = models.BooleanField(
        default=False,
        help_text=(
            "This account is driven by a subledger - receivables, payables or "
            "inventory - so entries reach it through those workflows rather "
            "than by direct posting."
        ),
    )
    control_type = models.CharField(
        max_length=18,
        choices=[
            ("AR", "Accounts receivable"),
            ("AP", "Accounts payable"),
            ("INVENTORY", "Inventory"),
            ("OUTPUT_TAX", "Output tax"),
            ("INPUT_TAX", "Input tax"),
            ("CASH_BANK", "Cash and bank"),
            ("CUSTOMER_ADVANCE", "Customer advances"),
            ("VENDOR_ADVANCE", "Vendor advances"),
        ],
        blank=True,
        help_text=(
            "Names which subledger backs this control account, so the GL-011 "
            "reconciliation can pair GL balances with subledger totals without "
            "hard-coding account codes. Adopted from the colleague's schema."
        ),
    )
    is_contra = models.BooleanField(
        default=False,
        help_text=(
            "Carries the opposite balance to its type — sales returns and discounts "
            "(debit-natured income), purchase returns and discounts (credit-natured "
            "expense). Reports net these against their parent group (RPT-001)."
        ),
    )
    requires_party = models.BooleanField(
        default=False,
        help_text="AR/AP lines must carry a party for the subledger to reconcile.",
    )
    currency = models.ForeignKey(
        "core.Currency",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        help_text="Set only for single-currency accounts such as a foreign bank account.",
    )
    is_active = models.BooleanField(default=True)
    description = models.TextField(blank=True)

    class Meta:
        db_table = "account"
        ordering = ["code"]
        constraints = [
            # An account cannot be its own parent.
            models.CheckConstraint(
                condition=~Q(parent=F("id")), name="account_not_self_parent"
            ),
            # Case-insensitive account codes.
            models.UniqueConstraint(Upper("code"), name="account_code_unique_ci"),
            # A control account must say which subledger backs it, and only a
            # control account may (colleague's account_control_needs_flag).
            models.CheckConstraint(
                condition=(
                    (Q(is_control=True) & ~Q(control_type=""))
                    | (Q(is_control=False) & Q(control_type=""))
                ),
                name="account_control_needs_type",
            ),
            # Normal balance must agree with the account type — unless the account
            # is explicitly flagged as a contra account, which inverts it.
            models.CheckConstraint(
                condition=(
                    (
                        Q(is_contra=False)
                        & (
                            (
                                Q(account_type__in=["ASSET", "EXPENSE"])
                                & Q(normal_balance="DEBIT")
                            )
                            | (
                                Q(account_type__in=["LIABILITY", "EQUITY", "INCOME"])
                                & Q(normal_balance="CREDIT")
                            )
                        )
                    )
                    | (
                        Q(is_contra=True)
                        & (
                            (
                                Q(account_type__in=["ASSET", "EXPENSE"])
                                & Q(normal_balance="CREDIT")
                            )
                            | (
                                Q(account_type__in=["LIABILITY", "EQUITY", "INCOME"])
                                & Q(normal_balance="DEBIT")
                            )
                        )
                    )
                ),
                name="account_normal_balance_matches_type",
            ),
            # Subtype must belong to the account type.
            models.CheckConstraint(
                condition=(
                    (
                        Q(account_type="ASSET")
                        & Q(subtype__in=["CURRENT_ASSET", "NONCURRENT_ASSET"])
                    )
                    | (
                        Q(account_type="LIABILITY")
                        & Q(subtype__in=["CURRENT_LIABILITY", "NONCURRENT_LIABILITY"])
                    )
                    | (Q(account_type="EQUITY") & Q(subtype="EQUITY"))
                    | (Q(account_type="INCOME") & Q(subtype__in=["REVENUE", "OTHER_INCOME"]))
                    | (
                        Q(account_type="EXPENSE")
                        & Q(subtype__in=["COGS", "OPERATING_EXPENSE", "OTHER_EXPENSE"])
                    )
                ),
                name="account_subtype_matches_type",
            ),
        ]
        indexes = [
            models.Index(fields=["account_type", "code"], name="ix_account_type_code"),
            models.Index(fields=["is_postable", "is_active"], name="ix_account_selectable"),
        ]

    def __str__(self):
        return f"{self.code} {self.name}"

    def get_absolute_url(self):
        return reverse("core:account_edit", args=[self.pk])


class MappingKey(models.TextChoices):
    """
    Every automatic posting resolves its accounts through these keys.
    CFG-007 requires posting to stop with a clear message when one is missing.
    """

    ACCOUNTS_RECEIVABLE = "ACCOUNTS_RECEIVABLE", "Accounts receivable (control)"
    ACCOUNTS_PAYABLE = "ACCOUNTS_PAYABLE", "Accounts payable (control)"
    CUSTOMER_ADVANCE = "CUSTOMER_ADVANCE", "Customer advances / unapplied credit"
    VENDOR_ADVANCE = "VENDOR_ADVANCE", "Vendor advances / prepayments"
    INVENTORY = "INVENTORY", "Inventory (control)"
    GOODS_IN_TRANSIT = "GOODS_IN_TRANSIT", "Goods received not invoiced"
    COGS = "COGS", "Cost of goods sold"
    SALES_REVENUE = "SALES_REVENUE", "Sales revenue"
    SALES_RETURNS = "SALES_RETURNS", "Sales returns and allowances"
    SALES_DISCOUNT = "SALES_DISCOUNT", "Sales discounts granted"
    PURCHASE_EXPENSE = "PURCHASE_EXPENSE", "Non-stock purchase expense"
    PURCHASE_RETURNS = "PURCHASE_RETURNS", "Purchase returns and allowances"
    PURCHASE_DISCOUNT = "PURCHASE_DISCOUNT", "Purchase discounts received"
    OUTPUT_TAX = "OUTPUT_TAX", "Output tax payable"
    INPUT_TAX = "INPUT_TAX", "Input tax recoverable"
    TAX_NON_RECOVERABLE = "TAX_NON_RECOVERABLE", "Non-recoverable tax expense"
    FX_GAIN = "FX_GAIN", "Realised FX gain"
    FX_LOSS = "FX_LOSS", "Realised FX loss"
    ROUNDING_GAIN = "ROUNDING_GAIN", "Rounding income"
    ROUNDING_LOSS = "ROUNDING_LOSS", "Rounding expense"
    MERCHANT_FEE = "MERCHANT_FEE", "Merchant and payment-processor fees"
    INVENTORY_GAIN = "INVENTORY_GAIN", "Inventory adjustment gain"
    INVENTORY_LOSS = "INVENTORY_LOSS", "Inventory adjustment loss / write-off"
    RETAINED_EARNINGS = "RETAINED_EARNINGS", "Retained earnings"
    CURRENT_YEAR_RESULT = "CURRENT_YEAR_RESULT", "Current-year profit or loss"
    OPENING_BALANCE_EQUITY = "OPENING_BALANCE_EQUITY", "Opening balance equity"
    STOCK_TRANSFER_CLEARING = "STOCK_TRANSFER_CLEARING", "Stock transfer clearing"


class AccountMapping(TimeStampedModel):
    """CFG-007. One row per key; posting validates presence before mutating anything."""

    key = models.CharField(max_length=32, choices=MappingKey.choices, unique=True)
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="mappings")
    notes = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = "account_mapping"
        ordering = ["key"]

    def __str__(self):
        return f"{self.key} -> {self.account_id}"

    def get_absolute_url(self):
        return reverse("core:mapping_edit", args=[self.pk])


# ---------------------------------------------------------------------------
# Journal entries (GL-001..GL-009, BR-005, BR-006)
# ---------------------------------------------------------------------------
class JournalType(models.TextChoices):
    SALES = "SALES", "Sales"
    PURCHASE = "PURCHASE", "Purchase"
    CASH = "CASH", "Cash and bank"
    INVENTORY = "INVENTORY", "Inventory"
    GENERAL = "GENERAL", "Manual journal"
    OPENING = "OPENING", "Opening balance"
    CLOSING = "CLOSING", "Period / year close"


class JournalStatus(models.TextChoices):
    POSTED = "POSTED", "Posted"
    REVERSED = "REVERSED", "Reversed"


class JournalEntry(TimeStampedModel):
    """
    Immutable once written (BR-004, 5.3). Correction is by linked reversal (GL-009).

    total_debit_base / total_credit_base are maintained by the posting service and
    carry a table-level CHECK so an unbalanced entry cannot exist even briefly at
    rest; a deferred constraint trigger (see migration 0002) additionally proves the
    stored totals equal the sum of the lines at commit time.
    """

    number = models.CharField(max_length=32, unique=True)
    entry_date = models.DateField()
    fiscal_period = models.ForeignKey(
        "core.FiscalPeriod", on_delete=models.PROTECT, related_name="journal_entries"
    )
    journal_type = models.CharField(max_length=10, choices=JournalType.choices)
    status = models.CharField(
        max_length=8, choices=JournalStatus.choices, default=JournalStatus.POSTED
    )
    narration = models.TextField(blank=True)

    # Currency context of the originating document (FTD-001, BR-013).
    currency = models.ForeignKey("core.Currency", on_delete=models.PROTECT, related_name="+")
    exchange_rate = models.DecimalField(**RATE, default=Decimal("1"))

    total_debit_base = models.DecimalField(**MONEY, default=ZERO)
    total_credit_base = models.DecimalField(**MONEY, default=ZERO)

    # SC-03 traceability. Generic link plus denormalised columns for fast filtering.
    source_content_type = models.ForeignKey(
        ContentType, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    source_object_id = models.BigIntegerField(null=True, blank=True)
    source = GenericForeignKey("source_content_type", "source_object_id")
    source_doc_type = models.CharField(max_length=4, blank=True)
    source_doc_number = models.CharField(max_length=32, blank=True)

    # GL-002: retrying the same post must not duplicate the journal.
    idempotency_key = models.CharField(max_length=120, unique=True, default=uuid.uuid4)

    # GL-009 reversal pair.
    reverses = models.OneToOneField(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="reversed_by"
    )
    is_reversal = models.BooleanField(default=False)
    reversal_reason = models.TextField(blank=True)

    # GL-008: an authorised manual journal, as opposed to an automatic posting.
    is_manual = models.BooleanField(default=False)

    posted_at = models.DateTimeField(null=True, blank=True)
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="posted_journals",
    )

    class Meta:
        db_table = "journal_entry"
        ordering = ["-entry_date", "-id"]
        verbose_name_plural = "journal entries"
        constraints = [
            # BR-006: the non-negotiable rule.
            models.CheckConstraint(
                condition=Q(total_debit_base=F("total_credit_base")),
                name="journal_entry_balanced",
            ),
            models.CheckConstraint(
                condition=Q(total_debit_base__gte=0) & Q(total_credit_base__gte=0),
                name="journal_entry_totals_nonneg",
            ),
            models.CheckConstraint(
                condition=Q(exchange_rate__gt=0), name="journal_entry_rate_positive"
            ),
            models.CheckConstraint(
                condition=Q(is_reversal=False) | Q(reverses__isnull=False),
                name="journal_entry_reversal_has_origin",
            ),
            # GL-008: a manual journal must not claim an operational source
            # document (colleague's journal_entry_manual_has_no_source).
            models.CheckConstraint(
                condition=(
                    Q(is_manual=False)
                    | (Q(source_content_type__isnull=True) & Q(source_doc_type=""))
                ),
                name="journal_entry_manual_has_no_source",
            ),
            # The generic source link is all-or-nothing.
            models.CheckConstraint(
                condition=(
                    (Q(source_content_type__isnull=True) & Q(source_object_id__isnull=True))
                    | (
                        Q(source_content_type__isnull=False)
                        & Q(source_object_id__isnull=False)
                    )
                ),
                name="journal_entry_source_pair",
            ),
        ]
        indexes = [
            models.Index(fields=["entry_date"], name="ix_je_date"),
            models.Index(fields=["fiscal_period", "entry_date"], name="ix_je_period_date"),
            models.Index(
                fields=["source_content_type", "source_object_id"], name="ix_je_source"
            ),
            models.Index(fields=["source_doc_type", "source_doc_number"], name="ix_je_doc"),
            models.Index(fields=["journal_type", "entry_date"], name="ix_je_type_date"),
        ]

    def __str__(self):
        return self.number


class JournalLine(models.Model):
    """
    One side of one posting. Exactly one of debit/credit is non-zero (BR-006).

    Base-currency columns drive every financial statement (FTD-003); the
    transaction-currency columns preserve the original amounts (FTD-001, FTD-004).
    """

    entry = models.ForeignKey(JournalEntry, on_delete=models.PROTECT, related_name="lines")
    line_no = models.PositiveSmallIntegerField()
    account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="journal_lines"
    )
    description = models.CharField(max_length=255, blank=True)

    debit_base = models.DecimalField(**MONEY, default=ZERO)
    credit_base = models.DecimalField(**MONEY, default=ZERO)
    debit_txn = models.DecimalField(**MONEY, default=ZERO)
    credit_txn = models.DecimalField(**MONEY, default=ZERO)
    currency = models.ForeignKey("core.Currency", on_delete=models.PROTECT, related_name="+")
    exchange_rate = models.DecimalField(**RATE, default=Decimal("1"))

    # Subledger dimensions (GL-011, RPT-004, RPT-019).
    customer = models.ForeignKey(
        "parties.Customer", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    vendor = models.ForeignKey(
        "parties.Vendor", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    product = models.ForeignKey(
        "catalog.Product", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    warehouse = models.ForeignKey(
        "inventory.Warehouse",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    tax_code = models.ForeignKey(
        "core.TaxCode", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    money_account = models.ForeignKey(
        "payments.MoneyAccount",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )

    class Meta:
        db_table = "journal_line"
        ordering = ["entry", "line_no"]
        constraints = [
            models.UniqueConstraint(
                fields=["entry", "line_no"], name="journal_line_unique_no"
            ),
            models.CheckConstraint(
                condition=(
                    Q(debit_base__gte=0)
                    & Q(credit_base__gte=0)
                    & Q(debit_txn__gte=0)
                    & Q(credit_txn__gte=0)
                ),
                name="journal_line_amounts_nonneg",
            ),
            # Exactly one side carries value, and it is never zero on both sides.
            models.CheckConstraint(
                condition=(
                    (Q(debit_base__gt=0) & Q(credit_base=0))
                    | (Q(credit_base__gt=0) & Q(debit_base=0))
                ),
                name="journal_line_debit_xor_credit",
            ),
            # The transaction side must agree with the base side.
            models.CheckConstraint(
                condition=(
                    (Q(debit_base__gt=0) & Q(credit_txn=0))
                    | (Q(credit_base__gt=0) & Q(debit_txn=0))
                ),
                name="journal_line_txn_side_matches_base",
            ),
            models.CheckConstraint(
                condition=Q(exchange_rate__gt=0), name="journal_line_rate_positive"
            ),
            # A line cannot be both a customer and a vendor line.
            models.CheckConstraint(
                condition=at_most_one("customer", "vendor"),
                name="journal_line_single_party",
            ),
        ]
        indexes = [
            models.Index(fields=["account", "entry"], name="ix_jl_account_entry"),
            models.Index(fields=["customer"], name="ix_jl_customer"),
            models.Index(fields=["vendor"], name="ix_jl_vendor"),
            models.Index(fields=["money_account"], name="ix_jl_money_account"),
            models.Index(fields=["product"], name="ix_jl_product"),
            models.Index(fields=["tax_code"], name="ix_jl_tax_code"),
        ]

    def __str__(self):
        side = f"Dr {self.debit_base}" if self.debit_base else f"Cr {self.credit_base}"
        return f"{self.entry_id}/{self.line_no} {self.account_id} {side}"


# ---------------------------------------------------------------------------
# Opening balances (BR-021)
# ---------------------------------------------------------------------------
class OpeningBalanceBatch(TimeStampedModel):
    """
    Opening balances arrive as one balanced journal plus subledger detail lines,
    never by editing a displayed balance (BR-021).
    """

    code = models.CharField(max_length=32, unique=True)
    as_of_date = models.DateField()
    description = models.CharField(max_length=255, blank=True)
    journal_entry = models.OneToOneField(
        JournalEntry,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="opening_batch",
    )
    is_posted = models.BooleanField(default=False)

    class Meta:
        db_table = "opening_balance_batch"
        ordering = ["-as_of_date"]

    def __str__(self):
        return self.code


# ---------------------------------------------------------------------------
# Posting link (SC-03, GL-002)  — adopted from the colleague's schema
# ---------------------------------------------------------------------------
class PostingEffect(models.TextChoices):
    JOURNAL = "JOURNAL", "Journal entry"
    STOCK = "STOCK", "Stock movement"


class PostingLink(models.Model):
    """
    One row per effect a source document produced.

    The document tables each carry their own `journal_entry` FK, which answers
    "what did this invoice post?". This table answers the harder questions:
    *everything* a document produced (journal and stock, in one place), and
    whether a given posting request has already been executed.

    The unique `idempotency_key` is what makes GL-002 hold under a retried or
    double-submitted post: the second attempt collides here and the whole
    transaction rolls back, rather than quietly writing a second journal.

    Taken from the colleague's SQL schema, which modelled this better than the
    original Django version's scattered nullable FKs did.
    """

    source_content_type = models.ForeignKey(
        ContentType, on_delete=models.PROTECT, related_name="+"
    )
    source_object_id = models.BigIntegerField()
    source = GenericForeignKey("source_content_type", "source_object_id")
    source_doc_type = models.CharField(max_length=4, blank=True)
    source_doc_number = models.CharField(max_length=32, blank=True)

    effect_type = models.CharField(max_length=8, choices=PostingEffect.choices)
    journal_entry = models.ForeignKey(
        JournalEntry,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="posting_links",
    )
    stock_movement = models.ForeignKey(
        "inventory.StockMovement",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="posting_links",
    )
    idempotency_key = models.CharField(max_length=120, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "posting_link"
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=exactly_one("journal_entry", "stock_movement"),
                name="posting_link_exactly_one_effect",
            ),
            # The effect column must agree with which FK is filled.
            models.CheckConstraint(
                condition=(
                    (Q(effect_type="JOURNAL") & Q(journal_entry__isnull=False))
                    | (Q(effect_type="STOCK") & Q(stock_movement__isnull=False))
                ),
                name="posting_link_effect_matches_target",
            ),
        ]
        indexes = [
            models.Index(
                fields=["source_content_type", "source_object_id"],
                name="ix_posting_link_source",
            ),
            models.Index(
                fields=["source_doc_type", "source_doc_number"],
                name="ix_posting_link_doc",
            ),
            models.Index(fields=["journal_entry"], name="ix_posting_link_journal"),
            models.Index(fields=["stock_movement"], name="ix_posting_link_stock"),
        ]

    def __str__(self):
        return f"{self.source_doc_type}{self.source_doc_number} -> {self.effect_type}"
