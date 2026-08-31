"""
Purchase order screens: list, create/edit with a line formset, detail, and the
PUR-002 approval workflow (submit, approve, reject).

Shape copied from apps/parties/views.py per CONTRIBUTING.md §4d/§4e.
"""

import json

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Max, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View
from django.views.generic import DetailView

from apps.catalog.models import Product
from apps.core import audit
from apps.core.list_views import ChoiceFilter, Column, FilteredListView
from apps.core.mixins import ActionPermissionMixin
from apps.core.models import AuditEvent, DocumentStatus
from apps.core.permissions import APPROVE_PURCHASE_ORDER, EXPORT_DATA
from apps.purchases import services
from apps.purchases.forms import (
    PurchaseOrderForm,
    PurchaseOrderLineFormSet,
    PurchaseOrderRejectForm,
)
from apps.purchases.models import PurchaseOrder


def _products_payload():
    """
    product id -> default unit / purchase price / tax code, so the line
    formset can prefill a new row in the browser without a round trip.
    Authoritative amounts are still computed server-side on save.
    """
    return json.dumps(
        {
            product.pk: {
                "unit": product.unit_id,
                "price": str(product.purchase_price),
                "tax_code": product.default_purchase_tax_code_id,
            }
            for product in Product.objects.filter(is_active=True)
        }
    )


# ---------------------------------------------------------------------------
# List (UX-002)
# ---------------------------------------------------------------------------
class PurchaseOrderListView(FilteredListView):
    model = PurchaseOrder
    required_permission = "purchases.view_purchaseorder"
    page_title = "Purchase orders"
    page_subtitle = "Everything ordered from vendors, and where it stands."
    create_url_name = "purchases:po_create"
    create_label = "New purchase order"
    export_permission = EXPORT_DATA
    export_filename = "purchase-orders"
    default_ordering = "-document_date"

    columns = [
        Column("number", "Number", sortable=True, link=True, css="font-mono text-xs"),
        Column("vendor", "Vendor", sortable=True, order_by="vendor__name"),
        Column("document_date", "Date", sortable=True),
        Column("expected_date", "Expected", sortable=True),
        Column("total_txn", "Total", align="right", money=True, sortable=True),
        Column("status", "Status", badge=True, align="center"),
    ]
    search_fields = ["number", "vendor__name", "vendor_reference"]
    filters = [ChoiceFilter("status", "Status", DocumentStatus.choices)]

    def get_queryset(self):
        return super().get_queryset().select_related("vendor", "warehouse", "currency")

    def get_summary(self):
        totals = PurchaseOrder.objects.aggregate(
            total=Count("id"),
            awaiting_approval=Count("id", filter=Q(status=DocumentStatus.SUBMITTED)),
            approved=Count("id", filter=Q(status=DocumentStatus.APPROVED)),
        )
        return [
            ("Purchase orders", totals["total"]),
            ("Awaiting approval", totals["awaiting_approval"]),
            ("Approved", totals["approved"]),
        ]


# ---------------------------------------------------------------------------
# Create / edit (PUR-001)
# ---------------------------------------------------------------------------
class PurchaseOrderFormView(ActionPermissionMixin, View):
    """Shared GET/POST handling for create and edit — only what differs varies."""

    template_name = "purchases/purchase_order_form.html"
    is_create = True

    def get_object(self, pk):
        return get_object_or_404(PurchaseOrder, pk=pk)

    def is_locked(self, order):
        return not self.is_create and order.status not in ("DRAFT", "SUBMITTED", "REJECTED")

    def render_form(self, request, form, formset, order):
        return render(
            request,
            self.template_name,
            {
                "form": form,
                "formset": formset,
                "empty_form": formset.empty_form,
                "products_data": _products_payload(),
                "object": None if self.is_create else order,
                "page_title": "New purchase order"
                if self.is_create
                else f"Edit {order.number}",
            },
        )

    def get(self, request, pk=None):
        order = PurchaseOrder() if self.is_create else self.get_object(pk)
        if self.is_locked(order):
            messages.error(
                request,
                f"{order.number} is {order.get_status_display()} and can no longer be edited.",
            )
            return redirect(order.get_absolute_url())
        form = PurchaseOrderForm(instance=order)
        formset = PurchaseOrderLineFormSet(instance=order, prefix="lines")
        return self.render_form(request, form, formset, order)

    def post(self, request, pk=None):
        order = PurchaseOrder() if self.is_create else self.get_object(pk)
        if self.is_locked(order):
            messages.error(
                request,
                f"{order.number} is {order.get_status_display()} and can no longer be edited.",
            )
            return redirect(order.get_absolute_url())

        before = None if self.is_create else audit.snapshot(order)
        form = PurchaseOrderForm(request.POST, instance=order)
        formset = PurchaseOrderLineFormSet(request.POST, instance=order, prefix="lines")

        if form.is_valid() and formset.is_valid():
            with transaction.atomic():
                order = form.save(commit=False)
                is_new = order.pk is None
                if is_new:
                    order.number = services.allocate_po_number(order.document_date)
                    order.created_by = request.user
                order.updated_by = request.user
                order.save()

                instances = formset.save(commit=False)
                for obj in formset.deleted_objects:
                    obj.delete()
                next_line_no = (order.lines.aggregate(Max("line_no"))["line_no__max"] or 0) + 1
                for instance in instances:
                    instance.order = order
                    if instance.pk is None:
                        instance.line_no = next_line_no
                        next_line_no += 1
                    instance.save()

                services.recalculate_order(order)

                if is_new:
                    audit.record_create(request, order)
                    messages.success(request, f"{order.number} created as a draft.")
                else:
                    event = audit.record_update(request, order, before)
                    if event:
                        messages.success(request, f"{order.number} updated.")
                    else:
                        messages.info(request, "No changes to save.")

            return redirect(order.get_absolute_url())

        return self.render_form(request, form, formset, order)


