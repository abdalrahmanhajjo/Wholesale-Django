"""
Purchase order, purchase bill, purchase return and vendor debit note screens:
list, create/edit with a line formset, detail, and the PUR-002 approval
workflow / posting actions (submit, approve, reject, post).

Shape copied from apps/parties/views.py per CONTRIBUTING.md §4d/§4e.
"""

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
from apps.core.permissions import (
    APPROVE_PURCHASE_ORDER,
    EXPORT_DATA,
    POST_DEBIT_NOTE,
    POST_PURCHASE_BILL,
    POST_STOCK_MOVEMENT,
)
from apps.purchases import services
from apps.purchases.forms import (
    PurchaseBillForm,
    PurchaseBillLineFormSet,
    PurchaseOrderForm,
    PurchaseOrderLineFormSet,
    PurchaseOrderRejectForm,
    PurchaseReturnForm,
    PurchaseReturnLineFormSet,
    VendorDebitNoteForm,
    VendorDebitNoteLineFormSet,
)
from apps.purchases.models import PurchaseBill, PurchaseOrder, PurchaseReturn, VendorDebitNote


def _products_payload():
    """
    product id -> default unit / purchase price / tax code, so the line
    formset can prefill a new row in the browser without a round trip.
    Authoritative amounts are still computed server-side on save.

    Returns a dict, not a JSON string: the template renders it through
    ``json_script``, which escapes the characters that would otherwise let a
    value containing ``</script>`` break out of the tag it is embedded in.
    Every value here happens to be numeric today, but the equivalent map in the
    sales app already carries a product *name*, and that is one edit away from
    being true here too.
    """
    return {
        product.pk: {
            "unit": product.unit_id,
            "price": str(product.purchase_price),
            "tax_code": product.default_purchase_tax_code_id,
        }
        for product in Product.objects.filter(is_active=True)
    }


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


# ---------------------------------------------------------------------------
# Purchase bill: list, create/edit, detail, post (PUR-005..PUR-008)
# ---------------------------------------------------------------------------
class PurchaseBillListView(FilteredListView):
    model = PurchaseBill
    required_permission = "purchases.view_purchasebill"
    page_title = "Purchase bills"
    page_subtitle = "Vendor invoices and what they owe against."
    create_url_name = "purchases:bill_create"
    create_label = "New bill"
    export_permission = EXPORT_DATA
    export_filename = "purchase-bills"
    default_ordering = "-document_date"

    columns = [
        Column("number", "Number", sortable=True, link=True, css="font-mono text-xs"),
        Column("vendor", "Vendor", sortable=True, order_by="vendor__name"),
        Column("vendor_invoice_number", "Vendor invoice #"),
        Column("document_date", "Date", sortable=True),
        Column("due_date", "Due", sortable=True),
        Column("total_txn", "Total", align="right", money=True, sortable=True),
        Column("open_txn", "Open", align="right", money=True, sortable=True),
        Column("status", "Status", badge=True, align="center"),
    ]
    search_fields = ["number", "vendor__name", "vendor_invoice_number"]
    filters = [ChoiceFilter("status", "Status", DocumentStatus.choices)]

    def get_queryset(self):
        return super().get_queryset().select_related("vendor", "currency")

    def get_summary(self):
        totals = PurchaseBill.objects.aggregate(
            total=Count("id"),
            draft=Count("id", filter=Q(status=DocumentStatus.DRAFT)),
            posted=Count("id", filter=Q(status=DocumentStatus.POSTED)),
        )
        return [
            ("Purchase bills", totals["total"]),
            ("Draft", totals["draft"]),
            ("Posted", totals["posted"]),
        ]


