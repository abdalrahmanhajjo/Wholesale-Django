"""Financial statement routes (RPT-001..RPT-005)."""

from django.urls import path

from apps.reports import views

app_name = "reports"

urlpatterns = [
    path("general-ledger/", views.GeneralLedgerView.as_view(), name="general_ledger"),
    path("trial-balance/", views.TrialBalanceView.as_view(), name="trial_balance"),
    path("profit-and-loss/", views.ProfitAndLossView.as_view(), name="profit_and_loss"),
    path("balance-sheet/", views.BalanceSheetView.as_view(), name="balance_sheet"),
]
