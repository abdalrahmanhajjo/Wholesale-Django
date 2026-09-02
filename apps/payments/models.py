"""
Money: cash/bank accounts, methods, payments, allocations and refunds.

BRD coverage: CFG-002, PAY-001..PAY-012, RET-004, RET-007, RET-009,
BR-008, BR-009, BR-014, BR-016, FTD-004, FTD-005, RPT-012, RPT-013, RPT-022.
"""

from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q
from django.urls import reverse

from apps.core.expressions import exactly_one
from apps.core.models import MONEY, RATE, DocumentStatus, TimeStampedModel

ZERO = Decimal("0")


# ---------------------------------------------------------------------------
# Cash and bank (CFG-002, RPT-013)
# ---------------------------------------------------------------------------
class MoneyAccountType(models.TextChoices):
    CASH = "CASH", "Cash on hand"
    BANK = "BANK", "Bank account"
    CARD = "CARD", "Card / merchant account"
    OTHER = "OTHER", "Other"


class MoneyAccount(TimeStampedModel):
    """
    A till or bank account. Every payment moves through exactly one, and each
    maps to one ledger account so RPT-013 reconciles to the GL by construction.
    """

    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=100)
    account_type = models.CharField(
        max_length=6, choices=MoneyAccountType.choices, default=MoneyAccountType.BANK
    )
    currency = models.ForeignKey("core.Currency", on_delete=models.PROTECT, related_name="+")
    gl_account = models.ForeignKey(
        "ledger.Account", on_delete=models.PROTECT, related_name="money_accounts"
    )
    bank_name = models.CharField(max_length=100, blank=True)
    account_number = models.CharField(max_length=64, blank=True)
    iban = models.CharField(max_length=64, blank=True)
    swift = models.CharField(max_length=20, blank=True)
    allow_negative_balance = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "money_account"
        ordering = ["code"]
        constraints = [
            # One ledger account should back one money account, or bank
            # reconciliation later becomes ambiguous.
            models.UniqueConstraint(fields=["gl_account"], name="money_account_gl_unique"),
        ]

    def __str__(self):
        return f"{self.code} {self.name}"


