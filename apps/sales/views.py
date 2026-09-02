"""
Sales-order screens (SAL-001..SAL-004).

Follows the parties/views.py worked example exactly:
  - FilteredListView for the list
  - AuditedFormMixin + CreateView/UpdateView for entry
  - ConfirmationRequiredMixin for approve/reject
"""

from django.contrib import messages
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.generic import (
    CreateView,
    DetailView,
    UpdateView,
    View,
)

from apps.core import audit
from apps.core.list_views import ChoiceFilter, Column, DateRangeFilter, FilteredListView
from apps.core.mixins import ActionPermissionMixin, ConfirmationRequiredMixin
from apps.core.models import EDITABLE_STATES, AuditEvent, DocumentStatus
from apps.core.permissions import APPROVE_SALES_ORDER, EXPORT_DATA
from apps.sales import services
from apps.sales.forms import SalesOrderForm, SalesOrderLineFormSet
from apps.sales.models import SalesOrder


def _number_lines(formset):
    """
    Assign sequential line_no (1, 2, 3...) to every line in the formset,
    including new ones. SalesOrderLine requires a positive line_no but the user
    never types one, so the view numbers them at save time.
    """
    n = 1
    for lf in formset.forms:
        if not lf.is_valid() or lf.cleaned_data.get("DELETE"):
            continue
        lf.instance.line_no = n
        n += 1


# ---------------------------------------------------------------------------
# List view (UX-002, UX-005)
# ---------------------------------------------------------------------------
class SalesOrderListView(FilteredListView):
    model = SalesOrder
    required_permission = "sales.view_salesorder"
    page_title = "Sales Orders"
    page_subtitle = "Orders awaiting fulfilment, or already completed."
    create_url_name = "sales:so_create"
    create_label = "New sales order"
    export_permission = EXPORT_DATA
    export_filename = "sales_orders"
    default_ordering = "-document_date"
    paginate_by = 25

    columns = [
        Column("number", "Number", sortable=True, link=True, css="font-mono text-xs"),
        Column("customer", "Customer", sortable=True, order_by="customer__name"),
        Column("document_date", "Date", sortable=True),
        Column("warehouse", "Warehouse", order_by="warehouse__code"),
        Column("status", "Status", badge=True, align="center"),
        Column("total_txn", "Total", align="right", money=True, sortable=True),
    ]

    search_fields = [
        "number",
        "customer__name",
        "customer__code",
        "customer_reference",
    ]
    trigram_search_fields = ["customer__name"]

    filters = [
        ChoiceFilter(
            "status",
            "Status",
            list(DocumentStatus.choices),
        ),
        DateRangeFilter("document_date", "Date range"),
    ]

    def get_queryset(self):
        return super().get_queryset().select_related("customer", "warehouse")

    def get_summary(self):
        totals = self.get_queryset().aggregate(
            draft=Count("id", filter=Q(status="DRAFT")),
            submitted=Count("id", filter=Q(status="SUBMITTED")),
            approved=Count("id", filter=Q(status="APPROVED")),
            open_value=Sum(
                "total_txn",
                filter=Q(status__in=["DRAFT", "SUBMITTED", "APPROVED"]),
            ),
        )
        return [
            ("Draft", totals["draft"] or 0),
            ("Submitted", totals["submitted"] or 0),
            ("Approved", totals["approved"] or 0),
            ("Open value", f"${totals['open_value'] or 0:,.2f}"),
        ]


