"""
Idempotent reference-data seed.

Everything here is a *starting point* pending accountant sign-off (BRD 14.4,
OD-01 / OD-02). Specifically flagged as placeholder:
  - base currency USD
  - standard tax rate 11%
  - fiscal year 2026, calendar months
  - account codes and names

Re-running this migration is safe: every row uses get_or_create / update_or_create
keyed on its natural key, so it also repairs a partially seeded database.
"""

from datetime import date
from decimal import Decimal

from django.db import migrations

# ---------------------------------------------------------------------------
# Chart of accounts
# (code, name, type, subtype, normal_balance, parent, postable, control,
#  contra, requires_party)
# ---------------------------------------------------------------------------
A, L, EQ, IN, EX = "ASSET", "LIABILITY", "EQUITY", "INCOME", "EXPENSE"
D, C = "DEBIT", "CREDIT"

ACCOUNTS = [
    # --- assets -----------------------------------------------------------
    ("1000", "ASSETS", A, "CURRENT_ASSET", D, None, False, False, False, False),
    ("1100", "Cash and Bank", A, "CURRENT_ASSET", D, "1000", False, False, False, False),
    ("1110", "Petty Cash", A, "CURRENT_ASSET", D, "1100", True, True, False, False),
    ("1120", "Main Bank Account", A, "CURRENT_ASSET", D, "1100", True, True, False, False),
    ("1130", "Card / Merchant Clearing", A, "CURRENT_ASSET", D, "1100", True, True, False, False),
    ("1200", "Receivables", A, "CURRENT_ASSET", D, "1000", False, False, False, False),
    ("1210", "Trade Receivables", A, "CURRENT_ASSET", D, "1200", True, True, False, True),
    ("1220", "Vendor Advances and Prepayments", A, "CURRENT_ASSET", D, "1200", True, True, False, True),
    ("1300", "Inventory", A, "CURRENT_ASSET", D, "1000", False, False, False, False),
    ("1310", "Inventory - Stock on Hand", A, "CURRENT_ASSET", D, "1300", True, True, False, False),
    ("1330", "Stock Transfer Clearing", A, "CURRENT_ASSET", D, "1300", True, False, False, False),
    ("1400", "Tax Assets", A, "CURRENT_ASSET", D, "1000", False, False, False, False),
    ("1410", "Input Tax Recoverable", A, "CURRENT_ASSET", D, "1400", True, True, False, False),
    ("1500", "Non-current Assets", A, "NONCURRENT_ASSET", D, None, False, False, False, False),
    ("1510", "Equipment and Fixtures", A, "NONCURRENT_ASSET", D, "1500", True, False, False, False),
    # --- liabilities ------------------------------------------------------
    ("2000", "LIABILITIES", L, "CURRENT_LIABILITY", C, None, False, False, False, False),
    ("2100", "Payables", L, "CURRENT_LIABILITY", C, "2000", False, False, False, False),
    ("2110", "Trade Payables", L, "CURRENT_LIABILITY", C, "2100", True, True, False, True),
    ("2150", "Goods Received Not Invoiced", L, "CURRENT_LIABILITY", C, "2100", True, False, False, False),
    ("2200", "Customer Advances", L, "CURRENT_LIABILITY", C, "2000", False, False, False, False),
    ("2210", "Customer Advances and Unapplied Receipts", L, "CURRENT_LIABILITY", C, "2200", True, True, False, True),
    ("2300", "Tax Liabilities", L, "CURRENT_LIABILITY", C, "2000", False, False, False, False),
    ("2310", "Output Tax Payable", L, "CURRENT_LIABILITY", C, "2300", True, True, False, False),
    ("2400", "Other Current Liabilities", L, "CURRENT_LIABILITY", C, "2000", False, False, False, False),
    ("2410", "Accrued Expenses", L, "CURRENT_LIABILITY", C, "2400", True, False, False, False),
    ("2500", "Non-current Liabilities", L, "NONCURRENT_LIABILITY", C, None, False, False, False, False),
    ("2510", "Long-term Loans", L, "NONCURRENT_LIABILITY", C, "2500", True, False, False, False),
    # --- equity -----------------------------------------------------------
    ("3000", "EQUITY", EQ, "EQUITY", C, None, False, False, False, False),
    ("3100", "Owner's Capital", EQ, "EQUITY", C, "3000", True, False, False, False),
    ("3200", "Retained Earnings", EQ, "EQUITY", C, "3000", True, False, False, False),
    ("3300", "Current Year Result", EQ, "EQUITY", C, "3000", True, False, False, False),
    ("3400", "Drawings", EQ, "EQUITY", D, "3000", True, False, True, False),
    ("3900", "Opening Balance Equity", EQ, "EQUITY", C, "3000", True, False, False, False),
    # --- income -----------------------------------------------------------
    ("4000", "INCOME", IN, "REVENUE", C, None, False, False, False, False),
    ("4100", "Sales Revenue", IN, "REVENUE", C, "4000", True, False, False, False),
    # Contra-revenue: debit-natured (BRD Appendix A "Sales Returns/Revenue").
    ("4200", "Sales Returns and Allowances", IN, "REVENUE", D, "4000", True, False, True, False),
    ("4300", "Sales Discounts Granted", IN, "REVENUE", D, "4000", True, False, True, False),
    ("4800", "Other Income", IN, "OTHER_INCOME", C, None, False, False, False, False),
    ("4810", "Realised FX Gain", IN, "OTHER_INCOME", C, "4800", True, False, False, False),
    ("4820", "Rounding Income", IN, "OTHER_INCOME", C, "4800", True, False, False, False),
    ("4830", "Inventory Adjustment Gain", IN, "OTHER_INCOME", C, "4800", True, False, False, False),
    # --- cost of sales ----------------------------------------------------
    ("5000", "COST OF SALES", EX, "COGS", D, None, False, False, False, False),
    ("5010", "Cost of Goods Sold", EX, "COGS", D, "5000", True, False, False, False),
    ("5020", "Non-stock Purchases / Direct Expense", EX, "COGS", D, "5000", True, False, False, False),
    # Contra-expense: credit-natured. Kept as two accounts on purpose — a return
    # of goods is not the same event as a discount on price, and the tax report
    # treats them differently.
    ("5100", "Purchase Discounts Received", EX, "COGS", C, "5000", True, False, True, False),
    ("5150", "Purchase Returns and Allowances", EX, "COGS", C, "5000", True, False, True, False),
    # --- operating expenses ----------------------------------------------
    ("6000", "OPERATING EXPENSES", EX, "OPERATING_EXPENSE", D, None, False, False, False, False),
    ("6100", "Salaries and Wages", EX, "OPERATING_EXPENSE", D, "6000", True, False, False, False),
    ("6200", "Rent", EX, "OPERATING_EXPENSE", D, "6000", True, False, False, False),
    ("6300", "Utilities", EX, "OPERATING_EXPENSE", D, "6000", True, False, False, False),
    ("6400", "Freight and Delivery", EX, "OPERATING_EXPENSE", D, "6000", True, False, False, False),
    ("6500", "Bank Charges", EX, "OPERATING_EXPENSE", D, "6000", True, False, False, False),
    ("6600", "Office and Administration", EX, "OPERATING_EXPENSE", D, "6000", True, False, False, False),
    ("6700", "Professional Fees", EX, "OPERATING_EXPENSE", D, "6000", True, False, False, False),
    ("6900", "Other Expenses", EX, "OTHER_EXPENSE", D, None, False, False, False, False),
    ("6910", "Realised FX Loss", EX, "OTHER_EXPENSE", D, "6900", True, False, False, False),
    ("6920", "Rounding Expense", EX, "OTHER_EXPENSE", D, "6900", True, False, False, False),
    ("6930", "Inventory Adjustment Loss / Write-off", EX, "OTHER_EXPENSE", D, "6900", True, False, False, False),
    ("6940", "Non-recoverable Tax", EX, "OTHER_EXPENSE", D, "6900", True, False, False, False),
]

