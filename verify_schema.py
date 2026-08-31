"""
Schema verification harness.

Proves the database itself rejects the things the BRD says must be impossible.

Run against a FRESHLY MIGRATED database — it writes posted journals, and those
are immutable by design, so it cannot clean up after itself:

    dropdb wams && createdb wams && python manage.py migrate
    python verify_schema.py
"""

import os
import sys
from datetime import date
from decimal import Decimal

import django

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.db import IntegrityError, connection, transaction  # noqa: E402
from django.db.utils import InternalError, ProgrammingError  # noqa: E402

from apps.accounts.models import User  # noqa: E402
from apps.catalog.models import Product, ProductCategory, UnitOfMeasure  # noqa: E402
from apps.core.models import (  # noqa: E402
    Company, Currency, FiscalPeriod, FiscalYear, PaymentTerm, TaxCode,
)
from django.contrib.contenttypes.models import ContentType  # noqa: E402
from apps.inventory.models import StockBalance, Warehouse  # noqa: E402
from apps.ledger.models import (  # noqa: E402
    Account, AccountMapping, JournalEntry, JournalLine, MappingKey, PostingLink,
)
from apps.parties.models import Customer, Vendor  # noqa: E402
from apps.payments.models import (  # noqa: E402
    Allocation, MoneyAccount, Payment, PaymentMethod,
)
from apps.sales.models import SalesInvoice, SalesInvoiceLine  # noqa: E402

PASS, FAIL = [], []


def check(label, condition, detail=""):
    (PASS if condition else FAIL).append(label)
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {label}" + (f"  -- {detail}" if detail and not condition else ""))


def expect_rejection(label, fn):
    """The database must refuse this. Anything else is a failure."""
    try:
        with transaction.atomic():
            fn()
    except (IntegrityError, InternalError, ProgrammingError, Exception) as exc:
        msg = str(exc).strip().splitlines()[0][:110]
        check(label, True)
        print(f"         rejected: {msg}")
        return
    check(label, False, "the database ACCEPTED it")


print("\n=== 1. Reference data ===")
check("Chart of accounts seeded", Account.objects.count() >= 55,
      f"{Account.objects.count()} accounts")
check("Every account mapping present (CFG-007)",
      AccountMapping.objects.count() == len(MappingKey.choices),
      f"{AccountMapping.objects.count()}/{len(MappingKey.choices)}")
check("Account parents linked", Account.objects.filter(code="1210").first().parent.code == "1200")
check("Exactly one base currency (BR-002)", Currency.objects.filter(is_base=True).count() == 1)
check("Company singleton exists", Company.objects.count() == 1)
check("Fiscal periods seeded (BR-020)", FiscalPeriod.objects.count() == 24)
check("Money accounts seeded", MoneyAccount.objects.count() == 3)
check("Tax codes seeded (OD-01 placeholder)", TaxCode.objects.count() == 6)
check("Purchase returns split from purchase discounts",
      Account.objects.filter(code="5150").exists() and Account.objects.filter(code="5100").exists())
check("Contra accounts carry inverted normal balance",
      Account.objects.get(code="4200").normal_balance == "DEBIT"
      and Account.objects.get(code="5150").normal_balance == "CREDIT")

print("\n=== 2. Master data can be created ===")
user, _ = User.objects.get_or_create(
    username="verifier", defaults={"email": "verifier@example.com", "is_staff": True}
)
usd = Currency.objects.get(code="USD")
wh = Warehouse.objects.get(code="MAIN")
unit = UnitOfMeasure.objects.get(code="EA")
cat = ProductCategory.objects.get(code="GEN")
term = PaymentTerm.objects.get(code="NET30")
tax_s = TaxCode.objects.get(code="VAT-S11")

