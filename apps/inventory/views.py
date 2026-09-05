"""
Goods receipt, stock transfer and stock adjustment screens: list, create/edit,
detail, and the posting / approval actions (PUR-003, PUR-004, INV-006,
INV-008, INV-009).

Shape copied from apps/purchases/views.py per CONTRIBUTING.md §4d/§4e.
"""

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, F, Max, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View
from django.views.generic import DetailView

from apps.core import audit
from apps.core.list_views import ChoiceFilter, Column, FilteredListView
from apps.core.mixins import ActionPermissionMixin
from apps.core.models import AuditEvent, DocumentStatus
from apps.core.permissions import (
    APPROVE_STOCK_ADJUSTMENT,
    EXPORT_DATA,
    POST_GOODS_RECEIPT,
    POST_STOCK_MOVEMENT,
)
from apps.inventory import services
from apps.inventory.forms import (
    GoodsReceiptForm,
    GoodsReceiptLineFormSet,
    StockAdjustmentForm,
    StockAdjustmentLineFormSet,
    StockAdjustmentRejectForm,
    StockTransferForm,
    StockTransferLineFormSet,
)
from apps.inventory.models import (
    GoodsReceipt,
    MovementType,
    StockAdjustment,
    StockBalance,
    StockMovement,
    StockTransfer,
    Warehouse,
)
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


# ---------------------------------------------------------------------------
# Stock ledger (INV-004, RPT-016, RPT-017)
# ---------------------------------------------------------------------------
class StockLedgerListView(FilteredListView):
    """
    Every posted stock movement, in order, with the running balance it left
    behind — the on-screen equivalent of `fn_stock_card`, across every product
    and warehouse rather than one at a time.
    """

    model = StockMovement
    required_permission = "inventory.view_stockmovement"
    page_title = "Stock ledger"
    page_subtitle = "Every posted movement, in order, with the balance it left behind."
    export_permission = EXPORT_DATA
    export_filename = "stock-ledger"
    default_ordering = "-movement_date"
    show_summary = False

    columns = [
        Column("movement_date", "Date", sortable=True),
        Column("product", "Product"),
        Column("warehouse", "Warehouse", sortable=True, order_by="warehouse__code"),
        Column("get_movement_type_display", "Type", sortable=True, order_by="movement_type"),
        Column("source_doc_number", "Document"),
        Column("signed_quantity", "Qty", align="right"),
        Column("unit_cost", "Unit cost", align="right", money=True),
        Column("balance_quantity_after", "Balance qty", align="right"),
        Column("average_cost_after", "Avg cost", align="right", money=True),
        Column("balance_value_after", "Balance value", align="right", money=True),
    ]
    search_fields = ["product__sku", "product__name", "source_doc_number"]

    @property
    def filters(self):
        warehouses = [(w.pk, str(w)) for w in Warehouse.objects.filter(is_active=True)]
        return [
            ChoiceFilter("warehouse", "Warehouse", warehouses),
            ChoiceFilter("movement_type", "Type", MovementType.choices),
        ]

    def get_queryset(self):
        return super().get_queryset().select_related("product", "warehouse")


# ---------------------------------------------------------------------------
# Inventory valuation (INV-005, RPT-018)
# ---------------------------------------------------------------------------
class StockValuationListView(FilteredListView):
    """
    Quantity on hand and its weighted-average cost, by product and warehouse —
    the screen equivalent of `v_inventory_valuation`, kept current because
    `StockBalance` is updated by every posting rather than recomputed here.
    """

    model = StockBalance
    required_permission = "inventory.view_stockbalance"
    page_title = "Inventory valuation"
    page_subtitle = "What is on hand right now, and what it is worth."
    export_permission = EXPORT_DATA
    export_filename = "inventory-valuation"
    default_ordering = "product__sku"

    columns = [
        Column("product", "Product"),
        Column("warehouse", "Warehouse", sortable=True, order_by="warehouse__code"),
        Column("quantity_on_hand", "On hand", align="right", sortable=True),
        Column("quantity_reserved", "Reserved", align="right"),
        Column("average_cost", "Avg cost", align="right", money=True),
        Column("total_value", "Value", align="right", money=True, sortable=True),
    ]
    search_fields = ["product__sku", "product__name"]

    @property
    def filters(self):
        warehouses = [(w.pk, str(w)) for w in Warehouse.objects.filter(is_active=True)]
        return [ChoiceFilter("warehouse", "Warehouse", warehouses)]

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related("product", "warehouse")
            .filter(product__is_inventory=True)
        )

    def get_summary(self):
        totals = StockBalance.objects.filter(product__is_inventory=True).aggregate(
            items=Count("id", filter=Q(quantity_on_hand__gt=0)),
            total_qty=Sum("quantity_on_hand"),
            total_value=Sum("total_value"),
        )
        below_reorder = StockBalance.objects.filter(
            product__is_inventory=True, quantity_on_hand__lte=F("product__reorder_level")
        ).count()
        return [
            ("Items in stock", totals["items"] or 0),
            ("Total quantity", totals["total_qty"] or 0),
            ("Total value", totals["total_value"] or 0),
            ("Below reorder level", below_reorder),
        ]