# Which subledger backs each control account (GL-011 / RPT-021). Drives
# v_control_account_balance and v_subledger_reconciliation.
CONTROL_TYPES = {
    "1110": "CASH_BANK",
    "1120": "CASH_BANK",
    "1130": "CASH_BANK",
    "1210": "AR",
    "1220": "VENDOR_ADVANCE",
    "1310": "INVENTORY",
    "1410": "INPUT_TAX",
    "2110": "AP",
    "2210": "CUSTOMER_ADVANCE",
    "2310": "OUTPUT_TAX",
}

MAPPINGS = {
    "ACCOUNTS_RECEIVABLE": "1210",
    "ACCOUNTS_PAYABLE": "2110",
    "CUSTOMER_ADVANCE": "2210",
    "VENDOR_ADVANCE": "1220",
    "INVENTORY": "1310",
    "GOODS_IN_TRANSIT": "2150",
    "COGS": "5010",
    "SALES_REVENUE": "4100",
    "SALES_RETURNS": "4200",
    "SALES_DISCOUNT": "4300",
    "PURCHASE_EXPENSE": "5020",
    "PURCHASE_RETURNS": "5150",
    "PURCHASE_DISCOUNT": "5100",
    "OUTPUT_TAX": "2310",
    "INPUT_TAX": "1410",
    "TAX_NON_RECOVERABLE": "6940",
    "FX_GAIN": "4810",
    "FX_LOSS": "6910",
    "ROUNDING_GAIN": "4820",
    "ROUNDING_LOSS": "6920",
    "INVENTORY_GAIN": "4830",
    "INVENTORY_LOSS": "6930",
    "RETAINED_EARNINGS": "3200",
    "CURRENT_YEAR_RESULT": "3300",
    "OPENING_BALANCE_EQUITY": "3900",
    "STOCK_TRANSFER_CLEARING": "1330",
}