# ---------------------------------------------------------------------------
# Create / Update with audit (ACC-005)
# ---------------------------------------------------------------------------
class SalesOrderCreateView(ActionPermissionMixin, CreateView):
    model = SalesOrder
    form_class = SalesOrderForm
    template_name = "sales/so_form.html"
    required_permission = "sales.add_salesorder"
    extra_context = {"page_title": "New sales order"}

    def get_formset(self):
        return SalesOrderLineFormSet(
            self.request.POST or None,
            instance=self.object,
            prefix="lines",
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        if "line_formset" not in ctx:
            ctx["line_formset"] = self.get_formset()
        ctx["page_subtitle"] = "Create a new order for a customer."
        return ctx

    def form_valid(self, form):
        formset = self.get_formset()
        if not formset.is_valid():
            return self.render_to_response(
                self.get_context_data(form=form, line_formset=formset)
            )

        with transaction.atomic():
            form.instance.number = services.allocate_so_number()
            form.instance.created_by = self.request.user
            form.instance.updated_by = self.request.user
            form.instance.status = DocumentStatus.DRAFT
            self.object = form.save()

            formset.instance = self.object
            _number_lines(formset)
            formset.save()

            services.recalculate_order(self.object)

            audit.record_create(self.request, self.object)
            messages.success(
                self.request,
                f"Sales order {self.object.number} created.",
            )

        return redirect(self.get_success_url())

    def get_success_url(self):
        return reverse("sales:so_detail", args=[self.object.pk])


class SalesOrderUpdateView(ActionPermissionMixin, UpdateView):
    model = SalesOrder
    form_class = SalesOrderForm
    template_name = "sales/so_form.html"
    required_permission = "sales.change_salesorder"

    def get_queryset(self):
        # Posted, completed and cancelled documents are immutable (BR-004).
        return super().get_queryset().filter(status__in=EDITABLE_STATES)

    def get_formset(self):
        return SalesOrderLineFormSet(
            self.request.POST or None,
            instance=self.object,
            prefix="lines",
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        if "line_formset" not in ctx:
            ctx["line_formset"] = self.get_formset()
        ctx["page_title"] = f"Edit {self.object.number}"
        return ctx

    def form_valid(self, form):
        formset = self.get_formset()
        if not formset.is_valid():
            return self.render_to_response(
                self.get_context_data(form=form, line_formset=formset)
            )

        before = audit.snapshot(self.object)

        with transaction.atomic():
            form.instance.updated_by = self.request.user
            self.object = form.save()

            formset.instance = self.object
            _number_lines(formset)
            formset.save()

            services.recalculate_order(self.object)

            event = audit.record_update(self.request, self.object, before)
            if event:
                changed = ", ".join(event.changes.keys())
                messages.success(
                    self.request,
                    f"{self.object.number} updated ({changed}).",
                )
            else:
                messages.info(self.request, "No changes to save.")

        return redirect(self.get_success_url())

    def get_success_url(self):
        return reverse("sales:so_detail", args=[self.object.pk])


# ---------------------------------------------------------------------------
# Detail view
# ---------------------------------------------------------------------------
class SalesOrderDetailView(ActionPermissionMixin, DetailView):
    model = SalesOrder
    template_name = "sales/so_detail.html"
    required_permission = "sales.view_salesorder"
    context_object_name = "order"

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related(
                "customer",
                "warehouse",
                "currency",
                "payment_term",
                "salesperson",
                "approved_by",
            )
            .prefetch_related("lines__product", "lines__unit", "lines__tax_code")
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = self.object.number
        ctx["page_subtitle"] = f"Order for {self.object.customer}"
        ctx["audit_events"] = AuditEvent.objects.filter(
            content_type__app_label="sales",
            content_type__model="salesorder",
            object_id=self.object.pk,
        ).select_related("user")[:20]
        return ctx


# ---------------------------------------------------------------------------
# Submit (DRAFT → SUBMITTED)
# ---------------------------------------------------------------------------
class SalesOrderSubmitView(ActionPermissionMixin, View):
    """Move a draft order into the submitted state (SAL-004)."""

    required_permission = "sales.change_salesorder"

    def post(self, request, pk):
        order = get_object_or_404(SalesOrder, pk=pk)
        try:
            services.submit_order(order, request.user)
            messages.success(
                request,
                f"Order {order.number} submitted for approval.",
            )
        except ValueError as exc:
            messages.error(request, str(exc))

        return redirect("sales:so_detail", pk=pk)


# ---------------------------------------------------------------------------
# Approve / Reject (SUBMITTED → APPROVED or REJECTED)
# ---------------------------------------------------------------------------
class SalesOrderApproveView(ConfirmationRequiredMixin, View):
    """
    Approve a submitted order (SAL-004). Requires the APPROVE_SALES_ORDER
    permission (ACC-004) and an explicit reason (ACC-008).
    """

    required_permission = APPROVE_SALES_ORDER

    def post(self, request, pk):
        order = get_object_or_404(SalesOrder, pk=pk)
        reason = self.get_confirmation_reason(request)

        try:
            services.approve_order(order, request.user, reason=reason)
            messages.success(
                request,
                f"Order {order.number} approved.",
            )
        except ValueError as exc:
            messages.error(request, str(exc))

        return redirect("sales:so_detail", pk=pk)


class SalesOrderRejectView(ConfirmationRequiredMixin, View):
    """
    Reject a submitted order (SAL-004). Requires a reason (ACC-008).
    """

    required_permission = APPROVE_SALES_ORDER

    def post(self, request, pk):
        order = get_object_or_404(SalesOrder, pk=pk)
        reason = self.get_confirmation_reason(request)

        try:
            services.reject_order(order, request.user, reason=reason)
            messages.success(
                request,
                f"Order {order.number} rejected.",
            )
        except ValueError as exc:
            messages.error(request, str(exc))

        return redirect("sales:so_detail", pk=pk)
