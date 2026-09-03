"""
Customers, vendors, their addresses and contacts.

BRD coverage: PTY-001..PTY-008, BR-007, PAY-004, RPT-006..RPT-009, RPT-022.

Design note: Customer and Vendor are separate concrete tables rather than one
"party with a flag". They carry different control accounts, different number
series, different credit semantics, and every sales/purchase FK then points at
exactly one table — which keeps the AR and AP subledger reconciliations (GL-011)
simple and lets the database, not the application, reject a vendor on a sales
invoice. Shared columns live in the PartyBase abstract model.
"""

from decimal import Decimal

from django.contrib.postgres.indexes import GinIndex, OpClass
from django.db import models
from django.db.models import Q
from django.db.models.functions import Upper

from apps.core.expressions import exactly_one
from apps.core.models import MONEY, TimeStampedModel

ZERO = Decimal("0")


class PartyBase(TimeStampedModel):
    code = models.CharField(max_length=20)
    name = models.CharField(max_length=200)
    legal_name = models.CharField(max_length=200, blank=True)
    tax_id = models.CharField(max_length=50, blank=True, db_index=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=50, blank=True)
    website = models.CharField(max_length=200, blank=True)

    currency = models.ForeignKey(
        "core.Currency",
        on_delete=models.PROTECT,
        related_name="+",
        help_text=(
            "Used on new documents for this party. Individual documents can "
            "still be raised in another currency."
        ),
    )
    payment_term = models.ForeignKey(
        "core.PaymentTerm", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    notes = models.TextField(blank=True)

    # PTY-008: deactivate, never delete, once posted history exists.
    is_active = models.BooleanField(default=True)
    deactivated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        abstract = True

    def __str__(self):
        return f"{self.code} {self.name}"


class Customer(PartyBase):
    # PTY-004 credit control
    credit_limit = models.DecimalField(**MONEY, default=ZERO)
    credit_hold = models.BooleanField(default=False)
    credit_hold_reason = models.CharField(max_length=255, blank=True)

    # Optional per-customer override of the AR control account (CFG-007).
    receivable_account = models.ForeignKey(
        "ledger.Account", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    advance_account = models.ForeignKey(
        "ledger.Account", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    default_tax_code = models.ForeignKey(
        "core.TaxCode", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    default_warehouse = models.ForeignKey(
        "inventory.Warehouse",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    salesperson = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        db_table = "customer"
        ordering = ["code"]
        constraints = [
            # PTY-007: exact duplicate codes are blocked — case-insensitively, so
            # "ACME-01" and "acme-01" cannot both exist. (The colleague's schema
            # achieved this with a citext column; Django removed its CIText fields
            # in 5.1, and a functional unique index is the supported equivalent.)
            models.UniqueConstraint(Upper("code"), name="customer_code_unique_ci"),
            models.CheckConstraint(
                condition=Q(credit_limit__gte=0), name="customer_credit_limit_nonneg"
            ),
        ]
        indexes = [
            models.Index(fields=["name"], name="ix_customer_name"),
            models.Index(fields=["is_active", "code"], name="ix_customer_selectable"),
            # PTY-007: "flag likely duplicate tax IDs and contact values" needs
            # fuzzy matching, which a B-tree cannot do. Trigram GIN indexes make
            # name/tax-ID similarity searches fast.
            GinIndex(
                OpClass(Upper("name"), name="gin_trgm_ops"), name="ix_customer_name_trgm"
            ),
            GinIndex(
                OpClass(Upper("tax_id"), name="gin_trgm_ops"),
                name="ix_customer_taxid_trgm",
            ),
        ]

    def get_absolute_url(self):
        from django.urls import reverse

        return reverse("parties:customer_detail", args=[self.pk])


class Vendor(PartyBase):
    payable_account = models.ForeignKey(
        "ledger.Account", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    advance_account = models.ForeignKey(
        "ledger.Account", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    default_tax_code = models.ForeignKey(
        "core.TaxCode", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    default_expense_account = models.ForeignKey(
        "ledger.Account", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        db_table = "vendor"
        ordering = ["code"]
        constraints = [
            models.UniqueConstraint(Upper("code"), name="vendor_code_unique_ci"),
        ]
        indexes = [
            models.Index(fields=["name"], name="ix_vendor_name"),
            models.Index(fields=["is_active", "code"], name="ix_vendor_selectable"),
            GinIndex(OpClass(Upper("name"), name="gin_trgm_ops"), name="ix_vendor_name_trgm"),
            GinIndex(
                OpClass(Upper("tax_id"), name="gin_trgm_ops"), name="ix_vendor_taxid_trgm"
            ),
        ]

    def get_absolute_url(self):
        from django.urls import reverse

        return reverse("parties:vendor_detail", args=[self.pk])


# ---------------------------------------------------------------------------
# Addresses and contacts (PTY-003)
# ---------------------------------------------------------------------------
class AddressType(models.TextChoices):
    BILLING = "BILLING", "Billing"
    SHIPPING = "SHIPPING", "Shipping"
    BOTH = "BOTH", "Billing and shipping"


class Address(models.Model):
    """
    Attached to exactly one customer or one vendor. Documents snapshot the text
    of the selected address so historical copies never change (PTY-003).
    """

    customer = models.ForeignKey(
        Customer, null=True, blank=True, on_delete=models.CASCADE, related_name="addresses"
    )
    vendor = models.ForeignKey(
        Vendor, null=True, blank=True, on_delete=models.CASCADE, related_name="addresses"
    )
    label = models.CharField(max_length=100, blank=True)
    address_type = models.CharField(
        max_length=8, choices=AddressType.choices, default=AddressType.BOTH
    )
    line1 = models.CharField(max_length=200)
    line2 = models.CharField(max_length=200, blank=True)
    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=100, blank=True)
    postal_code = models.CharField(max_length=20, blank=True)
    country = models.CharField(max_length=100, blank=True)
    is_default = models.BooleanField(default=False)

    class Meta:
        db_table = "party_address"
        constraints = [
            # num_nonnulls idiom adopted from the colleague's schema.
            models.CheckConstraint(
                condition=exactly_one("customer", "vendor"),
                name="address_exactly_one_party",
            ),
            models.UniqueConstraint(
                fields=["customer", "address_type"],
                condition=Q(is_default=True, customer__isnull=False),
                name="address_one_default_per_customer_type",
            ),
            models.UniqueConstraint(
                fields=["vendor", "address_type"],
                condition=Q(is_default=True, vendor__isnull=False),
                name="address_one_default_per_vendor_type",
            ),
        ]

    def __str__(self):
        return self.label or self.line1

    def as_text(self):
        parts = [self.line1, self.line2, self.city, self.state, self.postal_code, self.country]
        return "\n".join(p for p in parts if p)


class Contact(models.Model):
    customer = models.ForeignKey(
        Customer, null=True, blank=True, on_delete=models.CASCADE, related_name="contacts"
    )
    vendor = models.ForeignKey(
        Vendor, null=True, blank=True, on_delete=models.CASCADE, related_name="contacts"
    )
    name = models.CharField(max_length=150)
    job_title = models.CharField(max_length=100, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=50, blank=True)
    is_primary = models.BooleanField(default=False)

    class Meta:
        db_table = "party_contact"
        constraints = [
            models.CheckConstraint(
                condition=exactly_one("customer", "vendor"),
                name="contact_exactly_one_party",
            ),
            models.UniqueConstraint(
                fields=["customer"],
                condition=Q(is_primary=True, customer__isnull=False),
                name="contact_one_primary_per_customer",
            ),
            models.UniqueConstraint(
                fields=["vendor"],
                condition=Q(is_primary=True, vendor__isnull=False),
                name="contact_one_primary_per_vendor",
            ),
        ]

    def __str__(self):
        return self.name
