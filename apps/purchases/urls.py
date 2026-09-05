"""
Purchase order, purchase bill, purchase return and vendor debit note routes
(PUR-001, PUR-002, PUR-005..PUR-008, RET-005..RET-008).
"""

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
    path("bills/", views.PurchaseBillListView.as_view(), name="bill_list"),
    path("bills/new/", views.PurchaseBillCreateView.as_view(), name="bill_create"),
    path("bills/<int:pk>/", views.PurchaseBillDetailView.as_view(), name="bill_detail"),
    path("bills/<int:pk>/edit/", views.PurchaseBillEditView.as_view(), name="bill_edit"),
    path("bills/<int:pk>/post/", views.PurchaseBillPostView.as_view(), name="bill_post"),
    path("returns/", views.PurchaseReturnListView.as_view(), name="pr_list"),
    path("returns/new/", views.PurchaseReturnCreateView.as_view(), name="pr_create"),
    path("returns/<int:pk>/", views.PurchaseReturnDetailView.as_view(), name="pr_detail"),
    path("returns/<int:pk>/edit/", views.PurchaseReturnEditView.as_view(), name="pr_edit"),
    path("returns/<int:pk>/post/", views.PurchaseReturnPostView.as_view(), name="pr_post"),
    path("debit-notes/", views.VendorDebitNoteListView.as_view(), name="dbn_list"),
    path("debit-notes/new/", views.VendorDebitNoteCreateView.as_view(), name="dbn_create"),
    path(
        "debit-notes/<int:pk>/", views.VendorDebitNoteDetailView.as_view(), name="dbn_detail"
    ),
    path(
        "debit-notes/<int:pk>/edit/",
        views.VendorDebitNoteEditView.as_view(),
        name="dbn_edit",
    ),
    path(
        "debit-notes/<int:pk>/post/",
        views.VendorDebitNotePostView.as_view(),
        name="dbn_post",
    ),
]