class PurchaseOrderCreateView(PurchaseOrderFormView):
    required_permission = "purchases.add_purchaseorder"
    is_create = True


class PurchaseOrderEditView(PurchaseOrderFormView):
    required_permission = "purchases.change_purchaseorder"
    is_create = False


# ---------------------------------------------------------------------------
# Detail (PUR-001, ACC-005)
# ---------------------------------------------------------------------------
class PurchaseOrderDetailView(ActionPermissionMixin, DetailView):
    model = PurchaseOrder
    template_name = "purchases/purchase_order_detail.html"
    required_permission = "purchases.view_purchaseorder"
    context_object_name = "order"

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related(
                "vendor", "warehouse", "currency", "payment_term", "buyer", "approved_by"
            )
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = self.object.number
        ctx["page_subtitle"] = f"Purchase order for {self.object.vendor}"
        ctx["lines"] = self.object.lines.select_related(
            "product", "unit", "tax_code", "warehouse"
        )
        ctx["reject_form"] = PurchaseOrderRejectForm()
        ctx["can_edit"] = self.object.status in ("DRAFT", "SUBMITTED", "REJECTED")
        ctx["audit_events"] = AuditEvent.objects.filter(
            content_type__app_label="purchases",
            content_type__model="purchaseorder",
            object_id=self.object.pk,
        ).select_related("user")[:20]
        return ctx


# ---------------------------------------------------------------------------
# Approval workflow (PUR-002)
# ---------------------------------------------------------------------------
class PurchaseOrderSubmitView(ActionPermissionMixin, View):
    required_permission = "purchases.change_purchaseorder"

    def post(self, request, pk):
        order = get_object_or_404(PurchaseOrder, pk=pk)
        try:
            services.submit_purchase_order(order, request.user, request)
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            if order.status == DocumentStatus.APPROVED:
                messages.success(request, f"{order.number} submitted and auto-approved.")
            else:
                messages.success(request, f"{order.number} submitted for approval.")
        return redirect("purchases:po_detail", pk=pk)


class PurchaseOrderApproveView(ActionPermissionMixin, View):
    required_permission = APPROVE_PURCHASE_ORDER

    def post(self, request, pk):
        order = get_object_or_404(PurchaseOrder, pk=pk)
        try:
            services.approve_purchase_order(order, request.user, request)
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            messages.success(request, f"{order.number} approved.")
        return redirect("purchases:po_detail", pk=pk)


class PurchaseOrderRejectView(ActionPermissionMixin, View):
    required_permission = APPROVE_PURCHASE_ORDER

    def post(self, request, pk):
        order = get_object_or_404(PurchaseOrder, pk=pk)
        form = PurchaseOrderRejectForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Give a reason for rejecting this order.")
            return redirect("purchases:po_detail", pk=pk)
        try:
            services.reject_purchase_order(
                order, request.user, form.cleaned_data["reason"], request
            )
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            messages.success(request, f"{order.number} rejected.")
        return redirect("purchases:po_detail", pk=pk)
