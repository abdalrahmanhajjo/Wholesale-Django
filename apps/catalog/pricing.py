"""
Which price applies (CFG-011).

A product carries default prices, and optionally a price list with dated
entries per currency and minimum quantity. This module holds the one rule
that decides between them, so sales and purchasing resolve prices the same
way rather than each inventing an answer.
"""

from apps.catalog.models import PriceKind


def price_for(product, on_date, quantity=None, kind=PriceKind.SALES, currency=None):
    """
    The price for `product` on `on_date`, or None when the price list has no
    entry and the caller should fall back to the product's default price.

    Among entries whose window contains the date, the one with the highest
    minimum quantity at or below `quantity` wins — so a break at 100 units
    beats the base entry once you order 100. Overlapping windows are refused
    by ProductPriceForm, so at most one entry matches each quantity break.
    """
    entries = product.prices.filter(kind=kind, valid_from__lte=on_date)
    entries = entries.filter(valid_to__isnull=True) | entries.filter(valid_to__gte=on_date)
    if currency is not None:
        entries = entries.filter(currency=currency)
    if quantity is not None:
        entries = entries.filter(min_quantity__lte=quantity)

    entry = entries.order_by("-min_quantity", "-valid_from").first()
    return entry.price if entry else None
