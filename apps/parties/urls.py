"""Customer and vendor routes."""

from django.urls import path

from apps.parties import views

app_name = "parties"

urlpatterns = [
    path("customers/", views.CustomerListView.as_view(), name="customer_list"),
    path("customers/new/", views.CustomerCreateView.as_view(), name="customer_create"),
    path("customers/<int:pk>/", views.CustomerDetailView.as_view(), name="customer_detail"),
    path("customers/<int:pk>/edit/", views.CustomerUpdateView.as_view(), name="customer_edit"),
    path(
        "customers/<int:pk>/deactivate/",
        views.CustomerDeactivateView.as_view(),
        name="customer_deactivate",
    ),
    path("vendors/", views.VendorListView.as_view(), name="vendor_list"),
    path("vendors/new/", views.VendorCreateView.as_view(), name="vendor_create"),
    path("vendors/<int:pk>/", views.VendorDetailView.as_view(), name="vendor_detail"),
    path("vendors/<int:pk>/edit/", views.VendorUpdateView.as_view(), name="vendor_edit"),
]
