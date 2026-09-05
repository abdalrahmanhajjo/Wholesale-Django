"""Payment register, posting, allocation, reversal, voucher, and detail screens."""

import logging
from decimal import Decimal

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import CreateView, DetailView, UpdateView

from apps.core import audit
from apps.core.list_views import ChoiceFilter, Column, DateRangeFilter, FilteredListView
from apps.core.mixins import ActionPermissionMixin, BackLinkMixin, PostingPermissionMixin
from apps.core.models import AuditAction, AuditEvent, Company, DocumentStatus
from apps.core.permissions import (
    ALLOCATE_PAYMENT,
    EXPORT_DATA,
    POST_PAYMENT,
    REVERSE_DOCUMENT,
)
from apps.ledger.services.exceptions import PostingError
from apps.payments import allocation, reversal, services, stripe_gateway, stripe_service
from apps.payments.forms import (
    PaymentAllocationHeaderForm,
    PaymentAllocationLineFormSet,
    PaymentForm,
    ReversalForm,
)
from apps.payments.models import Allocation, Payment, PaymentDirection, StripeCheckout
from apps.purchases.models import PurchaseBill
from apps.sales.models import SalesInvoice

logger = logging.getLogger(__name__)


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


class PaymentCreateView(BackLinkMixin, ActionPermissionMixin, CreateView):
    back_url_name = "payments:payment_list"
    back_label = "Back to the payment register"
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


class PaymentUpdateView(BackLinkMixin, ActionPermissionMixin, UpdateView):
    back_to_object = True
    back_label = "Back to this payment"
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


class PaymentDetailView(BackLinkMixin, ActionPermissionMixin, DetailView):
    back_url_name = "payments:payment_list"
    back_label = "Back to the payment register"
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
                "journal_entry",
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
        context["allocations"] = (
            Allocation.objects.filter(payment=self.object, is_reversed=False)
            .select_related(
                "sales_invoice",
                "purchase_bill",
                "journal_entry",
                "created_by",
            )
            .order_by("-allocation_date", "-id")
        )
        return context


class PaymentPostView(PostingPermissionMixin, View):
    """Create the payment's one cash/bank journal (PAY-001, GL-002)."""

    required_permission = POST_PAYMENT
    http_method_names = ["post"]

    def post(self, request, pk):
        payment = get_object_or_404(Payment, pk=pk)
        try:
            with transaction.atomic():
                result = services.post_payment(payment, user=request.user)
                audit.record_action(
                    request,
                    AuditAction.POST,
                    result.payment,
                    amount_txn=str(result.payment.amount_txn),
                    journal_entry_id=result.payment.journal_entry_id,
                )
        except (ValidationError, PostingError) as exc:
            messages.error(request, _error_message(exc))
        else:
            verb = "posted" if result.created else "was already posted"
            messages.success(request, f"{result.payment.number} {verb} successfully.")
        return redirect("payments:payment_detail", pk=payment.pk)