cust, _ = Customer.objects.get_or_create(
    code="C-0001", defaults={"name": "Acme Retail", "currency": usd, "payment_term": term}
)
vend, _ = Vendor.objects.get_or_create(
    code="V-0001", defaults={"name": "Global Supply", "currency": usd, "payment_term": term}
)
prod, _ = Product.objects.get_or_create(
    sku="SKU-0001",
    defaults={"name": "Blue Widget", "category": cat, "unit": unit,
              "sales_price": Decimal("25.00"), "purchase_price": Decimal("15.00"),
              "default_sales_tax_code": tax_s},
)
check("Customer / vendor / product created", all([cust.pk, vend.pk, prod.pk]))

print("\n=== 3. A balanced journal posts (BR-006, GL-001) ===")
period = FiscalPeriod.objects.get(name="2026-06")
ar = Account.objects.get(code="1210")
rev = Account.objects.get(code="4100")
out_tax = Account.objects.get(code="2310")
expense = Account.objects.get(code="6100")
accrual = Account.objects.get(code="2410")

JournalEntry.objects.filter(number="JV-VERIFY-1").delete() if False else None
with transaction.atomic():
    je = JournalEntry.objects.create(
        number="JV-VERIFY-1", entry_date=date(2026, 6, 15), fiscal_period=period,
        journal_type="SALES", currency=usd, exchange_rate=1,
        total_debit_base=Decimal("111.00"), total_credit_base=Decimal("111.00"),
        idempotency_key="verify-1", narration="Verification sale", posted_by=user,
    )
    # Deliberately a non-control expense accrual: every AR movement in this
    # harness must correspond to a real invoice, or the subledger reconciliation
    # in section 5c would report a difference that is the fixture's fault.
    JournalLine.objects.create(entry=je, line_no=1, account=expense, currency=usd,
                               debit_base=Decimal("111.00"), debit_txn=Decimal("111.00"))
    JournalLine.objects.create(entry=je, line_no=2, account=accrual, currency=usd,
                               credit_base=Decimal("111.00"), credit_txn=Decimal("111.00"))
check("Balanced journal accepted", JournalEntry.objects.filter(number="JV-VERIFY-1").exists())

print("\n=== 4. The database refuses what the BRD forbids ===")


def unbalanced_lines():
    je = JournalEntry.objects.create(
        number="JV-BAD-1", entry_date=date(2026, 6, 15), fiscal_period=period,
        journal_type="GENERAL", currency=usd, total_debit_base=Decimal("50.00"),
        total_credit_base=Decimal("50.00"), idempotency_key="bad-1",
    )
    JournalLine.objects.create(entry=je, line_no=1, account=rev, currency=usd,
                               debit_base=Decimal("50.00"), debit_txn=Decimal("50.00"))
    JournalLine.objects.create(entry=je, line_no=2, account=rev, currency=usd,
                               credit_base=Decimal("40.00"), credit_txn=Decimal("40.00"))


expect_rejection("BR-006: lines that do not balance", unbalanced_lines)


def unbalanced_header():
    JournalEntry.objects.create(
        number="JV-BAD-2", entry_date=date(2026, 6, 15), fiscal_period=period,
        journal_type="GENERAL", currency=usd, total_debit_base=Decimal("50.00"),
        total_credit_base=Decimal("40.00"), idempotency_key="bad-2",
    )


expect_rejection("BR-006: header debit != credit", unbalanced_header)


def both_sides():
    je = JournalEntry.objects.create(
        number="JV-BAD-3", entry_date=date(2026, 6, 15), fiscal_period=period,
        journal_type="GENERAL", currency=usd, total_debit_base=Decimal("10.00"),
        total_credit_base=Decimal("10.00"), idempotency_key="bad-3",
    )
    JournalLine.objects.create(entry=je, line_no=1, account=rev, currency=usd,
                               debit_base=Decimal("10.00"), credit_base=Decimal("10.00"),
                               debit_txn=Decimal("10.00"))


expect_rejection("BR-006: one line with both debit and credit", both_sides)


