"""
Goods receipt screens: list, create/edit with an accept/reject line formset,
detail, and post (PUR-003, PUR-004, INV-006).

Shape copied from apps/purchases/views.py per CONTRIBUTING.md §4d/§4e.
"""

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Max, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View
from django.views.generic import DetailView

from apps.core import audit
from apps.core.list_views import ChoiceFilter, Column, FilteredListView
from apps.core.mixins import ActionPermissionMixin
from apps.core.models import AuditEvent, DocumentStatus
from apps.core.permissions import EXPORT_DATA, POST_GOODS_RECEIPT
from apps.inventory import services
from apps.inventory.forms import GoodsReceiptForm, GoodsReceiptLineFormSet
from apps.inventory.models import GoodsReceipt
from apps.purchases.models import PurchaseOrder


# ---------------------------------------------------------------------------
# List (UX-002)
# ---------------------------------------------------------------------------
class GoodsReceiptListView(FilteredListView):
    model = GoodsReceipt
    required_permission = "inventory.view_goodsreceipt"
    page_title = "Goods receipts"
    page_subtitle = "What arrived from vendors, and what it cost."
    create_url_name = "inventory:gr_create"
    create_label = "New receipt"
    export_permission = EXPORT_DATA
    export_filename = "goods-receipts"
    default_ordering = "-document_date"

    columns = [
        Column("number", "Number", sortable=True, link=True, css="font-mono text-xs"),
        Column("vendor", "Vendor", sortable=True, order_by="vendor__name"),
        Column("warehouse", "Warehouse", sortable=True, order_by="warehouse__code"),
        Column("document_date", "Date", sortable=True),
        Column("total_cost_base", "Cost", align="right", money=True, sortable=True),
        Column("status", "Status", badge=True, align="center"),
    ]
    search_fields = ["number", "vendor__name", "vendor_delivery_note"]
    filters = [ChoiceFilter("status", "Status", DocumentStatus.choices)]

    def get_queryset(self):
        return super().get_queryset().select_related("vendor", "warehouse")

    def get_summary(self):
        totals = GoodsReceipt.objects.aggregate(
            total=Count("id"),
            draft=Count("id", filter=Q(status=DocumentStatus.DRAFT)),
            posted=Count("id", filter=Q(status=DocumentStatus.POSTED)),
        )
        return [
            ("Goods receipts", totals["total"]),
            ("Draft", totals["draft"]),
            ("Posted", totals["posted"]),
        ]


# ---------------------------------------------------------------------------
# Create / edit (PUR-003)
# ---------------------------------------------------------------------------
def _receipt_initial_from_order(purchase_order):
    """Prefill a new receipt's lines from a PO's open (not yet received) qty."""
    initial_lines = []
    for line in purchase_order.lines.select_related("product", "unit"):
        remaining = line.quantity - line.quantity_received
        if remaining <= 0:
            continue
        initial_lines.append(
            {
                "purchase_order_line": line.pk,
                "product": line.product_id,
                "description": line.description,
                "unit": line.unit_id,
                "quantity_received": remaining,
            }
        )
    return initial_lines