# (code, name, rate, inclusive, recoverable, treatment, applies_to)
TAX_CODES = [
    ("VAT-S11", "Standard rated 11% (sales)", "11.0000", False, True, "STANDARD", "SALES"),
    ("VAT-P11", "Standard rated 11% (purchases)", "11.0000", False, True, "STANDARD", "PURCHASE"),
    ("VAT-S11-INC", "Standard rated 11% inclusive (sales)", "11.0000", True, True, "STANDARD", "SALES"),
    ("VAT-Z", "Zero rated", "0.0000", False, False, "ZERO_RATED", "BOTH"),
    ("VAT-E", "Exempt", "0.0000", False, False, "EXEMPT", "BOTH"),
    ("NO-TAX", "Out of scope / no tax", "0.0000", False, False, "NO_TAX", "BOTH"),
]

# (doc_type, prefix, padding)
SEQUENCES = [
    ("SO", "SO-", 5), ("DN", "DN-", 5), ("SI", "INV-", 5), ("SR", "SRT-", 5),
    ("CN", "CN-", 5), ("PO", "PO-", 5), ("GR", "GRN-", 5), ("PB", "BILL-", 5),
    ("PR", "PRT-", 5), ("DBN", "DN-V-", 5), ("RC", "RCT-", 5), ("PV", "PAY-", 5),
    ("RF", "RFD-", 5), ("JE", "JV-", 5), ("ST", "TRF-", 5), ("SA", "ADJ-", 5),
]

UNITS = [
    ("EA", "Each", 0), ("BOX", "Box", 0), ("CTN", "Carton", 0),
    ("KG", "Kilogram", 3), ("L", "Litre", 3), ("M", "Metre", 2),
]

METHODS = [
    ("CASH", "Cash", False), ("BANK", "Bank transfer", True),
    ("CARD", "Card", True), ("CHEQUE", "Cheque", True), ("OTHER", "Other", False),
]

ADJUSTMENT_REASONS = [
    ("COUNT-UP", "Stock count surplus", True, "4830"),
    ("COUNT-DN", "Stock count shortage", False, "6930"),
    ("DAMAGE", "Damaged / write-off", False, "6930"),
    ("EXPIRY", "Expired goods", False, "6930"),
    ("OPENING", "Opening stock load", True, "3900"),
]

# (group name, description, post, approve, reverse, close, configure)
ROLES = [
    ("Owner/Admin", "Full business access; approves, posts, reverses, closes periods.", True, True, True, True, True),
    ("Accountant", "Owns the ledger: posts, allocates, journals, reconciles, closes.", True, True, True, True, True),
    ("Sales", "Creates and submits sales documents for assigned customers.", False, False, False, False, False),
    ("Purchasing", "Creates and submits purchase documents.", False, False, False, False, False),
    ("Warehouse", "Confirms physical stock movements.", False, False, False, False, False),
    ("Cashier", "Records and allocates money; prints receipts.", True, False, False, False, False),
    ("Auditor", "Read-only across all approved records; may export.", False, False, False, False, False),
]


