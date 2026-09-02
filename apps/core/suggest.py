"""
The data behind type-to-search fields, and the values a chosen record implies.

Two endpoints, both read-only:

``/suggest/<kind>/?q=moh``
    Records matching what has been typed, each with a second line of context —
    a customer's currency and credit state, a product's price and stock — so the
    choice can be made from the list instead of by opening the record.

``/suggest/<kind>/<pk>/prefill/``
    What the rest of the form should adopt now that this record is chosen. A
    sales order raised for a customer already knows the currency, the payment
    term and the delivery address; asking for them again is asking the user to
    copy data the system holds.

Every kind names the permission it requires. A user who cannot view customers
cannot enumerate them here either — a search endpoint that skipped the check
would be a way around the list screens, which is why the permission lives on the
suggester rather than on the view.
"""

from dataclasses import dataclass, field
from decimal import Decimal

from django.db.models import Q

#: Never return more than this. A type-ahead that returns 500 rows is slower to
#: read than the list screen it was meant to replace.
MAX_RESULTS = 12
MIN_QUERY = 1


def _money(value):
    if value is None:
        return None
    return f"{Decimal(value):,.2f}"


@dataclass
class Suggester:
    """One searchable kind of record."""

    model: object
    permission: str
    #: Fields matched with `icontains`, in the order they are tried.
    search_fields: list
    #: Called with the object; returns the line under the label.
    detail: object = None
    #: Called with the object; returns {form_field_name: value} to fill in.
    prefill: object = None
    #: Extra `select_related` for the detail line.
    related: tuple = ()
    order_by: str = "name"
    #: Restrict to rows a document can actually use.
    active_only: bool = True
    filters: dict = field(default_factory=dict)

    def queryset(self):
        qs = self.model.objects.all()
        if self.related:
            qs = qs.select_related(*self.related)
        if self.active_only and any(f.name == "is_active" for f in self.model._meta.fields):
            qs = qs.filter(is_active=True)
        if self.filters:
            qs = qs.filter(**self.filters)
        return qs.order_by(self.order_by)

    def search(self, term):
        matches = Q()
        for name in self.search_fields:
            matches |= Q(**{f"{name}__icontains": term})
        return self.queryset().filter(matches)[:MAX_RESULTS]

    def as_option(self, obj):
        return {
            "value": str(obj.pk),
            "label": str(obj),
            "detail": self.detail(obj) if self.detail else "",
        }


# ---------------------------------------------------------------------------
# Detail lines
# ---------------------------------------------------------------------------
def _customer_detail(customer):
    bits = [customer.code, customer.currency_id or ""]
    if customer.credit_hold:
        bits.append("on credit hold")
    elif customer.credit_limit:
        bits.append(f"limit {_money(customer.credit_limit)}")
    return " · ".join(b for b in bits if b)


def _vendor_detail(vendor):
    return " · ".join(b for b in (vendor.code, vendor.currency_id or "") if b)


def _product_detail(product):
    bits = [product.sku]
    if product.sales_price is not None:
        bits.append(_money(product.sales_price))
    if product.unit_id:
        bits.append(str(product.unit))
    return " · ".join(b for b in bits if b)


def _account_detail(account):
    return " · ".join(b for b in (account.code, account.currency_id or "") if b)


def _order_detail(order):
    return " · ".join(
        b
        for b in (str(order.customer), order.get_status_display(), str(order.document_date))
        if b
    )