# ---------------------------------------------------------------------------
# Low stock (RPT-018)
# ---------------------------------------------------------------------------
class LowStockListView(FilteredListView):
    """
    Balances at or below the product's reorder level — what a buyer should
    look at before writing the next purchase order. A trimmed view over the
    same `StockBalance` rows as inventory valuation, not a separate model.
    """

    model = StockBalance
    required_permission = "inventory.view_stockbalance"
    page_title = "Low stock"
    page_subtitle = "Balances at or below their reorder level."
    export_permission = EXPORT_DATA
    export_filename = "low-stock"
    default_ordering = "product__sku"
    show_summary = False

    columns = [
        Column("product", "Product"),
        Column("warehouse", "Warehouse", sortable=True, order_by="warehouse__code"),
        Column("quantity_on_hand", "On hand", align="right", sortable=True),
        Column("product__reorder_level", "Reorder level", align="right"),
        Column("average_cost", "Avg cost", align="right", money=True),
    ]
    search_fields = ["product__sku", "product__name"]

    @property
    def filters(self):
        warehouses = [(w.pk, str(w)) for w in Warehouse.objects.filter(is_active=True)]
        return [ChoiceFilter("warehouse", "Warehouse", warehouses)]

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related("product", "warehouse")
            .filter(
                product__is_inventory=True, quantity_on_hand__lte=F("product__reorder_level")
            )
        )


# ---------------------------------------------------------------------------
# Stock transfers: list, create/edit, detail, post (INV-008)
# ---------------------------------------------------------------------------
class StockTransferListView(FilteredListView):
    model = StockTransfer
    required_permission = "inventory.view_stocktransfer"
    page_title = "Stock transfers"
    page_subtitle = "Moving stock between warehouses."
    create_url_name = "inventory:st_create"
    create_label = "New transfer"
    export_permission = EXPORT_DATA
    export_filename = "stock-transfers"
    default_ordering = "-document_date"

    columns = [
        Column("number", "Number", sortable=True, link=True, css="font-mono text-xs"),
        Column("from_warehouse", "From", sortable=True, order_by="from_warehouse__code"),
        Column("to_warehouse", "To", sortable=True, order_by="to_warehouse__code"),
        Column("document_date", "Date", sortable=True),
        Column("total_cost_base", "Cost", align="right", money=True, sortable=True),
        Column("status", "Status", badge=True, align="center"),
    ]
    search_fields = ["number", "reason"]
    filters = [ChoiceFilter("status", "Status", DocumentStatus.choices)]

    def get_queryset(self):
        return super().get_queryset().select_related("from_warehouse", "to_warehouse")

    def get_summary(self):
        totals = StockTransfer.objects.aggregate(
            total=Count("id"),
            draft=Count("id", filter=Q(status=DocumentStatus.DRAFT)),
            posted=Count("id", filter=Q(status=DocumentStatus.POSTED)),
        )
        return [
            ("Transfers", totals["total"]),
            ("Draft", totals["draft"]),
            ("Posted", totals["posted"]),
        ]


