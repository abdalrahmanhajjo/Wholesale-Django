"""Payment register, entry, editing and detail screens (PAY-001, PAY-002)."""

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.shortcuts import redirect
from django.views.generic import CreateView, DetailView, UpdateView

from apps.core import audit
from apps.core.list_views import ChoiceFilter, Column, DateRangeFilter, FilteredListView
from apps.core.mixins import ActionPermissionMixin
from apps.core.models import AuditEvent, DocumentStatus
from apps.core.permissions import EXPORT_DATA
from apps.payments import services
from apps.payments.forms import PaymentForm
from apps.payments.models import Payment, PaymentDirection


def _form_data(form):
    return {name: form.cleaned_data[name] for name in form._meta.fields}


class PaymentListView(FilteredListView):
    model = Payment
    required_permission = "payments.view_payment"
    page_title = "Payments & receipts"
    page_subtitle = "Customer money received and vendor money paid."
    create_url_name = "payments:payment_create"
    create_label = "New payment / receipt"
    export_permission = EXPORT_DATA
    export_filename = "payments_and_receipts"
    default_ordering = "-payment_date"
    columns = [
        Column("number", "Number", sortable=True, link=True, css="font-mono text-xs"),
        Column("direction", "Type", sortable=True),
        Column("party", "Party"),
        Column("payment_date", "Payment date", sortable=True),
        Column("method", "Method", order_by="method__name"),
        Column("reference", "Reference", css="font-mono text-xs"),
        Column("status", "Status", badge=True, align="center"),
        Column("amount_txn", "Amount", sortable=True, money=True, align="right"),
    ]
    search_fields = [
        "number",
        "reference",
        "customer__code",
        "customer__name",
        "vendor__code",
        "vendor__name",
        "narration",
    ]
    filters = [
        ChoiceFilter("direction", "Type", list(PaymentDirection.choices)),
        ChoiceFilter("status", "Status", list(DocumentStatus.choices)),
        DateRangeFilter("payment_date", "Payment date"),
    ]

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related("customer", "vendor", "currency", "method", "money_account")
        )

    def get_summary(self):
        values = self.get_queryset().aggregate(
            receipts=Sum("amount_txn", filter=Q(direction=PaymentDirection.RECEIPT)),
            payments=Sum("amount_txn", filter=Q(direction=PaymentDirection.PAYMENT)),
            drafts=Count("id", filter=Q(status=DocumentStatus.DRAFT)),
            unapplied=Sum("unallocated_txn"),
        )
        return [
            ("Receipts", f"{values['receipts'] or 0:,.2f}"),
            ("Vendor payments", f"{values['payments'] or 0:,.2f}"),
            ("Draft records", values["drafts"] or 0),
            ("Unallocated", f"{values['unapplied'] or 0:,.2f}"),
        ]


class PaymentCreateView(ActionPermissionMixin, CreateView):
    model = Payment
    form_class = PaymentForm
    template_name = "payments/payment_form.html"
    required_permission = "payments.add_payment"
    extra_context = {
        "page_title": "New payment / receipt",
        "page_subtitle": "Record money received from a customer or paid to a vendor.",
    }

    def form_valid(self, form):
        try:
            with transaction.atomic():
                self.object = services.create_payment(
                    user=self.request.user, **_form_data(form)
                )
                audit.record_create(self.request, self.object)
        except ValidationError as exc:
            _attach_validation_error(form, exc)
            return self.form_invalid(form)
        messages.success(self.request, f"{self.object.number} saved as a draft.")
        return redirect(self.object)


class PaymentUpdateView(ActionPermissionMixin, UpdateView):
    model = Payment
    form_class = PaymentForm
    template_name = "payments/payment_form.html"
    required_permission = "payments.change_payment"

    def get_queryset(self):
        # Filtering at queryset level keeps the state check behind the
        # permission mixin and prevents direct edits of posted records.
        return super().get_queryset().filter(status=DocumentStatus.DRAFT)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(page_title=f"Edit {self.object.number}", page_subtitle="Draft only")
        return context

    def form_valid(self, form):
        before = audit.snapshot(self.object)
        try:
            with transaction.atomic():
                self.object = services.update_draft_payment(
                    self.object, user=self.request.user, **_form_data(form)
                )
                audit.record_update(self.request, self.object, before)
        except ValidationError as exc:
            _attach_validation_error(form, exc)
            return self.form_invalid(form)
        messages.success(self.request, f"{self.object.number} updated.")
        return redirect(self.object)


class PaymentDetailView(ActionPermissionMixin, DetailView):
    model = Payment
    template_name = "payments/payment_detail.html"
    context_object_name = "payment"
    required_permission = "payments.view_payment"

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related(
                "customer",
                "vendor",
                "currency",
                "method",
                "money_account",
                "fiscal_period",
                "created_by",
                "updated_by",
            )
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = self.object.number
        context["audit_events"] = AuditEvent.objects.filter(
            content_type__app_label="payments",
            content_type__model="payment",
            object_id=self.object.pk,
        ).select_related("user")[:20]
        return context


def _attach_validation_error(form, error):
    if hasattr(error, "message_dict"):
        for field, messages_ in error.message_dict.items():
            target = field if field in form.fields else None
            for message in messages_:
                form.add_error(target, message)
    else:
        for message in error.messages:
            form.add_error(None, message)