def _bill_initial_from_source(purchase_order):
    """Prefill a new bill's header and open lines from an approved PO (PUR-005)."""
    initial_header = {
        "vendor": purchase_order.vendor_id,
        "purchase_order": purchase_order.pk,
        "warehouse": purchase_order.warehouse_id,
        "payment_term": purchase_order.payment_term_id,
        "currency": purchase_order.currency_id,
        "exchange_rate": purchase_order.exchange_rate,
    }
    initial_lines = []
    for line in purchase_order.lines.select_related(
        "product", "unit", "tax_code", "warehouse"
    ):
        remaining = line.quantity - line.quantity_billed
        if remaining <= 0:
            continue
        initial_lines.append(
            {
                "purchase_order_line": line.pk,
                "is_stock_line": True,
                "product": line.product_id,
                "description": line.description,
                "unit": line.unit_id,
                "warehouse": line.warehouse_id,
                "tax_code": line.tax_code_id,
                "quantity": remaining,
                "unit_price": line.unit_price,
                "discount_percent": line.discount_percent,
            }
        )
    return initial_header, initial_lines


class PurchaseBillFormView(ActionPermissionMixin, View):
    """Shared GET/POST handling for create and edit."""

    template_name = "purchases/bill_form.html"
    is_create = True

    def get_object(self, pk):
        return get_object_or_404(PurchaseBill, pk=pk)

    def is_locked(self, bill):
        return not self.is_create and bill.status != DocumentStatus.DRAFT

    def render_form(self, request, form, formset, bill):
        return render(
            request,
            self.template_name,
            {
                "form": form,
                "formset": formset,
                "empty_form": formset.empty_form,
                "products_data": _products_payload(),
                "object": None if self.is_create else bill,
                "page_title": "New purchase bill" if self.is_create else f"Edit {bill.number}",
            },
        )

    def get(self, request, pk=None):
        bill = PurchaseBill() if self.is_create else self.get_object(pk)
        if self.is_locked(bill):
            messages.error(
                request,
                f"{bill.number} is {bill.get_status_display()} and can no longer be edited.",
            )
            return redirect(bill.get_absolute_url())

        initial_lines = None
        if self.is_create and request.GET.get("po"):
            source_order = get_object_or_404(PurchaseOrder, pk=request.GET["po"])
            initial_header, initial_lines = _bill_initial_from_source(source_order)
            form = PurchaseBillForm(instance=bill, initial=initial_header)
        else:
            form = PurchaseBillForm(instance=bill)

        formset = PurchaseBillLineFormSet(
            instance=bill, prefix="lines", initial=initial_lines or None
        )
        if initial_lines:
            formset.extra = len(initial_lines)
        return self.render_form(request, form, formset, bill)

    def post(self, request, pk=None):
        bill = PurchaseBill() if self.is_create else self.get_object(pk)
        if self.is_locked(bill):
            messages.error(
                request,
                f"{bill.number} is {bill.get_status_display()} and can no longer be edited.",
            )
            return redirect(bill.get_absolute_url())

        before = None if self.is_create else audit.snapshot(bill)
        form = PurchaseBillForm(request.POST, instance=bill)
        formset = PurchaseBillLineFormSet(request.POST, instance=bill, prefix="lines")

        if form.is_valid() and formset.is_valid():
            with transaction.atomic():
                bill = form.save(commit=False)
                is_new = bill.pk is None
                if is_new:
                    bill.number = services.allocate_pb_number(bill.document_date)
                    bill.created_by = request.user
                bill.updated_by = request.user
                bill.save()

                instances = formset.save(commit=False)
                for obj in formset.deleted_objects:
                    obj.delete()
                next_line_no = (bill.lines.aggregate(Max("line_no"))["line_no__max"] or 0) + 1
                for instance in instances:
                    instance.bill = bill
                    if instance.pk is None:
                        instance.line_no = next_line_no
                        next_line_no += 1
                    instance.save()

                services.recalculate_bill(bill)

                if is_new:
                    audit.record_create(request, bill)
                    messages.success(request, f"{bill.number} created as a draft.")
                else:
                    event = audit.record_update(request, bill, before)
                    if event:
                        messages.success(request, f"{bill.number} updated.")
                    else:
                        messages.info(request, "No changes to save.")

            return redirect(bill.get_absolute_url())

        return self.render_form(request, form, formset, bill)


class PurchaseBillCreateView(PurchaseBillFormView):
    required_permission = "purchases.add_purchasebill"
    is_create = True


