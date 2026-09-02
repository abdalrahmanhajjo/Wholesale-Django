"""Purchase order routes (PUR-001, PUR-002). Purchase bills and returns follow."""

from django.urls import path

from apps.purchases import views

app_name = "purchases"

urlpatterns = [
    path("orders/", views.PurchaseOrderListView.as_view(), name="po_list"),
    path("orders/new/", views.PurchaseOrderCreateView.as_view(), name="po_create"),
    path("orders/<int:pk>/", views.PurchaseOrderDetailView.as_view(), name="po_detail"),
    path("orders/<int:pk>/edit/", views.PurchaseOrderEditView.as_view(), name="po_edit"),
    path("orders/<int:pk>/submit/", views.PurchaseOrderSubmitView.as_view(), name="po_submit"),
    path(
        "orders/<int:pk>/approve/", views.PurchaseOrderApproveView.as_view(), name="po_approve"
    ),
    path("orders/<int:pk>/reject/", views.PurchaseOrderRejectView.as_view(), name="po_reject"),
]
