"""
Test helpers for the sales app.

Creates the minimal master data a SalesOrder needs (currency, warehouse,
customer, product, tax code, document sequence) so tests read clearly instead
of repeating object construction in every test.
"""

from datetime import date
from decimal import Decimal

from apps.catalog.models import Product, UnitOfMeasure
from apps.core.models import (
    Currency,
    DocumentSequence,
    FiscalPeriod,
    FiscalYear,
    PaymentTerm,
    TaxCode,
)
from apps.inventory.models import Warehouse
from apps.parties.models import Customer
from apps.sales.models import DiscountKind, SalesOrder, SalesOrderLine

_order_seq = [0]


def make_currency(code="USD"):
    return Currency.objects.get_or_create(
        code=code, defaults={"name": code, "symbol": "$", "is_active": True}
    )[0]


def make_warehouse(code="MAIN", **kw):
    defaults = {"name": f"{code} Warehouse"}
    defaults.update(kw)
    return Warehouse.objects.get_or_create(code=code, defaults=defaults)[0]


def make_tax(code="VAT-P11", rate=Decimal("11.0"), **kw):
    defaults = {"name": code, "rate_percent": rate}
    defaults.update(kw)
    return TaxCode.objects.get_or_create(code=code, defaults=defaults)[0]


def make_unit(code="EA"):
    obj, _ = UnitOfMeasure.objects.get_or_create(code=code, defaults={"name": code})
    return obj


def make_product(sku="P-001", price=Decimal("100"), unit=None, tax=None):
    unit = unit or make_unit()
    return Product.objects.get_or_create(
        sku=sku,
        defaults=dict(
            name=f"Product {sku}",
            unit=unit,
            sales_price=price,
            is_inventory=True,
            is_active=True,
        ),
    )[0]


def make_payment_term(code="NET30", net_days=30):
    return PaymentTerm.objects.get_or_create(
        code=code, defaults=dict(name=code, net_days=net_days)
    )[0]


def make_customer(code="C-001", currency=None, payment_term=None, **kw):
    currency = currency or make_currency()
    defaults = dict(
        name=f"Customer {code}",
        currency=currency,
        payment_term=payment_term or make_payment_term(),
        is_active=True,
    )
    defaults.update(kw)
    return Customer.objects.get_or_create(code=code, defaults=defaults)[0]


def make_sequence(document_type="SO", prefix="SO-", padding=5):
    return DocumentSequence.objects.get_or_create(
        document_type=document_type,
        defaults=dict(prefix=prefix, padding=padding),
    )[0]


def make_order(customer=None, warehouse=None, currency=None, user=None, **kw):
    """Create a DRAFT SalesOrder with a generated number and valid required fields."""
    customer = customer or make_customer()
    warehouse = warehouse or make_warehouse()
    currency = currency or customer.currency
    make_sequence()

    from django.utils import timezone

    _order_seq[0] += 1
    number = "SO-TEST-%03d" % _order_seq[0]

    defaults = dict(
        customer=customer,
        warehouse=warehouse,
        currency=currency,
        exchange_rate=Decimal("1"),
        document_date=timezone.localdate(),
        posting_date=timezone.localdate(),
        status=SalesOrder._meta.get_field("status").get_default(),
        document_discount_kind=DiscountKind.NONE,
        document_discount_value=Decimal("0"),
    )
    defaults.update(kw)

    order = SalesOrder(number=number, **defaults)
    order.save()
    return order


def make_line(
    order,
    product=None,
    qty=Decimal("1"),
    price=None,
    line_no=1,
    tax=None,
    discount=Decimal("0"),
    **kw,
):
    product = product or make_product()
    line = SalesOrderLine(
        line_no=line_no,
        order=order,
        product=product,
        unit=product.unit,
        quantity=qty,
        unit_price=price if price is not None else product.sales_price,
        discount_percent=discount,
        tax_code=tax,
        tax_rate_percent=tax.rate_percent if tax else Decimal("0"),
        tax_is_inclusive=(tax.is_inclusive if tax else False),
    )
    for k, v in kw.items():
        setattr(line, k, v)
    line.save()
    return line


def make_open_period():
    """An open fiscal period for the current date, required before posting."""
    year = FiscalYear.objects.create(
        code="FY-TEST", start_date="2026-01-01", end_date="2026-12-31"
    )
    period, _ = FiscalPeriod.objects.get_or_create(
        fiscal_year=year,
        period_no=1,
        defaults=dict(
            name="Test period",
            start_date="2026-01-01",
            end_date="2026-12-31",
            status="OPEN",
        ),
    )
    return period