class PurchaseBillEditView(PurchaseBillFormView):
    required_permission = "purchases.change_purchasebill"
    is_create = False


class PurchaseBillDetailView(ActionPermissionMixin, DetailView):
    model = PurchaseBill
    template_name = "purchases/bill_detail.html"
    required_permission = "purchases.view_purchasebill"
    context_object_name = "bill"

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related(
                "vendor", "currency", "payment_term", "purchase_order", "goods_receipt"
            )
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = self.object.number
        ctx["page_subtitle"] = f"Purchase bill for {self.object.vendor}"
        ctx["lines"] = self.object.lines.select_related(
            "product", "unit", "tax_code", "warehouse", "expense_account"
        )
        ctx["can_edit"] = self.object.status == DocumentStatus.DRAFT
        ctx["audit_events"] = AuditEvent.objects.filter(
            content_type__app_label="purchases",
            content_type__model="purchasebill",
            object_id=self.object.pk,
        ).select_related("user")[:20]
        return ctx


class PurchaseBillPostView(ActionPermissionMixin, View):
    required_permission = POST_PURCHASE_BILL

    def post(self, request, pk):
        bill = get_object_or_404(PurchaseBill, pk=pk)
        try:
            services.post_purchase_bill(bill, request.user, request)
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            messages.success(request, f"{bill.number} posted.")
        return redirect("purchases:bill_detail", pk=pk)


# ---------------------------------------------------------------------------
# Purchase return: list, create/edit, detail, post (RET-005, RET-008)
# ---------------------------------------------------------------------------
def _return_initial_from_bill(bill):
    """Prefill a new return's header and open lines from a posted bill."""
    initial_header = {
        "vendor": bill.vendor_id,
        "original_bill": bill.pk,
        "warehouse": bill.warehouse_id,
    }
    initial_lines = []
    for line in bill.lines.filter(is_stock_line=True).select_related("product"):
        remaining = line.quantity - line.quantity_returned
        if remaining <= 0:
            continue
        initial_lines.append(
            {
                "bill_line": line.pk,
                "product": line.product_id,
                "quantity": remaining,
            }
        )
    return initial_header, initial_lines


class PurchaseReturnListView(FilteredListView):
    model = PurchaseReturn
    required_permission = "purchases.view_purchasereturn"
    page_title = "Purchase returns"
    page_subtitle = "Goods shipped back to a vendor."
    create_url_name = "purchases:pr_create"
    create_label = "New return"
    export_permission = EXPORT_DATA
    export_filename = "purchase-returns"
    default_ordering = "-document_date"

    columns = [
        Column("number", "Number", sortable=True, link=True, css="font-mono text-xs"),
        Column("vendor", "Vendor", sortable=True, order_by="vendor__name"),
        Column("warehouse", "Warehouse", sortable=True, order_by="warehouse__code"),
        Column("document_date", "Date", sortable=True),
        Column("total_cost_base", "Cost", align="right", money=True, sortable=True),
        Column("status", "Status", badge=True, align="center"),
    ]
    search_fields = ["number", "vendor__name", "reason"]
    filters = [ChoiceFilter("status", "Status", DocumentStatus.choices)]

    def get_queryset(self):
        return super().get_queryset().select_related("vendor", "warehouse")

    def get_summary(self):
        totals = PurchaseReturn.objects.aggregate(
            total=Count("id"),
            draft=Count("id", filter=Q(status=DocumentStatus.DRAFT)),
            posted=Count("id", filter=Q(status=DocumentStatus.POSTED)),
        )
        return [
            ("Returns", totals["total"]),
            ("Draft", totals["draft"]),
            ("Posted", totals["posted"]),
        ]


