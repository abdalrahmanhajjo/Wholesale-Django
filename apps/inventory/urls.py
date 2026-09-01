"""Goods receipt routes (PUR-003, PUR-004, INV-006). Deliveries, transfers and adjustments follow."""

from django.urls import path

from apps.inventory import views

app_name = "inventory"

urlpatterns = [
    path("receipts/", views.GoodsReceiptListView.as_view(), name="gr_list"),
    path("receipts/new/", views.GoodsReceiptCreateView.as_view(), name="gr_create"),
    path("receipts/<int:pk>/", views.GoodsReceiptDetailView.as_view(), name="gr_detail"),
    path("receipts/<int:pk>/edit/", views.GoodsReceiptEditView.as_view(), name="gr_edit"),
    path("receipts/<int:pk>/post/", views.GoodsReceiptPostView.as_view(), name="gr_post"),
]