def ensure_open_period_for_today():
    """Open fiscal period + JE sequence covering the current date, without
    creating a competing fiscal year (global no-overlap constraint). Reuses an
    existing open period if one already covers today."""
    from apps.core.models import DocumentSequence

    DocumentSequence.objects.get_or_create(
        document_type="JE", defaults=dict(prefix="JV-", padding=5)
    )
    today = date.today()
    if FiscalPeriod.objects.filter(
        start_date__lte=today, end_date__gte=today, status="OPEN"
    ).exists():
        return
    fiscal_year = (
        FiscalYear.objects.filter(start_date__lte=today, end_date__gte=today)
        .order_by("pk")
        .first()
    )
    if fiscal_year is None:
        fiscal_year, _ = FiscalYear.objects.get_or_create(
            code=f"FY-{today.year}",
            defaults=dict(
                start_date=date(today.year, 1, 1), end_date=date(today.year, 12, 31)
            ),
        )
    FiscalPeriod.objects.get_or_create(
        fiscal_year=fiscal_year,
        period_no=1,
        defaults=dict(
            name="OPEN",
            start_date=date(today.year, 1, 1),
            end_date=date(today.year, 12, 31),
        ),
    )


def ensure_account_mappings():
    """CFG-007 rows the delivery/invoice journals need (defensive; the seed
    migration already creates them)."""
    from apps.ledger.models import Account, AccountMapping, MappingKey

    specs = {
        MappingKey.ACCOUNTS_RECEIVABLE: (
            "1210",
            "ASSET",
            "CURRENT_ASSET",
            "DEBIT",
            True,
            "AR",
        ),
        MappingKey.SALES_REVENUE: ("4100", "INCOME", "REVENUE", "CREDIT", False, ""),
        MappingKey.OUTPUT_TAX: (
            "2310",
            "LIABILITY",
            "CURRENT_LIABILITY",
            "CREDIT",
            True,
            "OUTPUT_TAX",
        ),
        MappingKey.INVENTORY: ("1310", "ASSET", "CURRENT_ASSET", "DEBIT", True, "INVENTORY"),
        MappingKey.COGS: ("5010", "EXPENSE", "COGS", "DEBIT", False, ""),
        MappingKey.ROUNDING_GAIN: ("4820", "INCOME", "OTHER_INCOME", "CREDIT", False, ""),
        MappingKey.ROUNDING_LOSS: ("6920", "EXPENSE", "OTHER_EXPENSE", "DEBIT", False, ""),
    }
    for key, (code, atype, subtype, nb, control, ctype) in specs.items():
        account, _ = Account.objects.get_or_create(
            code=code,
            defaults=dict(
                name=f"{key} account",
                account_type=atype,
                subtype=subtype,
                normal_balance=nb,
                is_control=control,
                control_type=ctype,
                is_postable=True,
                is_active=True,
            ),
        )
        AccountMapping.objects.get_or_create(key=key, defaults={"account": account})


def make_company():
    """Singleton Company used by the posting engine when it books a journal."""
    from apps.core.models import Company

    company, _ = Company.objects.get_or_create(
        singleton=True,
        defaults=dict(
            name="Test Company",
            legal_name="Test Company",
            base_currency=make_currency(),
            allow_negative_stock=False,
        ),
    )
    return company


def seed_stock(product, warehouse, quantity, unit_cost, user, key):
    """Create on-hand stock for a product/warehouse via a GOODS_RECEIPT so a
    later delivery does not trip the BR-017 negative-stock guard."""
    from apps.inventory.services import post_stock_movement

    post_stock_movement(
        product=product,
        warehouse=warehouse,
        movement_date=date.today(),
        movement_type="GOODS_RECEIPT",
        quantity=quantity,
        unit_cost=unit_cost,
        source=product,
        source_doc_type="",
        source_doc_number="",
        idempotency_key=key,
        user=user,
    )


_user_seq = [0]


def make_user(username=None):
    """Create a real User (audit events require one)."""
    from apps.accounts.models import User

    _user_seq[0] += 1
    username = username or "user-%03d" % _user_seq[0]
    return User.objects.create_user(
        username=username,
        email=f"{username}@example.com",
        password="x-test-password",
    )
