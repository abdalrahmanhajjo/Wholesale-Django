"""
Core views.

Only the shell for now: a role-aware dashboard placeholder and the sign-in
redirect target. The real dashboard widgets (UX-001) come once the other
members' modules have data to show.
"""

from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q, Sum
from django.shortcuts import render

from apps.core.models import Company, FiscalPeriod
from apps.ledger.models import Account, AccountMapping, MappingKey
from apps.parties.models import Customer, Vendor
from apps.payments.models import Payment, PaymentDirection
from apps.sales.models import SalesOrder


@login_required
def dashboard(request):
    """
    Landing page after sign-in.

    Deliberately shows configuration readiness rather than fake figures: until
    Members 2, 3 and 4 post real documents there is nothing financial to report,
    and a dashboard of zeroes reads as a broken system rather than an empty one.
    """
    company = Company.objects.select_related("base_currency").first()
    open_periods = FiscalPeriod.objects.filter(status="OPEN").order_by("start_date")
    missing_mappings = sorted(
        set(dict(MappingKey.choices))
        - set(AccountMapping.objects.values_list("key", flat=True))
    )
    payment_totals = Payment.objects.aggregate(
        receipts=Sum("amount_base", filter=Q(direction=PaymentDirection.RECEIPT)),
        payments=Sum("amount_base", filter=Q(direction=PaymentDirection.PAYMENT)),
        unallocated=Sum("unallocated_txn"),
        drafts=Count("id", filter=Q(status="DRAFT")),
    )

    context = {
        "page_title": f"Good day, {request.user.full_name or request.user.username}.",
        "page_subtitle": "Configuration is in place. Operational modules arrive with the other slices.",
        "company": company,
        "base_currency": company.base_currency_id if company else None,
        "current_period": open_periods.first(),
        "open_period_count": open_periods.count(),
        "account_count": Account.objects.filter(is_active=True).count(),
        "postable_account_count": Account.objects.filter(
            is_active=True, is_postable=True
        ).count(),
        "missing_mappings": missing_mappings,
        "role": request.user.groups.first(),
        "customer_count": Customer.objects.filter(is_active=True).count(),
        "vendor_count": Vendor.objects.filter(is_active=True).count(),
        "sales_order_count": SalesOrder.objects.count(),
        "payment_totals": payment_totals,
        "recent_payments": Payment.objects.select_related(
            "customer", "vendor", "currency", "method"
        )[:5],
    }
    return render(request, "core/dashboard.html", context)
