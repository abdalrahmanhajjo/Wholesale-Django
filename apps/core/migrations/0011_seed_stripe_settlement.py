"""
Reference data for card-processor settlement (PAY-013).

A processor never hands over what the customer paid. It holds the money, keeps
a cut, and pays out the rest days later, so two things the chart of accounts
did not have are needed before a Stripe receipt can be recorded honestly:

  1140  a clearing account of its own. Money sits there from the moment the
        customer pays until the payout lands in the bank. 1130 already backs
        the generic CARD-01 money account and `money_account_gl_unique` allows
        only one money account per ledger account, so sharing it is not an
        option — and sharing it would make the two impossible to reconcile
        separately anyway.
  6510  the fee, as its own expense line rather than folded into 6500 Bank
        Charges. Whether card acceptance pays for itself is a question a
        wholesaler actually asks, and it cannot be answered from a total that
        also contains wire charges and account fees.

Both are placeholders pending accountant sign-off, like every other code seeded
in 0004, and MERCHANT_FEE can be re-pointed at any account from Settings ->
Account Mappings without a migration.

Idempotent, in the same way as 0004: keyed on natural keys throughout, so
re-running repairs a partial seed instead of duplicating it.
"""

from django.db import migrations

ACCOUNTS = [
    # (code, name, type, subtype, normal_balance, parent, control_type)
    (
        "1140",
        "Stripe Clearing",
        "ASSET",
        "CURRENT_ASSET",
        "DEBIT",
        "1100",
        "CASH_BANK",
    ),
    (
        "6510",
        "Merchant and Processor Fees",
        "EXPENSE",
        "OPERATING_EXPENSE",
        "DEBIT",
        "6000",
        "",
    ),
]


def seed(apps, schema_editor):
    Account = apps.get_model("ledger", "Account")
    AccountMapping = apps.get_model("ledger", "AccountMapping")
    Currency = apps.get_model("core", "Currency")
    MoneyAccount = apps.get_model("payments", "MoneyAccount")
    PaymentMethod = apps.get_model("payments", "PaymentMethod")

    for code, name, account_type, subtype, normal, parent, control in ACCOUNTS:
        Account.objects.update_or_create(
            code=code,
            defaults={
                "name": name,
                "account_type": account_type,
                "subtype": subtype,
                "normal_balance": normal,
                "parent": Account.objects.filter(code=parent).first(),
                "is_postable": True,
                "is_control": bool(control),
                "control_type": control,
                "is_contra": False,
                "requires_party": False,
                "is_active": True,
            },
        )

    AccountMapping.objects.update_or_create(
        key="MERCHANT_FEE",
        defaults={
            "account": Account.objects.get(code="6510"),
            "notes": "Card, gateway and processor fees deducted from settlement.",
        },
    )

    # Follow the base currency rather than hardcoding USD: 0004 flags the USD
    # base as a placeholder, and a clearing account denominated in something the
    # company does not report in would be wrong from its first entry.
    currency = Currency.objects.filter(is_base=True).first() or Currency.objects.get(
        code="USD"
    )
    clearing = MoneyAccount.objects.update_or_create(
        code="STRIPE-01",
        defaults={
            "name": "Stripe Clearing",
            "account_type": "CARD",
            "currency": currency,
            "gl_account": Account.objects.get(code="1140"),
            "is_active": True,
        },
    )[0]
    PaymentMethod.objects.update_or_create(
        code="STRIPE",
        defaults={
            "name": "Stripe",
            # The Stripe charge or payment-intent id. Without it a receipt
            # cannot be traced back to the dashboard when it is queried.
            "requires_reference": True,
            "default_money_account": clearing,
            "is_active": True,
        },
    )


def unseed(apps, schema_editor):
    """No-op, for the reason given in 0004: posted history references this."""
    return


class Migration(migrations.Migration):
    dependencies = [
        # Renumbered from 0009 when this branch met dev: dev had already taken
        # 0009 and 0010 for the sales-return permissions, and renumbering keeps
        # core on a single line rather than leaving a merge node behind.
        ("core", "0010_grant_sales_return_permissions"),
        ("ledger", "0005_alter_accountmapping_key"),
        ("payments", "0005_payment_fee_base_payment_fee_txn_and_more"),
    ]

    operations = [migrations.RunPython(seed, unseed)]
