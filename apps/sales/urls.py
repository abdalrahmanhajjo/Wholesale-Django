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
]
