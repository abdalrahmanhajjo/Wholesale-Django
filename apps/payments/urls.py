from django.urls import path

from apps.payments import views

app_name = "payments"

urlpatterns = [
    # PAY-013. The webhook is the only route in this project that is reachable
    # without a session, so it is kept at the top where it cannot be mistaken
    # for one of the staff screens below it. Its authentication is the Stripe
    # signature, checked in apps/payments/stripe_gateway.py.
    path("stripe/webhook/", views.StripeWebhookView.as_view(), name="stripe_webhook"),
    path(
        "stripe/invoice/<int:pk>/charge/",
        views.InvoiceStripeChargeView.as_view(),
        name="stripe_charge",
    ),
    path(
        "stripe/<int:pk>/settle/",
        views.StripeSettleRetryView.as_view(),
        name="stripe_settle",
    ),
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