def seed(apps, schema_editor):
    Currency = apps.get_model("core", "Currency")
    TaxCode = apps.get_model("core", "TaxCode")
    PaymentTerm = apps.get_model("core", "PaymentTerm")
    Company = apps.get_model("core", "Company")
    FiscalYear = apps.get_model("core", "FiscalYear")
    FiscalPeriod = apps.get_model("core", "FiscalPeriod")
    DocumentSequence = apps.get_model("core", "DocumentSequence")
    Account = apps.get_model("ledger", "Account")
    AccountMapping = apps.get_model("ledger", "AccountMapping")
    UnitOfMeasure = apps.get_model("catalog", "UnitOfMeasure")
    ProductCategory = apps.get_model("catalog", "ProductCategory")
    Warehouse = apps.get_model("inventory", "Warehouse")
    AdjustmentReason = apps.get_model("inventory", "AdjustmentReason")
    MoneyAccount = apps.get_model("payments", "MoneyAccount")
    PaymentMethod = apps.get_model("payments", "PaymentMethod")
    Group = apps.get_model("auth", "Group")
    RoleProfile = apps.get_model("accounts", "RoleProfile")

    # -- currencies (OD-02 placeholder) ------------------------------------
    for code, name, symbol, dp, is_base in [
        ("USD", "US Dollar", "$", 2, True),
        ("EUR", "Euro", "€", 2, False),
        ("GBP", "Pound Sterling", "£", 2, False),
        ("LBP", "Lebanese Pound", "L£", 0, False),
    ]:
        Currency.objects.update_or_create(
            code=code,
            defaults={"name": name, "symbol": symbol, "decimal_places": dp,
                      "is_base": is_base, "is_active": True},
        )
    usd = Currency.objects.get(code="USD")

    # -- chart of accounts --------------------------------------------------
    for code, name, atype, subtype, nb, parent, postable, control, contra, party in ACCOUNTS:
        Account.objects.update_or_create(
            code=code,
            defaults={
                "name": name, "account_type": atype, "subtype": subtype,
                "normal_balance": nb, "is_postable": postable, "is_control": control,
                "control_type": CONTROL_TYPES.get(code, ""),
                "is_contra": contra, "requires_party": party, "is_active": True,
            },
        )
    # Second pass so parents always exist before they are referenced.
    for code, *_rest in ACCOUNTS:
        parent_code = _rest[4]
        if parent_code:
            Account.objects.filter(code=code).update(
                parent=Account.objects.get(code=parent_code)
            )

    for key, code in MAPPINGS.items():
        AccountMapping.objects.update_or_create(
            key=key, defaults={"account": Account.objects.get(code=code)}
        )

    # -- tax codes (OD-01 placeholder) -------------------------------------
    output_tax = Account.objects.get(code="2310")
    input_tax = Account.objects.get(code="1410")
    for code, name, rate, inclusive, recoverable, treatment, applies in TAX_CODES:
        TaxCode.objects.update_or_create(
            code=code,
            defaults={
                "name": name, "rate_percent": Decimal(rate), "is_inclusive": inclusive,
                "is_recoverable": recoverable, "treatment": treatment,
                "applies_to": applies, "is_active": True,
                "output_tax_account": output_tax if applies in ("SALES", "BOTH") else None,
                "input_tax_account": input_tax if applies in ("PURCHASE", "BOTH") else None,
            },
        )

    # -- payment terms ------------------------------------------------------
    for code, name, days in [
        ("COD", "Cash on delivery", 0), ("NET15", "Net 15 days", 15),
        ("NET30", "Net 30 days", 30), ("NET60", "Net 60 days", 60),
        ("NET90", "Net 90 days", 90),
    ]:
        PaymentTerm.objects.update_or_create(
            code=code, defaults={"name": name, "net_days": days, "is_active": True}
        )

    # -- company ------------------------------------------------------------
    Company.objects.update_or_create(
        singleton=True,
        defaults={
            "name": "Wholesale Company",
            "legal_name": "Wholesale Company",
            "base_currency": usd,
            "fiscal_year_start_month": 1,
            "timezone": "Asia/Beirut",
            "allow_negative_stock": False,
            "rounding_tolerance": Decimal("0.05"),
        },
    )

    # -- fiscal calendar ----------------------------------------------------
    for year in (2026, 2027):
        fy, _ = FiscalYear.objects.update_or_create(
            code=f"FY{year}",
            defaults={"start_date": date(year, 1, 1), "end_date": date(year, 12, 31),
                      "status": "OPEN"},
        )
        for m in range(1, 13):
            end_day = 31 if m in (1, 3, 5, 7, 8, 10, 12) else (
                30 if m != 2 else (29 if year % 4 == 0 and year % 100 != 0 else 28)
            )
            FiscalPeriod.objects.update_or_create(
                fiscal_year=fy, period_no=m,
                defaults={
                    "name": f"{year}-{m:02d}",
                    "start_date": date(year, m, 1),
                    "end_date": date(year, m, end_day),
                    "status": "OPEN",
                },
            )

    # -- numbering ----------------------------------------------------------
    for doc_type, prefix, padding in SEQUENCES:
        DocumentSequence.objects.update_or_create(
            document_type=doc_type, series="DEFAULT",
            defaults={"prefix": prefix, "padding": padding, "next_number": 1,
                      "reset_policy": "YEARLY", "is_active": True},
        )

    # -- catalogue ----------------------------------------------------------
    for code, name, dp in UNITS:
        UnitOfMeasure.objects.update_or_create(
            code=code, defaults={"name": name, "decimal_places": dp, "is_active": True}
        )
    ProductCategory.objects.update_or_create(
        code="GEN", defaults={"name": "General", "is_active": True}
    )

    # -- inventory ----------------------------------------------------------
    Warehouse.objects.update_or_create(
        code="MAIN",
        defaults={"name": "Main Warehouse", "allow_negative_stock": False, "is_active": True},
    )
    for code, name, increases, acc in ADJUSTMENT_REASONS:
        AdjustmentReason.objects.update_or_create(
            code=code,
            defaults={"name": name, "increases_stock": increases,
                      "gain_loss_account": Account.objects.get(code=acc),
                      "requires_approval": True, "is_active": True},
        )

    # -- money --------------------------------------------------------------
    cash = MoneyAccount.objects.update_or_create(
        code="CASH-01",
        defaults={"name": "Petty Cash", "account_type": "CASH", "currency": usd,
                  "gl_account": Account.objects.get(code="1110"), "is_active": True},
    )[0]
    bank = MoneyAccount.objects.update_or_create(
        code="BANK-01",
        defaults={"name": "Main Bank Account", "account_type": "BANK", "currency": usd,
                  "gl_account": Account.objects.get(code="1120"), "is_active": True},
    )[0]
    MoneyAccount.objects.update_or_create(
        code="CARD-01",
        defaults={"name": "Card Clearing", "account_type": "CARD", "currency": usd,
                  "gl_account": Account.objects.get(code="1130"), "is_active": True},
    )
    for code, name, needs_ref in METHODS:
        PaymentMethod.objects.update_or_create(
            code=code,
            defaults={"name": name, "requires_reference": needs_ref, "is_active": True,
                      "default_money_account": cash if code == "CASH" else bank},
        )

    # -- roles (BRD 4.1) ----------------------------------------------------
    for name, desc, post, approve, reverse, close, configure in ROLES:
        group, _ = Group.objects.get_or_create(name=name)
        RoleProfile.objects.update_or_create(
            group=group,
            defaults={"description": desc, "is_system": True, "can_post": post,
                      "can_approve": approve, "can_reverse": reverse,
                      "can_close_period": close, "can_configure": configure},
        )


def unseed(apps, schema_editor):
    """
    Deliberately a no-op. Reference data is referenced by posted transactions
    (NFR-017); tearing it out on a rollback would orphan history. Drop the
    database instead if a clean slate is wanted.
    """
    return


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0003_initial"),
        ("ledger", "0003_posting_guards"),
        ("reports", "0001_reporting_layer"),
        ("catalog", "0002_initial"),
        ("inventory", "0002_initial"),
        ("payments", "0001_initial"),
        ("accounts", "0002_initial"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [migrations.RunPython(seed, unseed)]
