"""Sales-order routes."""

from django.urls import path

from apps.sales import views

app_name = "sales"

urlpatterns = [
    # Sales orders
    path("orders/", views.SalesOrderListView.as_view(), name="so_list"),
    path("orders/new/", views.SalesOrderCreateView.as_view(), name="so_create"),
    path("orders/<int:pk>/", views.SalesOrderDetailView.as_view(), name="so_detail"),
    path("orders/<int:pk>/edit/", views.SalesOrderUpdateView.as_view(), name="so_edit"),
    path("orders/<int:pk>/submit/", views.SalesOrderSubmitView.as_view(), name="so_submit"),
    path(
        "orders/<int:pk>/approve/",
        views.SalesOrderApproveView.as_view(),
        name="so_approve",
    ),
    path(
        "orders/<int:pk>/reject/",
        views.SalesOrderRejectView.as_view(),
        name="so_reject",
    ),
    # Delivery notes (SAL-005, INV-007)
    path("deliveries/", views.DeliveryNoteListView.as_view(), name="delivery_list"),
    path("deliveries/new/", views.DeliveryNoteCreateView.as_view(), name="delivery_create"),
    path(
        "deliveries/<int:pk>/", views.DeliveryNoteDetailView.as_view(), name="delivery_detail"
    ),
    path(
        "deliveries/<int:pk>/post/", views.DeliveryNotePostView.as_view(), name="delivery_post"
    ),
    # Sales invoices (SAL-006..SAL-011)
    path("invoices/", views.SalesInvoiceListView.as_view(), name="invoice_list"),
    path("invoices/new/", views.SalesInvoiceCreateView.as_view(), name="invoice_create"),
    path("invoices/<int:pk>/", views.SalesInvoiceDetailView.as_view(), name="invoice_detail"),
    path(
        "invoices/<int:pk>/submit/",
        views.SalesInvoiceSubmitView.as_view(),
        name="invoice_submit",
    ),
    path("invoices/<int:pk>/post/", views.SalesInvoicePostView.as_view(), name="invoice_post"),
    path(
        "invoices/<int:pk>/print/", views.SalesInvoicePrintView.as_view(), name="invoice_print"
    ),
]