class PaymentMethod(models.Model):
    """PAY-002. Configurable rather than an enum, so new methods need no migration."""

    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=100)
    requires_reference = models.BooleanField(
        default=False, help_text="Cheque number, transfer reference, card auth code."
    )
    default_money_account = models.ForeignKey(
        MoneyAccount, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "payment_method"
        ordering = ["code"]

    def __str__(self):
        return self.name


# ---------------------------------------------------------------------------
# Payments (PAY-001, PAY-004, PAY-008, PAY-010)
# ---------------------------------------------------------------------------
class PaymentDirection(models.TextChoices):
    RECEIPT = "RECEIPT", "Customer receipt (money in)"
    PAYMENT = "PAYMENT", "Vendor payment (money out)"


class Payment(TimeStampedModel):
    number = models.CharField(max_length=32, unique=True)
    direction = models.CharField(max_length=8, choices=PaymentDirection.choices)
    payment_date = models.DateField(db_index=True)
    posting_date = models.DateField()
    fiscal_period = models.ForeignKey(
        "core.FiscalPeriod", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    customer = models.ForeignKey(
        "parties.Customer",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="payments",
    )
    vendor = models.ForeignKey(
        "parties.Vendor",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="payments",
    )

    currency = models.ForeignKey("core.Currency", on_delete=models.PROTECT, related_name="+")
    exchange_rate = models.DecimalField(**RATE, default=Decimal("1"))
    amount_txn = models.DecimalField(**MONEY)
    amount_base = models.DecimalField(**MONEY, default=ZERO)
    allocated_txn = models.DecimalField(**MONEY, default=ZERO)
    unallocated_txn = models.DecimalField(
        **MONEY,
        default=ZERO,
        help_text=(
            "Money received but not yet matched to an invoice. It sits as a "
            "credit on the account until it is allocated."
        ),
    )

    method = models.ForeignKey(
        PaymentMethod, on_delete=models.PROTECT, related_name="payments"
    )
    money_account = models.ForeignKey(
        MoneyAccount, on_delete=models.PROTECT, related_name="payments"
    )
    reference = models.CharField(max_length=64, blank=True)
    narration = models.TextField(blank=True)

    status = models.CharField(
        max_length=10, choices=DocumentStatus.choices, default=DocumentStatus.DRAFT
    )
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

    # PAY-010 / PAY-011 controlled reversal
    is_reversed = models.BooleanField(default=False)
    reversed_at = models.DateTimeField(null=True, blank=True)
    reversed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    reversal_reason = models.TextField(blank=True)
    reversal_journal = models.ForeignKey(
        "ledger.JournalEntry",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )

    # PAY-009 printable voucher
    voucher_printed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "payment"
        ordering = ["-payment_date", "-id"]
        constraints = [
            # A payment belongs to exactly one party, on the matching side.
            models.CheckConstraint(
                condition=(
                    (
                        Q(direction="RECEIPT")
                        & Q(customer__isnull=False)
                        & Q(vendor__isnull=True)
                    )
                    | (
                        Q(direction="PAYMENT")
                        & Q(vendor__isnull=False)
                        & Q(customer__isnull=True)
                    )
                ),
                name="payment_party_matches_direction",
            ),
            models.CheckConstraint(
                condition=Q(amount_txn__gt=0), name="payment_amount_positive"
            ),
            models.CheckConstraint(
                condition=Q(exchange_rate__gt=0), name="payment_rate_positive"
            ),
            # BR-008: allocations can never exceed the payment.
            models.CheckConstraint(
                condition=Q(allocated_txn__gte=0) & Q(allocated_txn__lte=F("amount_txn")),
                name="payment_allocated_within_amount",
            ),
            # BR-009: the unapplied remainder is derived and never negative.
            models.CheckConstraint(
                condition=Q(unallocated_txn=F("amount_txn") - F("allocated_txn")),
                name="payment_unallocated_is_derived",
            ),
            models.CheckConstraint(
                condition=Q(unallocated_txn__gte=0), name="payment_unallocated_nonneg"
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(status__in=["POSTED", "PARTIAL", "COMPLETED", "REVERSED"])
                    | Q(journal_entry__isnull=False)
                ),
                name="payment_posted_has_journal",
            ),
            # PAY-010: a reversal is attributable and reasoned.
            models.CheckConstraint(
                condition=Q(is_reversed=False)
                | (Q(reversed_by__isnull=False) & ~Q(reversal_reason="")),
                name="payment_reversal_attributable",
            ),
        ]
        indexes = [
            models.Index(fields=["customer", "-payment_date"], name="ix_pay_customer_date"),
            models.Index(fields=["vendor", "-payment_date"], name="ix_pay_vendor_date"),
            models.Index(fields=["money_account", "-payment_date"], name="ix_pay_money_date"),
            models.Index(fields=["status", "-payment_date"], name="ix_pay_status_date"),
            # PAY-004: the "available advances" lookup.
            models.Index(
                fields=["customer", "vendor"],
                condition=Q(unallocated_txn__gt=0),
                name="ix_pay_unapplied",
            ),
        ]

    def __str__(self):
        return self.number

    @property
    def party(self):
        """The customer or vendor on the side selected by ``direction``."""
        return self.customer if self.direction == PaymentDirection.RECEIPT else self.vendor

    def get_absolute_url(self):
        return reverse("payments:payment_detail", args=[self.pk])

    def clean(self):
        """Cross-table rules that cannot be expressed as database CHECKs."""
        super().clean()
        errors = {}
        if self.direction == PaymentDirection.RECEIPT:
            if not self.customer_id:
                errors["customer"] = "A customer is required for a receipt."
            if self.vendor_id:
                errors["vendor"] = "A receipt cannot be assigned to a vendor."
        elif self.direction == PaymentDirection.PAYMENT:
            if not self.vendor_id:
                errors["vendor"] = "A vendor is required for a vendor payment."
            if self.customer_id:
                errors["customer"] = "A vendor payment cannot be assigned to a customer."

        if self.method_id and self.method.requires_reference and not self.reference.strip():
            errors["reference"] = (
                f"A reference is required for payment method {self.method.name}."
            )
        if self.method_id and not self.method.is_active:
            errors["method"] = "Select an active payment method."
        if self.money_account_id and not self.money_account.is_active:
            errors["money_account"] = "Select an active money account."
        if self.customer_id and not self.customer.is_active:
            errors["customer"] = "Select an active customer."
        if self.vendor_id and not self.vendor.is_active:
            errors["vendor"] = "Select an active vendor."
        if errors:
            raise ValidationError(errors)


# ---------------------------------------------------------------------------
# Allocation (PAY-003, PAY-005, PAY-007, RET-004, RET-007)
# ---------------------------------------------------------------------------
class Allocation(TimeStampedModel):
    """
    Links a money/credit SOURCE to an open-item TARGET.

    Sources: a payment (cash), a sales credit note (customer credit), or a
    vendor debit note (vendor credit).
    Targets: a sales invoice or a purchase bill.

    Applying a credit note to an invoice creates no second cash movement
    (PAY-005) because the source is the credit note, not a payment.

    Cross-currency settlement (FTD-005, BR-014): `source_amount_txn` is in the
    source's currency, `target_amount_txn` in the target's, `amount_base` is the
    common base figure, and `fx_gain_loss_base` carries the realised difference
    between the target's posting rate and this settlement rate.
    """

    allocation_date = models.DateField(db_index=True)

    # Discriminators adopted from the colleague's schema. They are redundant with
    # the nullable FKs below, but they let one CHECK express the whole rule
    # ("a customer-side credit may only settle a sales invoice"), and they make
    # allocation queries and indexes readable without four OR'd IS NULL tests.
    party_side = models.CharField(
        max_length=8, choices=[("CUSTOMER", "Customer"), ("VENDOR", "Vendor")]
    )
    source_type = models.CharField(
        max_length=18,
        choices=[
            ("PAYMENT", "Payment"),
            ("SALES_CREDIT_NOTE", "Sales credit note"),
            ("VENDOR_DEBIT_NOTE", "Vendor debit note"),
        ],
    )
    target_type = models.CharField(
        max_length=14,
        choices=[("SALES_INVOICE", "Sales invoice"), ("PURCHASE_BILL", "Purchase bill")],
    )

    # Denormalised party, so a customer statement is one indexed scan of this
    # table rather than a union across the source documents.
    customer = models.ForeignKey(
        "parties.Customer",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="allocations",
    )
    vendor = models.ForeignKey(
        "parties.Vendor",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="allocations",
    )

    # --- source (exactly one) ---
    payment = models.ForeignKey(
        Payment, null=True, blank=True, on_delete=models.PROTECT, related_name="allocations"
    )
    sales_credit_note = models.ForeignKey(
        "sales.SalesCreditNote",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="allocations",
    )
    vendor_debit_note = models.ForeignKey(
        "purchases.VendorDebitNote",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="allocations",
    )

    # --- target (exactly one) ---
    sales_invoice = models.ForeignKey(
        "sales.SalesInvoice",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="allocations",
    )
    purchase_bill = models.ForeignKey(
        "purchases.PurchaseBill",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="allocations",
    )

    source_amount_txn = models.DecimalField(**MONEY)
    target_amount_txn = models.DecimalField(**MONEY)
    amount_base = models.DecimalField(**MONEY, default=ZERO)
    settlement_rate = models.DecimalField(**RATE, default=Decimal("1"))
    fx_gain_loss_base = models.DecimalField(
        **MONEY,
        default=ZERO,
        help_text=(
            "Exchange difference realised when this payment settled, against "
            "the rate on the original document. Positive is a gain."
        ),
    )
    fx_journal_entry = models.ForeignKey(
        "ledger.JournalEntry",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    journal_entry = models.ForeignKey(
        "ledger.JournalEntry",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="allocations",
        help_text="Set when the allocation itself posts (credit application).",
    )
    is_reversed = models.BooleanField(default=False)
    reversal_reason = models.CharField(max_length=255, blank=True)
    notes = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = "payment_allocation"
        ordering = ["-allocation_date", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(source_amount_txn__gt=0) & Q(target_amount_txn__gt=0),
                name="allocation_amounts_positive",
            ),
            models.CheckConstraint(
                condition=Q(settlement_rate__gt=0), name="allocation_rate_positive"
            ),
            # Exactly one source, exactly one target, exactly one party.
            models.CheckConstraint(
                condition=exactly_one("payment", "sales_credit_note", "vendor_debit_note"),
                name="allocation_exactly_one_source",
            ),
            models.CheckConstraint(
                condition=exactly_one("sales_invoice", "purchase_bill"),
                name="allocation_exactly_one_target",
            ),
            models.CheckConstraint(
                condition=exactly_one("customer", "vendor"),
                name="allocation_exactly_one_party",
            ),
            # Each discriminator must agree with the FK it names.
            models.CheckConstraint(
                condition=(
                    (Q(source_type="PAYMENT") & Q(payment__isnull=False))
                    | (Q(source_type="SALES_CREDIT_NOTE") & Q(sales_credit_note__isnull=False))
                    | (Q(source_type="VENDOR_DEBIT_NOTE") & Q(vendor_debit_note__isnull=False))
                ),
                name="allocation_source_matches_type",
            ),
            models.CheckConstraint(
                condition=(
                    (Q(target_type="SALES_INVOICE") & Q(sales_invoice__isnull=False))
                    | (Q(target_type="PURCHASE_BILL") & Q(purchase_bill__isnull=False))
                ),
                name="allocation_target_matches_type",
            ),
            models.CheckConstraint(
                condition=(
                    (Q(party_side="CUSTOMER") & Q(customer__isnull=False))
                    | (Q(party_side="VENDOR") & Q(vendor__isnull=False))
                ),
                name="allocation_party_matches_side",
            ),
            # The whole side rule in one constraint, taken from the colleague's
            # allocation_side_consistency: a customer-side credit settles a sales
            # invoice and nothing else; a vendor-side credit settles a bill.
            models.CheckConstraint(
                condition=(
                    (
                        Q(party_side="CUSTOMER")
                        & Q(target_type="SALES_INVOICE")
                        & Q(source_type__in=["PAYMENT", "SALES_CREDIT_NOTE"])
                    )
                    | (
                        Q(party_side="VENDOR")
                        & Q(target_type="PURCHASE_BILL")
                        & Q(source_type__in=["PAYMENT", "VENDOR_DEBIT_NOTE"])
                    )
                ),
                name="allocation_side_consistency",
            ),
            # PAY-007: the same source may not be applied twice to the same target.
            models.UniqueConstraint(
                fields=["payment", "sales_invoice"],
                condition=Q(
                    payment__isnull=False, sales_invoice__isnull=False, is_reversed=False
                ),
                name="allocation_unique_payment_invoice",
            ),
            models.UniqueConstraint(
                fields=["payment", "purchase_bill"],
                condition=Q(
                    payment__isnull=False, purchase_bill__isnull=False, is_reversed=False
                ),
                name="allocation_unique_payment_bill",
            ),
            models.UniqueConstraint(
                fields=["sales_credit_note", "sales_invoice"],
                condition=Q(sales_credit_note__isnull=False, is_reversed=False),
                name="allocation_unique_credit_invoice",
            ),
            models.UniqueConstraint(
                fields=["vendor_debit_note", "purchase_bill"],
                condition=Q(vendor_debit_note__isnull=False, is_reversed=False),
                name="allocation_unique_debit_bill",
            ),
        ]
        indexes = [
            models.Index(fields=["sales_invoice"], name="ix_alloc_invoice"),
            models.Index(fields=["purchase_bill"], name="ix_alloc_bill"),
            models.Index(fields=["payment"], name="ix_alloc_payment"),
            models.Index(fields=["customer", "allocation_date"], name="ix_alloc_customer"),
            models.Index(fields=["vendor", "allocation_date"], name="ix_alloc_vendor"),
        ]

    def __str__(self):
        return f"Allocation {self.pk}: {self.target_amount_txn}"


# ---------------------------------------------------------------------------
# Refunds (RET-004, RET-007, RET-009, BR-016)
# ---------------------------------------------------------------------------
class RefundDirection(models.TextChoices):
    TO_CUSTOMER = "TO_CUSTOMER", "Refund paid to customer"
    FROM_VENDOR = "FROM_VENDOR", "Refund received from vendor"


class Refund(TimeStampedModel):
    """
    Converts an unapplied credit into a cash movement. The source is either a
    credit/debit note or an unapplied advance payment; BR-016 caps the amount at
    the credit still available, which the posting service checks under a row lock.
    """

    number = models.CharField(max_length=32, unique=True)
    direction = models.CharField(max_length=12, choices=RefundDirection.choices)
    refund_date = models.DateField(db_index=True)
    posting_date = models.DateField()
    fiscal_period = models.ForeignKey(
        "core.FiscalPeriod", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    customer = models.ForeignKey(
        "parties.Customer",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="refunds",
    )
    vendor = models.ForeignKey(
        "parties.Vendor",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="refunds",
    )

    # Source of the credit being refunded (at most one document reference).
    sales_credit_note = models.ForeignKey(
        "sales.SalesCreditNote",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="refunds",
    )
    vendor_debit_note = models.ForeignKey(
        "purchases.VendorDebitNote",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="refunds",
    )
    source_payment = models.ForeignKey(
        Payment,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="refunds",
        help_text="Refund of an unapplied advance.",
    )

    currency = models.ForeignKey("core.Currency", on_delete=models.PROTECT, related_name="+")
    exchange_rate = models.DecimalField(**RATE, default=Decimal("1"))
    amount_txn = models.DecimalField(**MONEY)
    amount_base = models.DecimalField(**MONEY, default=ZERO)

    method = models.ForeignKey(PaymentMethod, on_delete=models.PROTECT, related_name="refunds")
    money_account = models.ForeignKey(
        MoneyAccount, on_delete=models.PROTECT, related_name="refunds"
    )
    reference = models.CharField(max_length=64, blank=True)
    reason = models.TextField(help_text="Mandatory (RET-008).")

    status = models.CharField(
        max_length=10, choices=DocumentStatus.choices, default=DocumentStatus.DRAFT
    )
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
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )

    class Meta:
        db_table = "refund"
        ordering = ["-refund_date", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(amount_txn__gt=0), name="refund_amount_positive"
            ),
            models.CheckConstraint(
                condition=Q(exchange_rate__gt=0), name="refund_rate_positive"
            ),
            models.CheckConstraint(
                condition=(
                    (
                        Q(direction="TO_CUSTOMER")
                        & Q(customer__isnull=False)
                        & Q(vendor__isnull=True)
                    )
                    | (
                        Q(direction="FROM_VENDOR")
                        & Q(vendor__isnull=False)
                        & Q(customer__isnull=True)
                    )
                ),
                name="refund_party_matches_direction",
            ),
            # Exactly one credit source.
            models.CheckConstraint(
                condition=exactly_one(
                    "sales_credit_note", "vendor_debit_note", "source_payment"
                ),
                name="refund_exactly_one_source",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(status__in=["POSTED", "COMPLETED", "REVERSED"])
                    | Q(journal_entry__isnull=False)
                ),
                name="refund_posted_has_journal",
            ),
            models.CheckConstraint(condition=~Q(reason=""), name="refund_reason_required"),
        ]
        indexes = [
            models.Index(fields=["customer", "-refund_date"], name="ix_refund_customer"),
            models.Index(fields=["vendor", "-refund_date"], name="ix_refund_vendor"),
            models.Index(fields=["money_account", "-refund_date"], name="ix_refund_money"),
        ]

    def __str__(self):
        return self.number