class PaymentAllocationView(BackLinkMixin, ActionPermissionMixin, View):
    """Server-rendered workbench for partial and multi-document allocation."""

    required_permission = ALLOCATE_PAYMENT
    back_to_object = True
    back_label = "Back to this payment"
    template_name = "payments/payment_allocation.html"

    def _payment(self, pk):
        return get_object_or_404(
            Payment.objects.select_related("customer", "vendor", "currency", "journal_entry"),
            pk=pk,
        )

    def _targets(self, payment):
        return list(allocation.available_payment_targets(payment))

    def get(self, request, pk):
        payment = self._payment(pk)
        targets = self._targets(payment)
        header_form = PaymentAllocationHeaderForm()
        line_formset = PaymentAllocationLineFormSet(
            initial=[{"target_id": target.pk} for target in targets], prefix="lines"
        )
        return self._render(request, payment, targets, header_form, line_formset)

    def post(self, request, pk):
        payment = self._payment(pk)
        targets = self._targets(payment)
        header_form = PaymentAllocationHeaderForm(request.POST)
        line_formset = PaymentAllocationLineFormSet(request.POST, prefix="lines")
        if header_form.is_valid() and line_formset.is_valid():
            try:
                with transaction.atomic():
                    result = allocation.allocate_payment(
                        payment,
                        lines=line_formset.allocation_lines,
                        allocation_date=header_form.cleaned_data["allocation_date"],
                        user=request.user,
                        batch_key=header_form.cleaned_data["batch_key"],
                    )
                    audit.record_action(
                        request,
                        AuditAction.ALLOCATE,
                        result.source,
                        amount_txn=str(result.amount_txn),
                        remaining_txn=str(result.remaining_txn),
                        allocation_ids=[item.pk for item in result.allocations],
                        batch_key=str(header_form.cleaned_data["batch_key"]),
                    )
            except (ValidationError, PostingError) as exc:
                header_form.add_error(None, _error_message(exc))
            else:
                if result.created:
                    messages.success(
                        request,
                        f"Allocated {result.amount_txn:,.4f} from {payment.number}; "
                        f"{result.remaining_txn:,.4f} remains available.",
                    )
                else:
                    messages.info(request, "This allocation batch was already applied.")
                return redirect("payments:payment_detail", pk=payment.pk)
        return self._render(request, payment, targets, header_form, line_formset)

    def _render(self, request, payment, targets, header_form, line_formset):
        target_map = {target.pk: target for target in targets}
        rows = []
        for form in line_formset.forms:
            raw_id = form["target_id"].value()
            try:
                target_id = int(raw_id)
            except (TypeError, ValueError):
                target_id = None
            rows.append({"form": form, "target": target_map.get(target_id)})

        party_filter = {
            "customer_id": payment.customer_id
            if payment.direction == PaymentDirection.RECEIPT
            else None,
            "vendor_id": payment.vendor_id
            if payment.direction == PaymentDirection.PAYMENT
            else None,
        }
        if payment.direction == PaymentDirection.RECEIPT:
            deferred_fx_count = (
                SalesInvoice.objects.filter(
                    customer_id=party_filter["customer_id"],
                    open_txn__gt=0,
                    status__in=allocation.ALLOCATABLE_STATUSES,
                    is_reversed=False,
                )
                .exclude(currency_id=payment.currency_id, exchange_rate=payment.exchange_rate)
                .count()
            )
        else:
            deferred_fx_count = (
                PurchaseBill.objects.filter(
                    vendor_id=party_filter["vendor_id"],
                    open_txn__gt=0,
                    status__in=allocation.ALLOCATABLE_STATUSES,
                    is_reversed=False,
                )
                .exclude(currency_id=payment.currency_id, exchange_rate=payment.exchange_rate)
                .count()
            )

        context = {
            "payment": payment,
            "page_title": f"Allocate {payment.number}",
            "header_form": header_form,
            "line_formset": line_formset,
            "allocation_rows": rows,
            "deferred_fx_count": deferred_fx_count,
        }
        return self.render_to_response(context)

    def render_to_response(self, context):
        from django.shortcuts import render

        return render(self.request, self.template_name, context)


def _attach_validation_error(form, error):
    if hasattr(error, "message_dict"):
        for field, messages_ in error.message_dict.items():
            target = field if field in form.fields else None
            for message in messages_:
                form.add_error(target, message)
    else:
        for message in error.messages:
            form.add_error(None, message)


def _error_message(error):
    if isinstance(error, ValidationError):
        if hasattr(error, "message_dict"):
            return " ".join(
                message for messages_ in error.message_dict.values() for message in messages_
            )
        return " ".join(error.messages)
    return str(error)