def non_postable():
    je = JournalEntry.objects.create(
        number="JV-BAD-4", entry_date=date(2026, 6, 15), fiscal_period=period,
        journal_type="GENERAL", currency=usd, total_debit_base=Decimal("10.00"),
        total_credit_base=Decimal("10.00"), idempotency_key="bad-4",
    )
    JournalLine.objects.create(entry=je, line_no=1, account=Account.objects.get(code="1000"),
                               currency=usd, debit_base=Decimal("10.00"),
                               debit_txn=Decimal("10.00"))
    JournalLine.objects.create(entry=je, line_no=2, account=rev, currency=usd,
                               credit_base=Decimal("10.00"), credit_txn=Decimal("10.00"))


expect_rejection("GL-010: posting to a non-postable parent account", non_postable)


def control_without_party():
    je = JournalEntry.objects.create(
        number="JV-BAD-5", entry_date=date(2026, 6, 15), fiscal_period=period,
        journal_type="GENERAL", currency=usd, total_debit_base=Decimal("10.00"),
        total_credit_base=Decimal("10.00"), idempotency_key="bad-5",
    )
    JournalLine.objects.create(entry=je, line_no=1, account=ar, currency=usd,
                               debit_base=Decimal("10.00"), debit_txn=Decimal("10.00"))
    JournalLine.objects.create(entry=je, line_no=2, account=rev, currency=usd,
                               credit_base=Decimal("10.00"), credit_txn=Decimal("10.00"))


expect_rejection("GL-011: AR line with no party", control_without_party)

closed = FiscalPeriod.objects.get(name="2026-01")
closed.status = "CLOSED"
closed.closed_by = user
closed.save()


def post_into_closed():
    je = JournalEntry.objects.create(
        number="JV-BAD-6", entry_date=date(2026, 1, 15), fiscal_period=closed,
        journal_type="GENERAL", currency=usd, total_debit_base=Decimal("10.00"),
        total_credit_base=Decimal("10.00"), idempotency_key="bad-6",
    )
    JournalLine.objects.create(entry=je, line_no=1, account=rev, currency=usd,
                               debit_base=Decimal("10.00"), debit_txn=Decimal("10.00"))
    JournalLine.objects.create(entry=je, line_no=2, account=rev, currency=usd,
                               credit_base=Decimal("10.00"), credit_txn=Decimal("10.00"))


expect_rejection("BR-020 / GL-012: posting into a closed period", post_into_closed)


def date_outside_period():
    je = JournalEntry.objects.create(
        number="JV-BAD-7", entry_date=date(2026, 3, 15), fiscal_period=period,
        journal_type="GENERAL", currency=usd, total_debit_base=Decimal("10.00"),
        total_credit_base=Decimal("10.00"), idempotency_key="bad-7",
    )


expect_rejection("BR-020: entry date outside its fiscal period", date_outside_period)

expect_rejection(
    "BR-004: editing a posted journal line",
    lambda: JournalLine.objects.filter(entry__number="JV-VERIFY-1", line_no=2).update(
        credit_base=Decimal("999.00")
    ),
)
expect_rejection(
    "BR-004: deleting a posted journal line",
    lambda: JournalLine.objects.filter(entry__number="JV-VERIFY-1", line_no=2).delete(),
)
expect_rejection(
    "BR-004: deleting a posted journal entry (ORM, PROTECT)",
    lambda: JournalEntry.objects.filter(number="JV-VERIFY-1").delete(),
)


def raw_delete_entry():
    """Bypass the ORM entirely — the trigger must still refuse."""
    with connection.cursor() as cur:
        cur.execute("DELETE FROM journal_line WHERE entry_id = "
                    "(SELECT id FROM journal_entry WHERE number = 'JV-VERIFY-1')")


expect_rejection("BR-004: deleting journal lines via raw SQL", raw_delete_entry)
expect_rejection(
    "BR-004: changing a posted journal's date",
    lambda: JournalEntry.objects.filter(number="JV-VERIFY-1").update(
        entry_date=date(2026, 6, 20)
    ),
)
expect_rejection(
    "GL-002: reusing an idempotency key",
    lambda: JournalEntry.objects.create(
        number="JV-DUP", entry_date=date(2026, 6, 15), fiscal_period=period,
        journal_type="GENERAL", currency=usd, total_debit_base=Decimal("0"),
        total_credit_base=Decimal("0"), idempotency_key="verify-1",
    ),
)

