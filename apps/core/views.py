"""
Core views.

Role-aware dashboard backed entirely by Django ORM aggregates and templates.
"""

from django.conf import settings
from django.contrib.auth.decorators import login_required, permission_required
from django.core.cache import cache
from django.db.models import Count, Q, Sum
from django.shortcuts import render

from apps.core.models import Company, FiscalPeriod
from apps.ledger.models import Account, AccountMapping, MappingKey
from apps.parties.models import Customer, Vendor
from apps.payments.models import Payment, PaymentDirection
from apps.sales.models import SalesOrder


@login_required
@permission_required("core.view_company", raise_exception=True)
def dashboard(request):
    """
    Landing page after sign-in.

    The aggregate snapshot and recent activity share one short cache lifetime,
    which removes repeated round trips to a remote PostgreSQL region while
    keeping the operational overview acceptably fresh.
    """
    overview = cache.get("dashboard_overview_v1")
    if overview is None:
        company = Company.objects.select_related("base_currency").first()
        open_periods = list(FiscalPeriod.objects.filter(status="OPEN").order_by("start_date"))
        account_totals = Account.objects.filter(is_active=True).aggregate(
            total=Count("id"),
            postable=Count("id", filter=Q(is_postable=True)),
        )
        overview = {
            "company": company,
            "base_currency": company.base_currency_id if company else None,
            "current_period": open_periods[0] if open_periods else None,
            "open_period_count": len(open_periods),
            "account_count": account_totals["total"],
            "postable_account_count": account_totals["postable"],
            "missing_mappings": sorted(
                set(dict(MappingKey.choices))
                - set(AccountMapping.objects.values_list("key", flat=True))
            ),
            "customer_count": Customer.objects.filter(is_active=True).count(),
            "vendor_count": Vendor.objects.filter(is_active=True).count(),
            "sales_order_count": SalesOrder.objects.count(),
            "payment_totals": Payment.objects.aggregate(
                receipts=Sum("amount_base", filter=Q(direction=PaymentDirection.RECEIPT)),
                payments=Sum("amount_base", filter=Q(direction=PaymentDirection.PAYMENT)),
                unallocated=Sum("unallocated_txn"),
                drafts=Count("id", filter=Q(status="DRAFT")),
            ),
            "recent_payments": list(
                Payment.objects.select_related("customer", "vendor", "currency", "method")[:5]
            ),
        }
        cache.set("dashboard_overview_v1", overview, settings.DASHBOARD_CACHE_SECONDS)

    context = {
        "page_title": f"Good day, {request.user.full_name or request.user.username}.",
        "page_subtitle": "Configuration is in place. Operational modules arrive with the other slices.",
        **overview,
    }
    return render(request, "core/dashboard.html", context)