class StockTransferFormView(ActionPermissionMixin, View):
    """Shared GET/POST handling for create and edit."""

    template_name = "inventory/stock_transfer_form.html"
    is_create = True

    def get_object(self, pk):
        return get_object_or_404(StockTransfer, pk=pk)

    def is_locked(self, transfer):
        return not self.is_create and transfer.status != DocumentStatus.DRAFT

    def render_form(self, request, form, formset, transfer):
        return render(
            request,
            self.template_name,
            {
                "form": form,
                "formset": formset,
                "empty_form": formset.empty_form,
                "object": None if self.is_create else transfer,
                "page_title": "New stock transfer"
                if self.is_create
                else f"Edit {transfer.number}",
            },
        )

    def _locked_redirect(self, request, transfer):
        messages.error(
            request,
            f"{transfer.number} is {transfer.get_status_display()} and can no longer be edited.",
        )
        return redirect(transfer.get_absolute_url())

    def get(self, request, pk=None):
        transfer = StockTransfer() if self.is_create else self.get_object(pk)
        if self.is_locked(transfer):
            return self._locked_redirect(request, transfer)
        form = StockTransferForm(instance=transfer)
        formset = StockTransferLineFormSet(instance=transfer, prefix="lines")
        return self.render_form(request, form, formset, transfer)

    def post(self, request, pk=None):
        transfer = StockTransfer() if self.is_create else self.get_object(pk)
        if self.is_locked(transfer):
            return self._locked_redirect(request, transfer)

        before = None if self.is_create else audit.snapshot(transfer)
        form = StockTransferForm(request.POST, instance=transfer)
        formset = StockTransferLineFormSet(request.POST, instance=transfer, prefix="lines")

        if form.is_valid() and formset.is_valid():
            with transaction.atomic():
                transfer = form.save(commit=False)
                is_new = transfer.pk is None
                if is_new:
                    transfer.number = services.allocate_st_number(transfer.document_date)
                    transfer.created_by = request.user
                transfer.updated_by = request.user
                transfer.save()

                instances = formset.save(commit=False)
                for obj in formset.deleted_objects:
                    obj.delete()
                next_line_no = (
                    transfer.lines.aggregate(Max("line_no"))["line_no__max"] or 0
                ) + 1
                for instance in instances:
                    instance.transfer = transfer
                    if instance.pk is None:
                        instance.line_no = next_line_no
                        next_line_no += 1
                    instance.save()

                services.recalculate_transfer(transfer)

                if is_new:
                    audit.record_create(request, transfer)
                    messages.success(request, f"{transfer.number} created as a draft.")
                else:
                    event = audit.record_update(request, transfer, before)
                    if event:
                        messages.success(request, f"{transfer.number} updated.")
                    else:
                        messages.info(request, "No changes to save.")

            return redirect(transfer.get_absolute_url())

        return self.render_form(request, form, formset, transfer)


class StockTransferCreateView(StockTransferFormView):
    required_permission = "inventory.add_stocktransfer"
    is_create = True


class StockTransferEditView(StockTransferFormView):
    required_permission = "inventory.change_stocktransfer"
    is_create = False


class StockTransferDetailView(ActionPermissionMixin, DetailView):
    model = StockTransfer
    template_name = "inventory/stock_transfer_detail.html"
    required_permission = "inventory.view_stocktransfer"
    context_object_name = "transfer"

    def get_queryset(self):
        return super().get_queryset().select_related("from_warehouse", "to_warehouse")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = self.object.number
        ctx["page_subtitle"] = f"{self.object.from_warehouse} → {self.object.to_warehouse}"
        ctx["lines"] = self.object.lines.select_related("product")
        ctx["can_edit"] = self.object.status == DocumentStatus.DRAFT
        ctx["audit_events"] = AuditEvent.objects.filter(
            content_type__app_label="inventory",
            content_type__model="stocktransfer",
            object_id=self.object.pk,
        ).select_related("user")[:20]
        return ctx


class StockTransferPostView(ActionPermissionMixin, View):
    required_permission = POST_STOCK_MOVEMENT

    def post(self, request, pk):
        transfer = get_object_or_404(StockTransfer, pk=pk)
        try:
            services.post_stock_transfer(transfer, request.user, request)
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            messages.success(request, f"{transfer.number} posted — stock moved.")
        return redirect("inventory:st_detail", pk=pk)