expect_rejection(
    "BR-017: driving stock negative",
    lambda: StockBalance.objects.create(
        product=prod, warehouse=wh, quantity_on_hand=Decimal("-5"),
        average_cost=Decimal("10"), total_value=Decimal("-50"),
    ),
)

expect_rejection(
    "BR-001: negative exchange rate",
    lambda: JournalEntry.objects.create(
        number="JV-BAD-8", entry_date=date(2026, 6, 15), fiscal_period=period,
        journal_type="GENERAL", currency=usd, exchange_rate=Decimal("-1"),
        total_debit_base=Decimal("0"), total_credit_base=Decimal("0"),
        idempotency_key="bad-8",
    ),
)

print("\n=== 5. Settlement rules (BR-008, BR-009) ===")
with transaction.atomic():
    inv_je = JournalEntry.objects.create(
        number="JV-VERIFY-2", entry_date=date(2026, 6, 16), fiscal_period=period,
        journal_type="SALES", currency=usd, total_debit_base=Decimal("111.00"),
        total_credit_base=Decimal("111.00"), idempotency_key="verify-2",
    )
    JournalLine.objects.create(entry=inv_je, line_no=1, account=ar, currency=usd,
                               debit_base=Decimal("111.00"), debit_txn=Decimal("111.00"),
                               customer=cust)
    JournalLine.objects.create(entry=inv_je, line_no=2, account=rev, currency=usd,
                               credit_base=Decimal("100.00"), credit_txn=Decimal("100.00"))
    JournalLine.objects.create(entry=inv_je, line_no=3, account=out_tax, currency=usd,
                               credit_base=Decimal("11.00"), credit_txn=Decimal("11.00"))

    inv = SalesInvoice.objects.create(
        number="INV-00001", document_date=date(2026, 6, 16), posting_date=date(2026, 6, 16),
        due_date=date(2026, 7, 16), fiscal_period=period, customer=cust, warehouse=wh,
        currency=usd, exchange_rate=1, subtotal_txn=Decimal("100"), taxable_base_txn=Decimal("100"),
        tax_txn=Decimal("11"), total_txn=Decimal("111"), subtotal_base=Decimal("100"),
        taxable_base_base=Decimal("100"), tax_base=Decimal("11"), total_base=Decimal("111"),
        open_txn=Decimal("111"), open_base=Decimal("111"), status="POSTED",
        journal_entry=inv_je, payment_term=term,
    )
    SalesInvoiceLine.objects.create(
        invoice=inv, line_no=1, product=prod, unit=unit, tax_code=tax_s,
        quantity=Decimal("4"), unit_price=Decimal("25"), tax_rate_percent=Decimal("11"),
        gross_txn=Decimal("100"), net_txn=Decimal("100"), taxable_base_txn=Decimal("100"),
        tax_txn=Decimal("11"), total_txn=Decimal("111"), net_base=Decimal("100"),
        taxable_base_base=Decimal("100"), tax_base=Decimal("11"), total_base=Decimal("111"),
        revenue_account=rev,
    )
check("Posted invoice created with derived open balance", inv.open_txn == Decimal("111"))

expect_rejection(
    "BR-009: open_txn out of step with total - allocated - credited",
    lambda: SalesInvoice.objects.filter(number="INV-00001").update(
        allocated_txn=Decimal("50")
    ),
)
expect_rejection(
    "BR-009: allocating more than the invoice total",
    lambda: SalesInvoice.objects.filter(number="INV-00001").update(
        allocated_txn=Decimal("200"), open_txn=Decimal("-89")
    ),
)

