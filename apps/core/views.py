"""
Core views.

Only the shell for now: a role-aware dashboard placeholder and the sign-in
redirect target. The real dashboard widgets (UX-001) come once the other
members' modules have data to show.
"""

from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from apps.core.models import Company, FiscalPeriod
from apps.ledger.models import Account, AccountMapping, MappingKey


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
    }
    return render(request, "core/dashboard.html", context)