class _ReversalView(BackLinkMixin, ActionPermissionMixin, View):
    """Shared confirm-then-act shell for the two reversal actions."""

    required_permission = REVERSE_DOCUMENT
    back_to_object = True
    back_label = "Back to this payment"
    template_name = "payments/payment_reverse.html"

    def get_payment(self):
        return get_object_or_404(
            Payment.objects.select_related("customer", "vendor", "currency"),
            pk=self.kwargs["pk"],
        )

    def get(self, request, **kwargs):
        return self._render(self.get_payment(), ReversalForm())

    def post(self, request, **kwargs):
        payment = self.get_payment()
        form = ReversalForm(request.POST)
        if form.is_valid():
            try:
                result = self.perform(payment, form)
            except (ValidationError, PostingError) as exc:
                form.add_error(None, _error_message(exc))
            else:
                audit.record_action(
                    request,
                    self.audit_action,
                    payment,
                    reason=form.cleaned_data["reason"],
                    amount_txn=str(result.amount_txn),
                    journal=getattr(result.journal_entry, "number", ""),
                )
                messages.success(request, self.success_message(payment, result))
                return redirect("payments:payment_detail", pk=payment.pk)
        return self._render(payment, form)

    def _render(self, payment, form):
        from django.shortcuts import render

        return render(
            self.request,
            self.template_name,
            {
                "payment": payment,
                "form": form,
                "page_title": self.page_title(payment),
                "intro": self.intro(payment),
                "live_batches": reversal.live_allocation_batches(payment),
                "mode": self.mode,
            },
        )


class PaymentReverseView(_ReversalView):
    """PAY-010: reverse the cash itself, once nothing is applied to it."""

    mode = "payment"
    audit_action = AuditAction.REVERSE

    def page_title(self, payment):
        return f"Reverse {payment.number}"

    def intro(self, payment):
        return (
            "This posts a journal that cancels the original and marks the "
            "payment reversed. The original journal is left untouched."
        )

    def perform(self, payment, form):
        return reversal.reverse_payment(
            payment,
            user=self.request.user,
            reason=form.cleaned_data["reason"],
            reversal_date=form.cleaned_data["reversal_date"],
        )

    def success_message(self, payment, result):
        return f"{payment.number} reversed by journal {result.journal_entry.number}."


class AllocationReverseView(_ReversalView):
    """PAY-011: un-apply one allocation batch and give the balances back."""

    mode = "allocation"
    audit_action = AuditAction.UNALLOCATE
    back_label = "Back to this payment"

    def page_title(self, payment):
        return f"Reverse an allocation of {payment.number}"

    def intro(self, payment):
        return (
            "The settled documents get their open balance back, and any "
            "exchange difference realised at the time is reversed with it."
        )

    def perform(self, payment, form):
        return reversal.reverse_allocation_batch(
            self.kwargs["batch_key"],
            user=self.request.user,
            reason=form.cleaned_data["reason"],
            reversal_date=form.cleaned_data["reversal_date"],
        )

    def success_message(self, payment, result):
        return (
            f"Reversed {result.amount_txn:,.4f} of {payment.number}; "
            f"{result.source.unallocated_txn:,.4f} is available again."
        )


class PaymentVoucherView(BackLinkMixin, ActionPermissionMixin, DetailView):
    """PTY-003: the printable receipt or payment voucher."""

    back_to_object = True
    back_label = "Back to this payment"
    model = Payment
    template_name = "payments/payment_voucher.html"
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
                "journal_entry",
                "posted_by",
            )
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        payment = self.object
        context["company"] = Company.objects.select_related("base_currency").first()
        context["party"] = payment.customer or payment.vendor
        context["allocations"] = (
            Allocation.objects.filter(payment=payment, is_reversed=False)
            .select_related("sales_invoice", "purchase_bill")
            .order_by("allocation_date", "id")
        )
        context["fx_total"] = sum(
            (row.fx_gain_loss_base for row in context["allocations"]), Decimal("0")
        )
        return context