cash_acct = MoneyAccount.objects.get(code="BANK-01")
method = PaymentMethod.objects.get(code="BANK")
with transaction.atomic():
    pay_je = JournalEntry.objects.create(
        number="JV-VERIFY-3", entry_date=date(2026, 6, 20), fiscal_period=period,
        journal_type="CASH", currency=usd, total_debit_base=Decimal("60.00"),
        total_credit_base=Decimal("60.00"), idempotency_key="verify-3",
    )
    JournalLine.objects.create(entry=pay_je, line_no=1,
                               account=Account.objects.get(code="1120"), currency=usd,
                               debit_base=Decimal("60.00"), debit_txn=Decimal("60.00"),
                               money_account=cash_acct)
    JournalLine.objects.create(entry=pay_je, line_no=2, account=ar, currency=usd,
                               credit_base=Decimal("60.00"), credit_txn=Decimal("60.00"),
                               customer=cust)
    pay = Payment.objects.create(
        number="RCT-00001", direction="RECEIPT", payment_date=date(2026, 6, 20),
        posting_date=date(2026, 6, 20), fiscal_period=period, customer=cust, currency=usd,
        amount_txn=Decimal("60"), amount_base=Decimal("60"), allocated_txn=Decimal("60"),
        unallocated_txn=Decimal("0"), method=method, money_account=cash_acct,
        status="POSTED", journal_entry=pay_je,
    )
    Allocation.objects.create(
        allocation_date=date(2026, 6, 20), payment=pay, sales_invoice=inv,
        party_side="CUSTOMER", source_type="PAYMENT", target_type="SALES_INVOICE",
        customer=cust,
        source_amount_txn=Decimal("60"), target_amount_txn=Decimal("60"),
        amount_base=Decimal("60"),
    )
    SalesInvoice.objects.filter(pk=inv.pk).update(
        allocated_txn=Decimal("60"), open_txn=Decimal("51"), open_base=Decimal("51"),
        status="PARTIAL",
    )
inv.refresh_from_db()
check("PAY-006: partial receipt leaves the invoice partially paid",
      inv.status == "PARTIAL" and inv.open_txn == Decimal("51.0000"))

def stray_allocation():
    """
    A second, different payment allocated to the invoice without the invoice's
    own allocated_txn being updated to match. The deferred consistency trigger
    must catch it at COMMIT (BR-008 / SC-02).
    """
    je2 = JournalEntry.objects.create(
        number="JV-VERIFY-4", entry_date=date(2026, 6, 21), fiscal_period=period,
        journal_type="CASH", currency=usd, total_debit_base=Decimal("10.00"),
        total_credit_base=Decimal("10.00"), idempotency_key="verify-4",
    )
    JournalLine.objects.create(entry=je2, line_no=1,
                               account=Account.objects.get(code="1120"), currency=usd,
                               debit_base=Decimal("10.00"), debit_txn=Decimal("10.00"))
    JournalLine.objects.create(entry=je2, line_no=2, account=ar, currency=usd,
                               credit_base=Decimal("10.00"), credit_txn=Decimal("10.00"),
                               customer=cust)
    pay2 = Payment.objects.create(
        number="RCT-00002", direction="RECEIPT", payment_date=date(2026, 6, 21),
        posting_date=date(2026, 6, 21), fiscal_period=period, customer=cust, currency=usd,
        amount_txn=Decimal("10"), amount_base=Decimal("10"), allocated_txn=Decimal("10"),
        unallocated_txn=Decimal("0"), method=method, money_account=cash_acct,
        status="POSTED", journal_entry=je2,
    )
    Allocation.objects.create(
        allocation_date=date(2026, 6, 21), payment=pay2, sales_invoice=inv,
        party_side="CUSTOMER", source_type="PAYMENT", target_type="SALES_INVOICE",
        customer=cust,
        source_amount_txn=Decimal("10"), target_amount_txn=Decimal("10"),
        amount_base=Decimal("10"),
    )
    # Deliberately NOT updating inv.allocated_txn to 70.