class GoodsReceiptFormView(ActionPermissionMixin, View):
    """Shared GET/POST handling for create and edit."""

    template_name = "inventory/goods_receipt_form.html"
    is_create = True

    def get_object(self, pk):
        return get_object_or_404(GoodsReceipt, pk=pk)

    def is_locked(self, receipt):
        return not self.is_create and receipt.status != DocumentStatus.DRAFT

    def render_form(self, request, form, formset, receipt):
        return render(
            request,
            self.template_name,
            {
                "form": form,
                "formset": formset,
                "empty_form": formset.empty_form,
                "object": None if self.is_create else receipt,
                "page_title": "New goods receipt"
                if self.is_create
                else f"Edit {receipt.number}",
            },
        )

    def get(self, request, pk=None):
        receipt = GoodsReceipt() if self.is_create else self.get_object(pk)
        if self.is_locked(receipt):
            messages.error(
                request,
                f"{receipt.number} is {receipt.get_status_display()} and can no longer be edited.",
            )
            return redirect(receipt.get_absolute_url())

        initial_lines = None
        if self.is_create and request.GET.get("po"):
            source_order = get_object_or_404(PurchaseOrder, pk=request.GET["po"])
            initial_lines = _receipt_initial_from_order(source_order)
            form = GoodsReceiptForm(
                instance=receipt,
                initial={
                    "vendor": source_order.vendor_id,
                    "purchase_order": source_order.pk,
                    "warehouse": source_order.warehouse_id,
                },
            )
        else:
            form = GoodsReceiptForm(instance=receipt)

        formset = GoodsReceiptLineFormSet(
            instance=receipt, prefix="lines", initial=initial_lines or None
        )
        if initial_lines:
            formset.extra = len(initial_lines)
        return self.render_form(request, form, formset, receipt)

    def post(self, request, pk=None):
        receipt = GoodsReceipt() if self.is_create else self.get_object(pk)
        if self.is_locked(receipt):
            messages.error(
                request,
                f"{receipt.number} is {receipt.get_status_display()} and can no longer be edited.",
            )
            return redirect(receipt.get_absolute_url())

        before = None if self.is_create else audit.snapshot(receipt)
        form = GoodsReceiptForm(request.POST, instance=receipt)
        formset = GoodsReceiptLineFormSet(request.POST, instance=receipt, prefix="lines")

        if form.is_valid() and formset.is_valid():
            with transaction.atomic():
                receipt = form.save(commit=False)
                is_new = receipt.pk is None
                if is_new:
                    receipt.number = services.allocate_gr_number(receipt.document_date)
                    receipt.created_by = request.user
                receipt.updated_by = request.user
                receipt.save()

                instances = formset.save(commit=False)
                for obj in formset.deleted_objects:
                    obj.delete()
                next_line_no = (
                    receipt.lines.aggregate(Max("line_no"))["line_no__max"] or 0
                ) + 1
                for instance in instances:
                    instance.receipt = receipt
                    if instance.pk is None:
                        instance.line_no = next_line_no
                        next_line_no += 1
                    # gr_line_split_sums is a same-statement CHECK constraint —
                    # accepted must already equal received-rejected at INSERT
                    # time. recalculate_receipt() below repeats this with the
                    # authoritative cost figures; this just satisfies the DB.
                    instance.quantity_accepted = instance.quantity_received - (
                        instance.quantity_rejected or 0
                    )
                    instance.save()

                services.recalculate_receipt(receipt)

                if is_new:
                    audit.record_create(request, receipt)
                    messages.success(request, f"{receipt.number} created as a draft.")
                else:
                    event = audit.record_update(request, receipt, before)
                    if event:
                        messages.success(request, f"{receipt.number} updated.")
                    else:
                        messages.info(request, "No changes to save.")

            return redirect(receipt.get_absolute_url())

        return self.render_form(request, form, formset, receipt)


class GoodsReceiptCreateView(GoodsReceiptFormView):
    required_permission = "inventory.add_goodsreceipt"
    is_create = True


class GoodsReceiptEditView(GoodsReceiptFormView):
    required_permission = "inventory.change_goodsreceipt"
    is_create = False


# ---------------------------------------------------------------------------
# Detail (PUR-003, ACC-005)
# ---------------------------------------------------------------------------
class GoodsReceiptDetailView(ActionPermissionMixin, DetailView):
    model = GoodsReceipt
    template_name = "inventory/goods_receipt_detail.html"
    required_permission = "inventory.view_goodsreceipt"
    context_object_name = "receipt"

    def get_queryset(self):
        return super().get_queryset().select_related("vendor", "warehouse", "purchase_order")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = self.object.number
        ctx["page_subtitle"] = f"Goods receipt from {self.object.vendor}"
        ctx["lines"] = self.object.lines.select_related("product", "unit")
        ctx["can_edit"] = self.object.status == DocumentStatus.DRAFT
        ctx["audit_events"] = AuditEvent.objects.filter(
            content_type__app_label="inventory",
            content_type__model="goodsreceipt",
            object_id=self.object.pk,
        ).select_related("user")[:20]
        return ctx


# ---------------------------------------------------------------------------
# Post (INV-006)
# ---------------------------------------------------------------------------
class GoodsReceiptPostView(ActionPermissionMixin, View):
    required_permission = POST_GOODS_RECEIPT

    def post(self, request, pk):
        receipt = get_object_or_404(GoodsReceipt, pk=pk)
        try:
            services.post_goods_receipt(receipt, request.user, request)
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            messages.success(request, f"{receipt.number} posted — stock updated.")
        return redirect("inventory:gr_detail", pk=pk)