class PurchaseReturnFormView(ActionPermissionMixin, View):
    """Shared GET/POST handling for create and edit."""

    template_name = "purchases/purchase_return_form.html"
    is_create = True

    def get_object(self, pk):
        return get_object_or_404(PurchaseReturn, pk=pk)

    def is_locked(self, purchase_return):
        return not self.is_create and purchase_return.status != DocumentStatus.DRAFT

    def render_form(self, request, form, formset, purchase_return):
        return render(
            request,
            self.template_name,
            {
                "form": form,
                "formset": formset,
                "empty_form": formset.empty_form,
                "object": None if self.is_create else purchase_return,
                "page_title": "New purchase return"
                if self.is_create
                else f"Edit {purchase_return.number}",
            },
        )

    def _locked_redirect(self, request, purchase_return):
        messages.error(
            request,
            f"{purchase_return.number} is {purchase_return.get_status_display()} "
            "and can no longer be edited.",
        )
        return redirect(purchase_return.get_absolute_url())

    def get(self, request, pk=None):
        purchase_return = PurchaseReturn() if self.is_create else self.get_object(pk)
        if self.is_locked(purchase_return):
            return self._locked_redirect(request, purchase_return)

        initial_lines = None
        if self.is_create and request.GET.get("bill"):
            source_bill = get_object_or_404(PurchaseBill, pk=request.GET["bill"])
            initial_header, initial_lines = _return_initial_from_bill(source_bill)
            form = PurchaseReturnForm(instance=purchase_return, initial=initial_header)
        else:
            form = PurchaseReturnForm(instance=purchase_return)

        formset = PurchaseReturnLineFormSet(
            instance=purchase_return, prefix="lines", initial=initial_lines or None
        )
        if initial_lines:
            formset.extra = len(initial_lines)
        return self.render_form(request, form, formset, purchase_return)

    def post(self, request, pk=None):
        purchase_return = PurchaseReturn() if self.is_create else self.get_object(pk)
        if self.is_locked(purchase_return):
            return self._locked_redirect(request, purchase_return)

        before = None if self.is_create else audit.snapshot(purchase_return)
        form = PurchaseReturnForm(request.POST, instance=purchase_return)
        formset = PurchaseReturnLineFormSet(
            request.POST, instance=purchase_return, prefix="lines"
        )

        if form.is_valid() and formset.is_valid():
            with transaction.atomic():
                purchase_return = form.save(commit=False)
                is_new = purchase_return.pk is None
                if is_new:
                    purchase_return.number = services.allocate_pr_number(
                        purchase_return.document_date
                    )
                    purchase_return.created_by = request.user
                purchase_return.updated_by = request.user
                purchase_return.save()

                instances = formset.save(commit=False)
                for obj in formset.deleted_objects:
                    obj.delete()
                next_line_no = (
                    purchase_return.lines.aggregate(Max("line_no"))["line_no__max"] or 0
                ) + 1
                for instance in instances:
                    instance.purchase_return = purchase_return
                    if instance.pk is None:
                        instance.line_no = next_line_no
                        next_line_no += 1
                    instance.save()

                services.recalculate_purchase_return(purchase_return)

                if is_new:
                    audit.record_create(request, purchase_return)
                    messages.success(request, f"{purchase_return.number} created as a draft.")
                else:
                    event = audit.record_update(request, purchase_return, before)
                    if event:
                        messages.success(request, f"{purchase_return.number} updated.")
                    else:
                        messages.info(request, "No changes to save.")

            return redirect(purchase_return.get_absolute_url())

        return self.render_form(request, form, formset, purchase_return)


class PurchaseReturnCreateView(PurchaseReturnFormView):
    required_permission = "purchases.add_purchasereturn"
    is_create = True


class PurchaseReturnEditView(PurchaseReturnFormView):
    required_permission = "purchases.change_purchasereturn"
    is_create = False


class PurchaseReturnDetailView(ActionPermissionMixin, DetailView):
    model = PurchaseReturn
    template_name = "purchases/purchase_return_detail.html"
    required_permission = "purchases.view_purchasereturn"
    context_object_name = "purchase_return"

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related("vendor", "warehouse", "original_bill", "original_receipt")
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = self.object.number
        ctx["page_subtitle"] = f"Return to {self.object.vendor}"
        ctx["lines"] = self.object.lines.select_related("product", "bill_line", "receipt_line")
        ctx["can_edit"] = self.object.status == DocumentStatus.DRAFT
        ctx["audit_events"] = AuditEvent.objects.filter(
            content_type__app_label="purchases",
            content_type__model="purchasereturn",
            object_id=self.object.pk,
        ).select_related("user")[:20]
        return ctx


