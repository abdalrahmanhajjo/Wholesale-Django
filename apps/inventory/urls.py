"""
Goods receipt routes (PUR-003, PUR-004, INV-006), the stock ledger and
inventory valuation report screens (INV-004, INV-005, RPT-016..RPT-018), and
stock transfers / adjustments (INV-008, INV-009). Deliveries follow.
"""

from django.urls import path

from apps.inventory import views

app_name = "inventory"

urlpatterns = [
    path("receipts/", views.GoodsReceiptListView.as_view(), name="gr_list"),
    path("receipts/new/", views.GoodsReceiptCreateView.as_view(), name="gr_create"),
    path("receipts/<int:pk>/", views.GoodsReceiptDetailView.as_view(), name="gr_detail"),
    path("receipts/<int:pk>/edit/", views.GoodsReceiptEditView.as_view(), name="gr_edit"),
    path("receipts/<int:pk>/post/", views.GoodsReceiptPostView.as_view(), name="gr_post"),
    path("stock/ledger/", views.StockLedgerListView.as_view(), name="stock_ledger"),
    path("stock/valuation/", views.StockValuationListView.as_view(), name="stock_valuation"),
    path("stock/low/", views.LowStockListView.as_view(), name="low_stock"),
    path("transfers/", views.StockTransferListView.as_view(), name="st_list"),
    path("transfers/new/", views.StockTransferCreateView.as_view(), name="st_create"),
    path("transfers/<int:pk>/", views.StockTransferDetailView.as_view(), name="st_detail"),
    path("transfers/<int:pk>/edit/", views.StockTransferEditView.as_view(), name="st_edit"),
    path("transfers/<int:pk>/post/", views.StockTransferPostView.as_view(), name="st_post"),
    path("adjustments/", views.StockAdjustmentListView.as_view(), name="sa_list"),
    path("adjustments/new/", views.StockAdjustmentCreateView.as_view(), name="sa_create"),
    path("adjustments/<int:pk>/", views.StockAdjustmentDetailView.as_view(), name="sa_detail"),
    path(
        "adjustments/<int:pk>/edit/", views.StockAdjustmentEditView.as_view(), name="sa_edit"
    ),
    path(
        "adjustments/<int:pk>/submit/",
        views.StockAdjustmentSubmitView.as_view(),
        name="sa_submit",
    ),
    path(
        "adjustments/<int:pk>/approve/",
        views.StockAdjustmentApproveView.as_view(),
        name="sa_approve",
    ),
    path(
        "adjustments/<int:pk>/reject/",
        views.StockAdjustmentRejectView.as_view(),
        name="sa_reject",
    ),
    path(
        "adjustments/<int:pk>/post/", views.StockAdjustmentPostView.as_view(), name="sa_post"
    ),
]
