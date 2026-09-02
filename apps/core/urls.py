"""Configuration and settings screens (CFG-001..CFG-009)."""

from django.urls import path

from apps.core import api, views

app_name = "core"

urlpatterns = [
    path("company/", views.CompanySettingsView.as_view(), name="company_settings"),
    path("currencies/", views.CurrencyListView.as_view(), name="currency_list"),
    path("currencies/new/", views.CurrencyCreateView.as_view(), name="currency_create"),
    path(
        "currencies/<str:pk>/edit/",
        views.CurrencyUpdateView.as_view(),
        name="currency_edit",
    ),
    path("tax-codes/", views.TaxCodeListView.as_view(), name="taxcode_list"),
    path("tax-codes/new/", views.TaxCodeCreateView.as_view(), name="taxcode_create"),
    path("tax-codes/<int:pk>/edit/", views.TaxCodeUpdateView.as_view(), name="taxcode_edit"),
    path("payment-terms/", views.PaymentTermListView.as_view(), name="paymentterm_list"),
    path(
        "payment-terms/new/",
        views.PaymentTermCreateView.as_view(),
        name="paymentterm_create",
    ),
    path(
        "payment-terms/<int:pk>/edit/",
        views.PaymentTermUpdateView.as_view(),
        name="paymentterm_edit",
    ),
    path("number-series/", views.DocumentSequenceListView.as_view(), name="sequence_list"),
    path(
        "number-series/new/",
        views.DocumentSequenceCreateView.as_view(),
        name="sequence_create",
    ),
    path(
        "number-series/<int:pk>/edit/",
        views.DocumentSequenceUpdateView.as_view(),
        name="sequence_edit",
    ),
    path("fiscal-periods/", views.FiscalPeriodListView.as_view(), name="fiscalperiod_list"),
    # Read-only JSON the form layer calls while someone is typing. Each is
    # permission-checked against the same permission its list screen requires.
    path("suggest/<slug:kind>/", api.suggest, name="suggest"),
    path("suggest/<slug:kind>/<str:pk>/prefill/", api.prefill, name="suggest_prefill"),
    path("check/", api.check, name="check"),
]