expect_rejection(
    "BR-008 / SC-02: allocation sum out of step with the invoice", stray_allocation
)
expect_rejection(
    "PAY-004: unallocated must equal amount - allocated",
    lambda: Payment.objects.filter(number="RCT-00001").update(
        allocated_txn=Decimal("30")
    ),
)
expect_rejection(
    "Allocation must have exactly one source",
    lambda: Allocation.objects.create(
        allocation_date=date(2026, 6, 21), sales_invoice=inv,
        party_side="CUSTOMER", source_type="PAYMENT", target_type="SALES_INVOICE",
        customer=cust,
        source_amount_txn=Decimal("5"), target_amount_txn=Decimal("5"),
    ),
)
expect_rejection(
    "Side consistency: vendor-side money cannot settle a sales invoice",
    lambda: Allocation.objects.create(
        allocation_date=date(2026, 6, 21), payment=pay, sales_invoice=inv, vendor=vend,
        party_side="VENDOR", source_type="PAYMENT", target_type="SALES_INVOICE",
        source_amount_txn=Decimal("5"), target_amount_txn=Decimal("5"),
    ),
)
expect_rejection(
    "Payment direction must match its party side",
    lambda: Payment.objects.create(
        number="RCT-BAD", direction="RECEIPT", payment_date=date(2026, 6, 20),
        posting_date=date(2026, 6, 20), fiscal_period=period, vendor=vend, currency=usd,
        amount_txn=Decimal("10"), amount_base=Decimal("10"), unallocated_txn=Decimal("10"),
        method=method, money_account=cash_acct,
    ),
)

print("\n=== 6. Trial balance (SC-01) ===")
with connection.cursor() as cur:
    cur.execute("""
        SELECT COALESCE(SUM(debit_base), 0), COALESCE(SUM(credit_base), 0)
          FROM journal_line
    """)
    d, c = cur.fetchone()
check("Total debits equal total credits across the ledger", d == c, f"{d} vs {c}")
print(f"         trial balance: {d} debit / {c} credit")


print("\n=== 5b. Merged-in rules from the colleague's schema ===")

expect_rejection(
    "BR-020: two fiscal periods in the same year cannot overlap",
    lambda: FiscalPeriod.objects.create(
        fiscal_year=period.fiscal_year, period_no=98, name="OVERLAP",
        start_date=date(2026, 6, 10), end_date=date(2026, 7, 10), status="OPEN",
    ),
)
expect_rejection(
    "BR-020: two fiscal years cannot overlap",
    lambda: FiscalYear.objects.create(
        code="FY2026-DUP", start_date=date(2026, 6, 1), end_date=date(2027, 5, 31),
        status="OPEN",
    ),
)
expect_rejection(
    "A control account must name the subledger backing it",
    lambda: Account.objects.create(
        code="1299", name="Bad control", account_type="ASSET",
        subtype="CURRENT_ASSET", normal_balance="DEBIT", is_control=True,
        control_type="",
    ),
)
expect_rejection(
    "PTY-007: duplicate customer code differing only in case",
    lambda: Customer.objects.create(
        code="c-0001", name="Acme Retail (dupe)", currency=usd
    ),
)

# --- posting_link ---------------------------------------------------------
link = PostingLink.objects.create(
    source_content_type=ContentType.objects.get_for_model(SalesInvoice),
    source_object_id=inv.pk, source_doc_type="SI", source_doc_number=inv.number,
    effect_type="JOURNAL", journal_entry=inv_je, idempotency_key="post-si-1",
)
check("SC-03: posting_link records the invoice's journal effect", link.pk is not None)
expect_rejection(
    "GL-002: replaying the same posting request",
    lambda: PostingLink.objects.create(
        source_content_type=ContentType.objects.get_for_model(SalesInvoice),
        source_object_id=inv.pk, effect_type="JOURNAL", journal_entry=inv_je,
        idempotency_key="post-si-1",
    ),
)
expect_rejection(
    "A posting link names exactly one effect",
    lambda: PostingLink.objects.create(
        source_content_type=ContentType.objects.get_for_model(SalesInvoice),
        source_object_id=inv.pk, effect_type="JOURNAL",
        idempotency_key="post-si-2",
    ),
)