# ---------------------------------------------------------------------------
# What a chosen record implies for the rest of the form
# ---------------------------------------------------------------------------
def _customer_prefill(customer):
    """
    Filled in, not locked. A customer normally trades in one currency on one
    payment term, but a given order may legitimately differ, so every value
    here stays editable and the form says where it came from.
    """
    values = {
        "currency": customer.currency_id or "",
        "payment_term": str(customer.payment_term_id or ""),
        "default_tax_code": str(customer.default_tax_code_id or ""),
        "warehouse": str(customer.default_warehouse_id or ""),
        "salesperson": str(customer.salesperson_id or ""),
    }
    notices = []
    if customer.credit_hold:
        notices.append(
            {
                "level": "warning",
                "text": (
                    f"{customer.name} is on credit hold"
                    + (
                        f": {customer.credit_hold_reason}"
                        if customer.credit_hold_reason
                        else ""
                    )
                    + ". Posting will be refused until the hold is lifted."
                ),
            }
        )
    elif customer.credit_limit:
        notices.append(
            {
                "level": "info",
                "text": f"Credit limit {_money(customer.credit_limit)} {customer.currency_id or ''}".strip(),
            }
        )
    return {"values": {k: v for k, v in values.items() if v}, "notices": notices}


def _vendor_prefill(vendor):
    return {
        "values": {
            k: v
            for k, v in {
                "currency": vendor.currency_id or "",
                "payment_term": str(vendor.payment_term_id or ""),
            }.items()
            if v
        },
        "notices": [],
    }


def _money_account_prefill(account):
    return {"values": {"currency": account.currency_id or ""}, "notices": []}


def _product_prefill(product):
    values = {
        "unit": str(product.unit_id or ""),
        "tax_code": str(product.default_sales_tax_code_id or ""),
        "description": product.name,
    }
    if product.sales_price is not None:
        values["unit_price"] = f"{product.sales_price:.2f}"
    notices = []
    if product.max_discount_percent:
        notices.append(
            {
                "level": "info",
                "text": f"Discount on this product is capped at {product.max_discount_percent}%.",
            }
        )
    return {"values": {k: v for k, v in values.items() if v}, "notices": notices}


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------
def build_registry():
    """Built lazily so importing this module does not import every app."""
    from apps.catalog.models import Product
    from apps.core.models import Currency, PaymentTerm, TaxCode
    from apps.inventory.models import Warehouse
    from apps.parties.models import Customer, Vendor
    from apps.payments.models import MoneyAccount
    from apps.sales.models import SalesOrder

    return {
        "customer": Suggester(
            model=Customer,
            permission="parties.view_customer",
            search_fields=["name", "code", "legal_name", "tax_id"],
            detail=_customer_detail,
            prefill=_customer_prefill,
            related=("currency", "payment_term"),
        ),
        "vendor": Suggester(
            model=Vendor,
            permission="parties.view_vendor",
            search_fields=["name", "code", "legal_name", "tax_id"],
            detail=_vendor_detail,
            prefill=_vendor_prefill,
            related=("currency",),
        ),
        "product": Suggester(
            model=Product,
            permission="catalog.view_product",
            search_fields=["name", "sku", "barcode"],
            detail=_product_detail,
            prefill=_product_prefill,
            related=("unit",),
        ),
        "warehouse": Suggester(
            model=Warehouse,
            permission="inventory.view_warehouse",
            search_fields=["name", "code"],
        ),
        "money_account": Suggester(
            model=MoneyAccount,
            permission="payments.view_moneyaccount",
            search_fields=["name", "code", "bank_name"],
            detail=_account_detail,
            prefill=_money_account_prefill,
            related=("currency",),
        ),
        "currency": Suggester(
            model=Currency,
            permission="core.view_currency",
            search_fields=["code", "name"],
            order_by="code",
        ),
        "payment_term": Suggester(
            model=PaymentTerm,
            permission="core.view_paymentterm",
            search_fields=["code", "name"],
            order_by="code",
        ),
        "tax_code": Suggester(
            model=TaxCode,
            permission="core.view_taxcode",
            search_fields=["code", "name"],
            order_by="code",
        ),
        "sales_order": Suggester(
            model=SalesOrder,
            permission="sales.view_salesorder",
            search_fields=["number"],
            detail=_order_detail,
            related=("customer",),
            active_only=False,
            order_by="-document_date",
        ),
    }
