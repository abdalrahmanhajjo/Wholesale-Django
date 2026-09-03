from django.urls import path

from apps.payments import views

app_name = "payments"

urlpatterns = [
    path("", views.PaymentListView.as_view(), name="payment_list"),
    path("new/", views.PaymentCreateView.as_view(), name="payment_create"),
    path("<int:pk>/post/", views.PaymentPostView.as_view(), name="payment_post"),
    path(
        "<int:pk>/allocate/",
        views.PaymentAllocationView.as_view(),
        name="payment_allocate",
    ),
    path("<int:pk>/reverse/", views.PaymentReverseView.as_view(), name="payment_reverse"),
    path(
        "<int:pk>/allocations/<uuid:batch_key>/reverse/",
        views.AllocationReverseView.as_view(),
        name="allocation_reverse",
    ),
    path("<int:pk>/voucher/", views.PaymentVoucherView.as_view(), name="payment_voucher"),
    path("<int:pk>/", views.PaymentDetailView.as_view(), name="payment_detail"),
    path("<int:pk>/edit/", views.PaymentUpdateView.as_view(), name="payment_edit"),
]
