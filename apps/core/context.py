"""
Context available to every template.

`base_currency` was previously supplied by the dashboard view alone, so the
sidebar and footer — which render on every page — fell back to the literal
"USD" everywhere else. On a company whose base currency is not USD, every screen
but one stated the wrong currency.

It is cached because it changes roughly never and is read on every request.
"""

from django.core.cache import cache

CACHE_KEY = "company_identity_v1"
CACHE_SECONDS = 300


def company(request):
    identity = cache.get(CACHE_KEY)
    if identity is None:
        from apps.core.models import Company

        row = Company.objects.only("name", "base_currency").first()
        identity = {
            "company_name": row.name if row else "Atlas Wholesale",
            "base_currency": (row.base_currency_id if row else "") or "",
        }
        cache.set(CACHE_KEY, identity, CACHE_SECONDS)
    return identity