# ---------------------------------------------------------------------------
# Stock adjustments: list, create/edit, detail, approval workflow, post
# (INV-009)
# ---------------------------------------------------------------------------
class StockAdjustmentListView(FilteredListView):
    model = StockAdjustment
    required_permission = "inventory.view_stockadjustment"
    page_title = "Stock adjustments"
    page_subtitle = "Correcting stock on hand, with a reason and an audit trail."
    create_url_name = "inventory:sa_create"
    create_label = "New adjustment"
    export_permission = EXPORT_DATA
    export_filename = "stock-adjustments"
    default_ordering = "-document_date"

    columns = [
        Column("number", "Number", sortable=True, link=True, css="font-mono text-xs"),
        Column("warehouse", "Warehouse", sortable=True, order_by="warehouse__code"),
        Column("reason", "Reason", sortable=True, order_by="reason__name"),
        Column("document_date", "Date", sortable=True),
        Column("total_value_base", "Value", align="right", money=True, sortable=True),
        Column("status", "Status", badge=True, align="center"),
    ]
    search_fields = ["number", "narration"]
    filters = [ChoiceFilter("status", "Status", DocumentStatus.choices)]

    def get_queryset(self):
        return super().get_queryset().select_related("warehouse", "reason")

    def get_summary(self):
        totals = StockAdjustment.objects.aggregate(
            total=Count("id"),
            draft=Count("id", filter=Q(status=DocumentStatus.DRAFT)),
            posted=Count("id", filter=Q(status=DocumentStatus.POSTED)),
        )
        return [
            ("Adjustments", totals["total"]),
            ("Draft", totals["draft"]),
            ("Posted", totals["posted"]),
        ]


class StockAdjustmentFormView(ActionPermissionMixin, View):
    """Shared GET/POST handling for create and edit."""

    template_name = "inventory/stock_adjustment_form.html"
    is_create = True

    def get_object(self, pk):
        return get_object_or_404(StockAdjustment, pk=pk)

    def is_locked(self, adjustment):
        return not self.is_create and adjustment.status not in (
            DocumentStatus.DRAFT,
            DocumentStatus.SUBMITTED,
            DocumentStatus.REJECTED,
        )

    def render_form(self, request, form, formset, adjustment):
        return render(
            request,
            self.template_name,
            {
                "form": form,
                "formset": formset,
                "empty_form": formset.empty_form,
                "object": None if self.is_create else adjustment,
                "page_title": "New stock adjustment"
                if self.is_create
                else f"Edit {adjustment.number}",
            },
        )

    def _locked_redirect(self, request, adjustment):
        messages.error(
            request,
            f"{adjustment.number} is {adjustment.get_status_display()} "
            "and can no longer be edited.",
        )
        return redirect(adjustment.get_absolute_url())

    def get(self, request, pk=None):
        adjustment = StockAdjustment() if self.is_create else self.get_object(pk)
        if self.is_locked(adjustment):
            return self._locked_redirect(request, adjustment)
        form = StockAdjustmentForm(instance=adjustment)
        formset = StockAdjustmentLineFormSet(instance=adjustment, prefix="lines")
        return self.render_form(request, form, formset, adjustment)

    def post(self, request, pk=None):
        adjustment = StockAdjustment() if self.is_create else self.get_object(pk)
        if self.is_locked(adjustment):
            return self._locked_redirect(request, adjustment)

        before = None if self.is_create else audit.snapshot(adjustment)
        form = StockAdjustmentForm(request.POST, instance=adjustment)
        formset = StockAdjustmentLineFormSet(request.POST, instance=adjustment, prefix="lines")

        if form.is_valid() and formset.is_valid():
            reason = form.cleaned_data["reason"]
            bad_lines = services.validate_adjustment_directions(reason, formset.cleaned_data)
            if bad_lines:
                direction = "increase" if reason.increases_stock else "decrease"
                messages.error(
                    request,
                    f'"{reason}" only allows lines that {direction} stock — check line '
                    f'{", ".join(map(str, bad_lines))}.',
                )
                return self.render_form(request, form, formset, adjustment)

            with transaction.atomic():
                adjustment = form.save(commit=False)
                is_new = adjustment.pk is None
                if is_new:
                    adjustment.number = services.allocate_sa_number(adjustment.document_date)
                    adjustment.created_by = request.user
                adjustment.updated_by = request.user
                adjustment.save()

                instances = formset.save(commit=False)
                for obj in formset.deleted_objects:
                    obj.delete()
                next_line_no = (
                    adjustment.lines.aggregate(Max("line_no"))["line_no__max"] or 0
                ) + 1
                for instance in instances:
                    instance.adjustment = adjustment
                    if instance.pk is None:
                        instance.line_no = next_line_no
                        next_line_no += 1
                    instance.save()

                services.recalculate_adjustment(adjustment)

                if is_new:
                    audit.record_create(request, adjustment)
                    messages.success(request, f"{adjustment.number} created as a draft.")
                else:
                    event = audit.record_update(request, adjustment, before)
                    if event:
                        messages.success(request, f"{adjustment.number} updated.")
                    else:
                        messages.info(request, "No changes to save.")

            return redirect(adjustment.get_absolute_url())

        return self.render_form(request, form, formset, adjustment)


