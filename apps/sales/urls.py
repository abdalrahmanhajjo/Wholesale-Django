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
    # Sales returns (RET-001..RET-009)
    path("returns/", views.SalesReturnListView.as_view(), name="return_list"),
    path("returns/new/", views.SalesReturnCreateView.as_view(), name="return_create"),
    path("returns/<int:pk>/", views.SalesReturnDetailView.as_view(), name="return_detail"),
    path(
        "returns/<int:pk>/submit/", views.SalesReturnSubmitView.as_view(), name="return_submit"
    ),
    path(
        "returns/<int:pk>/approve/",
        views.SalesReturnApproveView.as_view(),
        name="return_approve",
    ),
    path(
        "returns/<int:pk>/reject/", views.SalesReturnRejectView.as_view(), name="return_reject"
    ),
    path("returns/<int:pk>/post/", views.SalesReturnPostView.as_view(), name="return_post"),
    # Sales credit notes (RET-003, RET-004, SAL-007)
    path("credit-notes/", views.CreditNoteListView.as_view(), name="credit_note_list"),
    path(
        "credit-notes/new/",
        views.CreditNoteCreateView.as_view(),
        name="credit_note_create",
    ),
    path(
        "credit-notes/<int:pk>/",
        views.CreditNoteDetailView.as_view(),
        name="credit_note_detail",
    ),
    path(
        "credit-notes/<int:pk>/submit/",
        views.CreditNoteSubmitView.as_view(),
        name="credit_note_submit",
    ),
    path(
        "credit-notes/<int:pk>/approve/",
        views.CreditNoteApproveView.as_view(),
        name="credit_note_approve",
    ),
    path(
        "credit-notes/<int:pk>/reject/",
        views.CreditNoteRejectView.as_view(),
        name="credit_note_reject",
    ),
    path(
        "credit-notes/<int:pk>/post/",
        views.CreditNotePostView.as_view(),
        name="credit_note_post",
    ),
]