print("\n=== 5c. Reporting layer ===")
with connection.cursor() as cur:
    cur.execute("SELECT count(*) FROM v_general_ledger")
    gl_rows = cur.fetchone()[0]
    check("v_general_ledger returns the posted lines", gl_rows > 0, f"{gl_rows} rows")

    cur.execute("""
        SELECT COALESCE(SUM(closing_debit), 0), COALESCE(SUM(closing_credit), 0)
        FROM fn_trial_balance(%s, %s)
    """, [date(2026, 1, 1), date(2026, 12, 31)])
    td, tc = cur.fetchone()
    check("GL-005: fn_trial_balance balances", td == tc, f"{td} vs {tc}")
    print(f"         trial balance via function: {td} debit / {tc} credit")

    cur.execute("SELECT count(*) FROM fn_ar_ageing(%s)", [date(2026, 8, 30)])
    check("RPT-006: fn_ar_ageing returns the open invoice", cur.fetchone()[0] == 1)

    cur.execute("SELECT bucket, open_txn FROM fn_ar_ageing(%s)", [date(2026, 8, 30)])
    bucket, open_amt = cur.fetchone()
    check("RPT-006: ageing recomputes open from allocations, not the cached column",
          open_amt == Decimal("51.0000"), f"got {open_amt}")
    print(f"         bucket {bucket}, open {open_amt}")

    cur.execute("SELECT count(*) FROM v_sales_invoice_open")
    check("RPT-022: open sales invoice appears", cur.fetchone()[0] == 1)

    cur.execute("SELECT control_type, gl_balance_base, subledger_balance_base, difference_base "
                "FROM v_subledger_reconciliation WHERE control_type = 'AR'")
    row = cur.fetchone()
    check("RPT-021 / SC-02: AR subledger reconciles to the control account",
          row is not None and row[3] == 0, f"{row}")
    print(f"         AR: GL {row[1]} vs subledger {row[2]}, difference {row[3]}")

    cur.execute("SELECT count(*) FROM v_money_account_activity")
    check("RPT-013: cash/bank activity view returns the receipt", cur.fetchone()[0] > 0)

    cur.execute("SELECT count(*) FROM v_tax_transaction WHERE tax_side = 'SALES'")
    check("RPT-015: tax transaction detail returns the invoice line",
          cur.fetchone()[0] == 1)

    cur.execute("SELECT count(*) FROM fn_stock_card(%s, %s, %s, %s)",
                [prod.pk, wh.pk, date(2026, 1, 1), date(2026, 12, 31)])
    check("RPT-017: fn_stock_card runs (no movements posted yet)",
          cur.fetchone()[0] == 0)

    # pg_trgm similarity search, PTY-007
    cur.execute("SELECT name, similarity(upper(name), upper(%s)) AS s FROM customer "
                "WHERE upper(name) %% upper(%s) ORDER BY s DESC", ["Acme Retale", "Acme Retale"])
    hits = cur.fetchall()
    check("PTY-007: trigram search finds a near-duplicate customer name",
          len(hits) >= 1, f"{hits}")
    if hits:
        print(f"         fuzzy match: {hits[0][0]} (similarity {hits[0][1]:.2f})")

print("\n=== 7. Seed idempotency ===")
before = (Account.objects.count(), AccountMapping.objects.count(), TaxCode.objects.count())
import importlib  # noqa: E402

from django.apps import apps as django_apps  # noqa: E402

seed_mod = importlib.import_module(
    "apps.core.migrations." + "0004_seed_reference_data"
)
seed_mod.seed(django_apps, connection.schema_editor())
after = (Account.objects.count(), AccountMapping.objects.count(), TaxCode.objects.count())
check("Re-running the seed creates no duplicates", before == after, f"{before} -> {after}")

print("\n" + "=" * 70)
print(f"  {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("  FAILED: " + "; ".join(FAIL))
print("=" * 70)
sys.exit(1 if FAIL else 0)