class StockAdjustmentCreateView(StockAdjustmentFormView):
    required_permission = "inventory.add_stockadjustment"
    is_create = True


class StockAdjustmentEditView(StockAdjustmentFormView):
    required_permission = "inventory.change_stockadjustment"
    is_create = False


class StockAdjustmentDetailView(ActionPermissionMixin, DetailView):
    model = StockAdjustment
    template_name = "inventory/stock_adjustment_detail.html"
    required_permission = "inventory.view_stockadjustment"
    context_object_name = "adjustment"

    def get_queryset(self):
        return super().get_queryset().select_related("warehouse", "reason")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = self.object.number
        ctx["page_subtitle"] = f"{self.object.reason} at {self.object.warehouse}"
        ctx["lines"] = self.object.lines.select_related("product")
        ctx["reject_form"] = StockAdjustmentRejectForm()
        ctx["can_edit"] = self.object.status in (
            DocumentStatus.DRAFT,
            DocumentStatus.SUBMITTED,
            DocumentStatus.REJECTED,
        )
        ctx["audit_events"] = AuditEvent.objects.filter(
            content_type__app_label="inventory",
            content_type__model="stockadjustment",
            object_id=self.object.pk,
        ).select_related("user")[:20]
        return ctx


class StockAdjustmentSubmitView(ActionPermissionMixin, View):
    required_permission = "inventory.change_stockadjustment"

    def post(self, request, pk):
        adjustment = get_object_or_404(StockAdjustment, pk=pk)
        try:
            services.submit_stock_adjustment(adjustment, request.user, request)
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            if adjustment.status == DocumentStatus.APPROVED:
                messages.success(request, f"{adjustment.number} submitted and auto-approved.")
            else:
                messages.success(request, f"{adjustment.number} submitted for approval.")
        return redirect("inventory:sa_detail", pk=pk)


class StockAdjustmentApproveView(ActionPermissionMixin, View):
    required_permission = APPROVE_STOCK_ADJUSTMENT

    def post(self, request, pk):
        adjustment = get_object_or_404(StockAdjustment, pk=pk)
        try:
            services.approve_stock_adjustment(adjustment, request.user, request)
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            messages.success(request, f"{adjustment.number} approved.")
        return redirect("inventory:sa_detail", pk=pk)


class StockAdjustmentRejectView(ActionPermissionMixin, View):
    required_permission = APPROVE_STOCK_ADJUSTMENT

    def post(self, request, pk):
        adjustment = get_object_or_404(StockAdjustment, pk=pk)
        form = StockAdjustmentRejectForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Give a reason for rejecting this adjustment.")
            return redirect("inventory:sa_detail", pk=pk)
        try:
            services.reject_stock_adjustment(
                adjustment, request.user, form.cleaned_data["reason"], request
            )
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            messages.success(request, f"{adjustment.number} rejected.")
        return redirect("inventory:sa_detail", pk=pk)


class StockAdjustmentPostView(ActionPermissionMixin, View):
    required_permission = POST_STOCK_MOVEMENT

    def post(self, request, pk):
        adjustment = get_object_or_404(StockAdjustment, pk=pk)
        try:
            services.post_stock_adjustment(adjustment, request.user, request)
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            messages.success(request, f"{adjustment.number} posted — stock updated.")
        return redirect("inventory:sa_detail", pk=pk)