# ---------------------------------------------------------------------------
# Stripe (PAY-013)
# ---------------------------------------------------------------------------
class InvoiceStripeChargeView(ActionPermissionMixin, View):
    """Create a Stripe payment link for one posted invoice, for staff to send.

    Staff-initiated on purpose. The link is generated here and handed to
    whoever is dealing with the customer; nothing in this application is exposed
    to the customer, and no route but the webhook below is reachable without a
    login.
    """

    required_permission = POST_PAYMENT
    http_method_names = ["post"]

    def post(self, request, pk):
        invoice = get_object_or_404(SalesInvoice, pk=pk)
        try:
            checkout = stripe_service.start_checkout(invoice, user=request.user)
        except ValidationError as exc:
            messages.error(request, _error_message(exc))
        else:
            audit.record_action(
                request,
                AuditAction.CREATE,
                invoice,
                stripe_session=checkout.session_id,
                amount_txn=str(checkout.amount_txn),
            )
            messages.success(
                request,
                f"Payment link ready for {invoice.number}. Send it to the "
                "customer - it is shown on this page until it is paid or expires.",
            )
        return redirect("sales:invoice_detail", pk=invoice.pk)


class StripeSettleRetryView(ActionPermissionMixin, View):
    """Try again to post a receipt for a checkout the customer already paid.

    The button behind this exists because the webhook deliberately refuses to
    fail: when the ledger would not take the entry - a closed period, a missing
    rate - it records why and moves on. This is how someone finishes the job
    once they have fixed the cause.
    """

    required_permission = POST_PAYMENT
    http_method_names = ["post"]

    def post(self, request, pk):
        checkout = get_object_or_404(StripeCheckout.objects.select_related("invoice"), pk=pk)
        checkout = stripe_service.post_settled_checkout(checkout)
        if checkout.payment_id:
            messages.success(
                request,
                f"{checkout.payment.number} posted and applied to "
                f"{checkout.invoice.number}.",
            )
        else:
            messages.error(
                request,
                checkout.settlement_error or "The receipt still could not be posted.",
            )
        return redirect("sales:invoice_detail", pk=checkout.invoice_id)


@method_decorator(csrf_exempt, name="dispatch")
class StripeWebhookView(View):
    """Stripe's callback. The only route in this project without a login.

    Three deliberate choices, each of which has a failure mode behind it:

    * **CSRF exempt, but not unauthenticated.** Stripe cannot hold a CSRF
      token. The signature check in the gateway is the authentication, and it
      is not optional - with no signing secret configured every request is
      refused rather than trusted.
    * **The body is a notification, not evidence.** Nothing here reads an
      amount out of the payload. The session id is used to go and ask Stripe
      what happened, so a forged body that somehow passed the signature check
      still could not invent a payment.
    * **200 for anything a retry cannot fix.** Stripe retries non-2xx for days
      and then disables the endpoint. A closed fiscal period is not Stripe's
      problem to solve, so it is recorded and acknowledged; only a genuinely
      transient failure gets a 5xx and asks to be sent again.
    """

    http_method_names = ["post"]

    def post(self, request):
        signature = request.headers.get("Stripe-Signature", "")
        try:
            event = stripe_gateway.verify_webhook(request.body, signature)
        except stripe_gateway.StripeUnavailable as exc:
            # Not configured to receive callbacks at all.
            logger.warning("Stripe webhook rejected: %s", exc)
            return HttpResponse("Stripe webhooks are not configured.", status=503)
        except Exception as exc:
            # Bad signature, replayed timestamp, or an unparseable body. Never
            # echo any of it back; a probe learns nothing from a bare 400.
            logger.warning("Stripe webhook signature rejected: %s", exc)
            return HttpResponse("Invalid signature.", status=400)

        try:
            outcome = stripe_service.handle_event(event)
        except stripe_service.UnknownStripeSession as exc:
            logger.warning("Stripe webhook for unknown session: %s", exc)
            return HttpResponse("Unknown session; ignored.", status=200)
        except stripe_service.StripeSettlementError as exc:
            # Could not reach Stripe to confirm. Ask to be told again.
            logger.warning("Stripe settlement deferred: %s", exc)
            return HttpResponse("Could not confirm with Stripe.", status=503)

        logger.info("Stripe webhook handled: %s", outcome)
        return HttpResponse("ok", status=200)