class PurchaseReturnPostView(ActionPermissionMixin, View):
    required_permission = POST_STOCK_MOVEMENT

    def post(self, request, pk):
        purchase_return = get_object_or_404(PurchaseReturn, pk=pk)
        try:
            services.post_purchase_return(purchase_return, request.user, request)
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            messages.success(request, f"{purchase_return.number} posted — stock returned.")
        return redirect("purchases:pr_detail", pk=pk)


# ---------------------------------------------------------------------------
# Vendor debit note: list, create/edit, detail, post (RET-006, RET-007)
# ---------------------------------------------------------------------------
def _debit_note_initial_from_return(purchase_return):
    """Prefill a new debit note from a posted return, pricing each line off
    whatever bill line it traces back to so the credit matches the original."""
    initial_header = {
        "vendor": purchase_return.vendor_id,
        "original_bill": purchase_return.original_bill_id,
        "purchase_return": purchase_return.pk,
        "currency": purchase_return.vendor.currency_id,
    }
    initial_lines = []
    for line in purchase_return.lines.select_related("product", "bill_line__unit"):
        bill_line = line.bill_line
        initial_lines.append(
            {
                "return_line": line.pk,
                "bill_line": bill_line.pk if bill_line else None,
                "is_stock_line": True,
                "product": line.product_id,
                "unit": bill_line.unit_id if bill_line else line.product.unit_id,
                "tax_code": bill_line.tax_code_id if bill_line else None,
                "quantity": line.quantity,
                "unit_price": bill_line.unit_price if bill_line else line.unit_cost,
            }
        )
    return initial_header, initial_lines


class VendorDebitNoteListView(FilteredListView):
    model = VendorDebitNote
    required_permission = "purchases.view_vendordebitnote"
    page_title = "Vendor debit notes"
    page_subtitle = "Credit owed back to us by a vendor."
    create_url_name = "purchases:dbn_create"
    create_label = "New debit note"
    export_permission = EXPORT_DATA
    export_filename = "vendor-debit-notes"
    default_ordering = "-document_date"

    columns = [
        Column("number", "Number", sortable=True, link=True, css="font-mono text-xs"),
        Column("vendor", "Vendor", sortable=True, order_by="vendor__name"),
        Column("document_date", "Date", sortable=True),
        Column("total_txn", "Total", align="right", money=True, sortable=True),
        Column("open_txn", "Open", align="right", money=True, sortable=True),
        Column("status", "Status", badge=True, align="center"),
    ]
    search_fields = ["number", "vendor__name", "vendor_credit_reference"]
    filters = [ChoiceFilter("status", "Status", DocumentStatus.choices)]

    def get_queryset(self):
        return super().get_queryset().select_related("vendor")

    def get_summary(self):
        totals = VendorDebitNote.objects.aggregate(
            total=Count("id"),
            draft=Count("id", filter=Q(status=DocumentStatus.DRAFT)),
            posted=Count("id", filter=Q(status=DocumentStatus.POSTED)),
        )
        return [
            ("Debit notes", totals["total"]),
            ("Draft", totals["draft"]),
            ("Posted", totals["posted"]),
        ]


