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
        path(
        "customers/<int:customer_pk>/addresses/new/",
        views.CustomerAddressCreateView.as_view(),
        name="customer_address_create",
    ),
    path("addresses/<int:pk>/edit/", views.AddressUpdateView.as_view(), name="address_edit"),
    path(
        "addresses/<int:pk>/delete/",
        views.AddressDeleteView.as_view(),
        name="address_delete",
    ),
    path(
        "customers/<int:customer_pk>/contacts/new/",
        views.CustomerContactCreateView.as_view(),
        name="customer_contact_create",
    ),
    path("contacts/<int:pk>/edit/", views.ContactUpdateView.as_view(), name="contact_edit"),
    path(
        "contacts/<int:pk>/delete/",
        views.ContactDeleteView.as_view(),
        name="contact_delete",
    ),
]