class VendorDebitNoteFormView(ActionPermissionMixin, View):
    """Shared GET/POST handling for create and edit."""

    template_name = "purchases/vendor_debit_note_form.html"
    is_create = True

    def get_object(self, pk):
        return get_object_or_404(VendorDebitNote, pk=pk)

    def is_locked(self, note):
        return not self.is_create and note.status != DocumentStatus.DRAFT

    def render_form(self, request, form, formset, note):
        return render(
            request,
            self.template_name,
            {
                "form": form,
                "formset": formset,
                "empty_form": formset.empty_form,
                "products_data": _products_payload(),
                "object": None if self.is_create else note,
                "page_title": "New vendor debit note"
                if self.is_create
                else f"Edit {note.number}",
            },
        )

    def _locked_redirect(self, request, note):
        messages.error(
            request,
            f"{note.number} is {note.get_status_display()} and can no longer be edited.",
        )
        return redirect(note.get_absolute_url())

    def get(self, request, pk=None):
        note = VendorDebitNote() if self.is_create else self.get_object(pk)
        if self.is_locked(note):
            return self._locked_redirect(request, note)

        initial_lines = None
        if self.is_create and request.GET.get("return"):
            source_return = get_object_or_404(PurchaseReturn, pk=request.GET["return"])
            initial_header, initial_lines = _debit_note_initial_from_return(source_return)
            form = VendorDebitNoteForm(instance=note, initial=initial_header)
        else:
            form = VendorDebitNoteForm(instance=note)

        formset = VendorDebitNoteLineFormSet(
            instance=note, prefix="lines", initial=initial_lines or None
        )
        if initial_lines:
            formset.extra = len(initial_lines)
        return self.render_form(request, form, formset, note)

    def post(self, request, pk=None):
        note = VendorDebitNote() if self.is_create else self.get_object(pk)
        if self.is_locked(note):
            return self._locked_redirect(request, note)

        before = None if self.is_create else audit.snapshot(note)
        form = VendorDebitNoteForm(request.POST, instance=note)
        formset = VendorDebitNoteLineFormSet(request.POST, instance=note, prefix="lines")

        if form.is_valid() and formset.is_valid():
            with transaction.atomic():
                note = form.save(commit=False)
                is_new = note.pk is None
                if is_new:
                    note.number = services.allocate_dbn_number(note.document_date)
                    note.created_by = request.user
                note.updated_by = request.user
                note.save()

                instances = formset.save(commit=False)
                for obj in formset.deleted_objects:
                    obj.delete()
                next_line_no = (note.lines.aggregate(Max("line_no"))["line_no__max"] or 0) + 1
                for instance in instances:
                    instance.debit_note = note
                    if instance.pk is None:
                        instance.line_no = next_line_no
                        next_line_no += 1
                    instance.save()

                services.recalculate_debit_note(note)

                if is_new:
                    audit.record_create(request, note)
                    messages.success(request, f"{note.number} created as a draft.")
                else:
                    event = audit.record_update(request, note, before)
                    if event:
                        messages.success(request, f"{note.number} updated.")
                    else:
                        messages.info(request, "No changes to save.")

            return redirect(note.get_absolute_url())

        return self.render_form(request, form, formset, note)


class VendorDebitNoteCreateView(VendorDebitNoteFormView):
    required_permission = "purchases.add_vendordebitnote"
    is_create = True


class VendorDebitNoteEditView(VendorDebitNoteFormView):
    required_permission = "purchases.change_vendordebitnote"
    is_create = False


class VendorDebitNoteDetailView(ActionPermissionMixin, DetailView):
    model = VendorDebitNote
    template_name = "purchases/vendor_debit_note_detail.html"
    required_permission = "purchases.view_vendordebitnote"
    context_object_name = "note"

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related("vendor", "currency", "original_bill", "purchase_return")
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = self.object.number
        ctx["page_subtitle"] = f"Debit note for {self.object.vendor}"
        ctx["lines"] = self.object.lines.select_related(
            "product", "unit", "tax_code", "expense_account"
        )
        ctx["can_edit"] = self.object.status == DocumentStatus.DRAFT
        ctx["audit_events"] = AuditEvent.objects.filter(
            content_type__app_label="purchases",
            content_type__model="vendordebitnote",
            object_id=self.object.pk,
        ).select_related("user")[:20]
        return ctx


class VendorDebitNotePostView(ActionPermissionMixin, View):
    required_permission = POST_DEBIT_NOTE

    def post(self, request, pk):
        note = get_object_or_404(VendorDebitNote, pk=pk)
        try:
            services.post_vendor_debit_note(note, request.user, request)
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            messages.success(request, f"{note.number} posted.")
        return redirect("purchases:dbn_detail", pk=pk)
