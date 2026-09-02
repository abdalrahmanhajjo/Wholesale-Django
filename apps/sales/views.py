"""
Sales-order screens (SAL-001..SAL-004).

Follows the parties/views.py worked example exactly:
  - FilteredListView for the list
  - AuditedFormMixin + CreateView/UpdateView for entry
  - ConfirmationRequiredMixin for approve/reject
"""

from decimal import Decimal, InvalidOperation

from django import forms
from django.contrib import messages
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.generic import (
    CreateView,
    DetailView,
    TemplateView,
    UpdateView,
    View,
)

from apps.core import audit
from apps.core.context import company
from apps.core.list_views import ChoiceFilter, Column, DateRangeFilter, FilteredListView
from apps.core.mixins import ActionPermissionMixin, BackLinkMixin, ConfirmationRequiredMixin
from apps.core.models import (
    EDITABLE_STATES,
    ZERO,
    AuditEvent,
    Company,
    DocumentStatus,
    TaxCode,
)
from apps.core.permissions import (
    APPROVE_SALES_ORDER,
    EXPORT_DATA,
    POST_DELIVERY,
    POST_SALES_INVOICE,
)
from apps.inventory.models import DeliveryNote
from apps.ledger.services import PostingError
from apps.sales import services
from apps.sales.forms import (
    DeliveryLineFormSet,
    DeliveryNoteForm,
    SalesOrderForm,
    SalesOrderLineFormSet,
)
from apps.sales.models import SalesInvoice, SalesOrder


def _tax_rate_map():
    """JS-serializable map of TaxCode pk → {rate, inclusive} for live previews."""
    return {
        tc.pk: {"rate": str(tc.rate_percent), "inclusive": bool(tc.is_inclusive)}
        for tc in TaxCode.objects.filter(is_active=True)
    }


def _product_map():
    """
    JS-serializable map of Product pk → {sales_price, tax_code, unit, name}.
    Used to auto-fill a sales-order line when its product is chosen (UX).
    """
    from apps.catalog.models import Product

    return {
        p.pk: {
            "price": str(p.sales_price),
            "tax_code": p.default_sales_tax_code_id,
            "unit": p.unit_id,
            "name": p.name,
        }
        for p in Product.objects.filter(is_active=True)
    }


def _number_lines(formset):
    """
    Assign line_no to every NEW line in the formset; existing lines keep the
    number they were saved with. New lines are numbered after the highest
    existing line_no, so the (order, line_no) unique key can never collide.
    """
    highest = 0
    for lf in formset.forms:
        if lf.instance.pk and lf.instance.line_no:
            highest = max(highest, lf.instance.line_no)

    n = highest + 1
    for lf in formset.forms:
        if not lf.is_valid() or lf.cleaned_data.get("DELETE"):
            continue
        if lf.instance.pk:
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
        totals = SalesOrder.objects.aggregate(
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
class SalesOrderCreateView(BackLinkMixin, ActionPermissionMixin, CreateView):
    back_url_name = "sales:so_list"
    back_label = "Back to sales orders"
    model = SalesOrder
    form_class = SalesOrderForm
    template_name = "sales/so_form.html"
    required_permission = "sales.add_salesorder"
    extra_context = {"page_title": "New sales order"}

    def get_initial(self):
        """
        Start on the company's base currency.

        It is the answer on almost every order and the system already knows it;
        leaving the field empty only produces a "this field is required" on
        submit for a value nobody had to think about. Set here rather than on
        the form so constructing a form still touches no database — and the
        company row is already cached for the page shell.
        """
        initial = super().get_initial()
        initial.setdefault("currency", company(self.request).get("base_currency") or "")
        return initial

    def get_formset(self):
        return SalesOrderLineFormSet(
            self.request.POST or None,
            instance=self.object,
            prefix="lines",
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["line_formset"] = self.get_formset()
        ctx["tax_rates"] = _tax_rate_map()
        ctx["product_map"] = _product_map()
        ctx["page_subtitle"] = "Create a new order for a customer."
        return ctx

    def form_valid(self, form):
        """
        Nothing is written until the lines are valid.

        Saving the header first and warning about the lines afterwards leaves
        two problems behind. A document number is drawn from a controlled
        sequence (CFG-008, NFR-008), so a rejected submit burns one and puts a
        gap in the numbering, which is exactly the thing an auditor asks about.
        And the order it leaves in the database has no lines, so it is not a
        document anyone can act on. Returning early inside `atomic()` does not
        undo either: only an exception rolls a block back, and this path raises
        nothing.
        """
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

            # Full recalculation (SAL-002)
            services.recalculate_order(self.object)

            audit.record_create(self.request, self.object)
            messages.success(
                self.request,
                f"Sales order {self.object.number} created.",
            )

        return redirect(self.get_success_url())

    def get_success_url(self):
        return reverse("sales:so_detail", args=[self.object.pk])


class SalesOrderUpdateView(BackLinkMixin, ActionPermissionMixin, UpdateView):
    back_to_object = True
    back_label = "Back to this order"
    model = SalesOrder
    form_class = SalesOrderForm
    template_name = "sales/so_form.html"
    required_permission = "sales.change_salesorder"

    def get_queryset(self):
        """
        Posted, completed and cancelled documents are immutable (BR-004).

        Filtering the queryset rather than checking in dispatch means the guard
        covers GET and POST from one place: a non-editable order is not found,
        so it can neither be opened nor submitted to. Losing this let an
        approved order be reopened and edited, which is the rule the whole
        double-entry model rests on.
        """
        return super().get_queryset().filter(status__in=EDITABLE_STATES)

    def get_formset(self):
        return SalesOrderLineFormSet(
            self.request.POST or None,
            instance=self.object,
            prefix="lines",
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["line_formset"] = self.get_formset()
        ctx["tax_rates"] = _tax_rate_map()
        ctx["product_map"] = _product_map()
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

            # Only recalculate if status is editable
            if self.object.status in ["DRAFT", "SUBMITTED", "REJECTED"]:
                services.recalculate_order(self.object)

            event = audit.record_update(self.request, self.object, before)
            line_changes = len(formset.new_objects) + len(formset.changed_objects)

            if event or line_changes:
                detail = ", ".join(event.changes.keys()) if event else ""
                if line_changes:
                    suffix = "s" if line_changes != 1 else ""
                    detail = (
                        f"{detail + ', ' if detail else ''}" f"{line_changes} line{suffix}"
                    )
                messages.success(
                    self.request,
                    f"{self.object.number} updated ({detail}).",
                )
            else:
                messages.info(self.request, "No changes to save.")

        return redirect(self.get_success_url())

    def get_success_url(self):
        return reverse("sales:so_detail", args=[self.object.pk])


# ---------------------------------------------------------------------------
# Detail view
# ---------------------------------------------------------------------------
class SalesOrderDetailView(BackLinkMixin, ActionPermissionMixin, DetailView):
    back_url_name = "sales:so_list"
    back_label = "Back to sales orders"
    model = SalesOrder
    template_name = "sales/so_detail.html"
    required_permission = "sales.view_salesorder"
    context_object_name = "order"

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


# ---------------------------------------------------------------------------
# Delivery notes (SAL-005, INV-007)
# ---------------------------------------------------------------------------
class DeliveryNoteListView(FilteredListView):
    model = DeliveryNote
    page_title = "Delivery notes"
    page_subtitle = "Goods shipped against approved sales orders."
    create_url_name = "sales:delivery_create"
    create_label = "New delivery note"
    export_permission = EXPORT_DATA
    export_filename = "delivery_notes"
    default_ordering = "-document_date"
    paginate_by = 25

    columns = [
        Column("number", "Number", sortable=True, link=True, css="font-mono text-xs"),
        Column("customer", "Customer", sortable=True, order_by="customer__name"),
        Column("sales_order", "Sales order", order_by="sales_order__number"),
        Column("document_date", "Date", sortable=True),
        Column("warehouse", "Warehouse", order_by="warehouse__code"),
        Column("status", "Status", badge=True, align="center"),
        Column("total_cost_base", "Total cost", align="right", money=True, sortable=True),
    ]

    search_fields = [
        "number",
        "customer__name",
        "customer__code",
        "sales_order__number",
        "carrier",
        "tracking_reference",
    ]
    trigram_search_fields = ["customer__name"]

    filters = [
        ChoiceFilter("status", "Status", list(DocumentStatus.choices)),
        DateRangeFilter("document_date", "Date range"),
    ]

    def get_queryset(self):
        return super().get_queryset().select_related("customer", "warehouse", "sales_order")

    def get_summary(self):
        agg = DeliveryNote.objects.aggregate(
            draft=Count("id", filter=Q(status="DRAFT")),
            posted=Count("id", filter=Q(status="POSTED")),
            completed=Count("id", filter=Q(status="COMPLETED")),
        )
        return [
            ("Draft", agg["draft"] or 0),
            ("Posted", agg["posted"] or 0),
            ("Completed", agg["completed"] or 0),
        ]


class DeliveryOrderSelectForm(forms.Form):
    """First step of creating a delivery note: pick an approved order."""

    order = forms.ModelChoiceField(
        queryset=SalesOrder.objects.none(),
        label="Approved sales order",
        empty_label="Select an order…",
        widget=forms.Select(attrs={"class": "field"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["order"].queryset = (
            SalesOrder.objects.filter(
                status__in=[DocumentStatus.APPROVED, DocumentStatus.PARTIAL]
            )
            .select_related("customer", "warehouse")
            .order_by("-document_date")
        )


class DeliveryNoteCreateView(BackLinkMixin, ActionPermissionMixin, TemplateView):
    """
    Create a delivery note against an approved sales order.

    GET without `?order=` shows the approved-order picker. GET with `?order=`
    renders the header form plus the order's remaining lines with quantities
    pre-filled from what still needs delivering. POST validates, creates and
    posts the note (SAL-005), then redirects to its detail.
    """

    back_url_name = "sales:delivery_list"
    back_label = "Back to delivery notes"

    template_name = "sales/delivery_note_form.html"
    required_permission = "inventory.add_deliverynote"

    def get_order(self):
        pk = self.request.GET.get("order") or self.request.POST.get("order")
        if not pk:
            return None
        order = get_object_or_404(SalesOrder, pk=pk)
        if order.status not in (DocumentStatus.APPROVED, DocumentStatus.PARTIAL):
            return None
        return order

    def get_initial(self, order):
        return {
            "customer": order.customer_id,
            "sales_order": order.pk,
            "warehouse": order.warehouse_id,
            "document_date": timezone.localdate(),
            "shipping_address_text": order.shipping_address_text,
        }

    def build_formset(self, order):
        rows = services.build_delivery_lines(order)
        formset = DeliveryLineFormSet(
            self.request.POST or None,
            initial=[
                {"sales_order_line": ln.pk, "quantity": remaining} for ln, remaining in rows
            ],
            prefix="lines",
        )
        for form, (ln, remaining) in zip(formset.forms, rows):
            form.so_line = ln
            form.remaining = remaining
        return formset

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        order = self.get_order()
        if order is None:
            ctx["order_picker"] = DeliveryOrderSelectForm()
            ctx["step"] = "pick"
            ctx["page_title"] = "New delivery note"
            ctx["page_subtitle"] = "Start from an approved sales order."
            return ctx

        ctx["order"] = order
        ctx["step"] = "create"
        ctx["page_title"] = f"Deliver {order.number}"
        ctx["page_subtitle"] = f"{order.customer} · {order.get_status_display()}"

        header = kwargs.get("header_form")
        if header is None:
            header = DeliveryNoteForm(initial=self.get_initial(order))
        ctx["header_form"] = header

        formset = kwargs.get("line_formset")
        if formset is None:
            formset = self.build_formset(order)
        ctx["line_formset"] = formset
        return ctx

    def get(self, request, *args, **kwargs):
        if request.GET.get("order") and self.get_order() is not None:
            return self.render_to_response(self.get_context_data())
        # No valid order selected yet: show the picker.
        ctx = self.get_context_data()
        return self.render_to_response(ctx)

    def post(self, request, *args, **kwargs):
        order = self.get_order()
        if order is None:
            messages.error(request, "Choose an approved sales order to deliver.")
            return redirect("sales:delivery_create")

        header = DeliveryNoteForm(request.POST)
        header.instance.customer = order.customer
        header.instance.sales_order = order

        rows = services.build_delivery_lines(order)
        formset = DeliveryLineFormSet(request.POST, prefix="lines")
        for form, (ln, remaining) in zip(formset.forms, rows):
            form.so_line = ln
            form.remaining = remaining

        if not header.is_valid() or not formset.is_valid():
            return self.render_to_response(
                self.get_context_data(header_form=header, line_formset=formset)
            )

        quantities = {
            int(form.cleaned_data["sales_order_line"]): form.cleaned_data["quantity"]
            for form in formset.forms
            if form.cleaned_data and form.cleaned_data.get("quantity") is not None
        }
        if not quantities:
            messages.error(request, "Choose at least one line quantity to deliver.")
            return self.render_to_response(
                self.get_context_data(header_form=header, line_formset=formset)
            )

        try:
            note = services.draft_delivery_from_order(
                order=order,
                user=request.user,
                quantities=quantities,
                warehouse=header.cleaned_data.get("warehouse"),
                document_date=header.cleaned_data.get("document_date"),
                reference=header.cleaned_data.get("reference", ""),
                notes=header.cleaned_data.get("notes", ""),
                carrier=header.cleaned_data.get("carrier", ""),
                tracking_reference=header.cleaned_data.get("tracking_reference", ""),
                shipping_address_text=header.cleaned_data.get("shipping_address_text", ""),
            )
            services.post_delivery(note, request.user)
            messages.success(
                request,
                f"Delivery note {note.number} created and posted.",
            )
            return redirect("sales:delivery_detail", pk=note.pk)
        except ValueError as exc:
            messages.error(request, str(exc))
            return self.render_to_response(
                self.get_context_data(header_form=header, line_formset=formset)
            )


class DeliveryNoteDetailView(BackLinkMixin, ActionPermissionMixin, DetailView):
    back_url_name = "sales:delivery_list"
    back_label = "Back to delivery notes"
    model = DeliveryNote
    template_name = "sales/delivery_note_detail.html"
    context_object_name = "note"
    required_permission = "inventory.view_deliverynote"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = self.object.number
        ctx["page_subtitle"] = f"Delivery for {self.object.customer}"
        ctx["audit_events"] = AuditEvent.objects.filter(
            content_type__app_label="inventory",
            content_type__model="deliverynote",
            object_id=self.object.pk,
        ).select_related("user")[:20]
        return ctx


class DeliveryNotePostView(ActionPermissionMixin, View):
    """
    Post a DRAFT delivery note (SAL-005). Requires the post_delivery permission
    (WAREHOUSE role). Order counters update here; stock movement rows go
    through Member 2's Day 5 engine via the services seam.
    """

    required_permission = POST_DELIVERY

    def post(self, request, pk):
        note = get_object_or_404(DeliveryNote, pk=pk)
        try:
            services.post_delivery(note, request.user)
            messages.success(
                request,
                f"Delivery note {note.number} posted.",
            )
        except ValueError as exc:
            messages.error(request, str(exc))
        return redirect("sales:delivery_detail", pk=pk)


# ---------------------------------------------------------------------------
# Sales invoices (SAL-006..SAL-011)
# ---------------------------------------------------------------------------
class SalesInvoiceListView(FilteredListView):
    model = SalesInvoice
    required_permission = "sales.view_salesinvoice"
    page_title = "Sales invoices"
    page_subtitle = "Amounts billed to customers and posted to the ledger."
    create_url_name = "sales:invoice_create"
    create_label = "New sales invoice"
    export_permission = EXPORT_DATA
    export_filename = "sales_invoices"
    default_ordering = "-document_date"
    paginate_by = 25

    columns = [
        Column("number", "Number", sortable=True, link=True, css="font-mono text-xs"),
        Column("customer", "Customer", sortable=True, order_by="customer__name"),
        Column("document_date", "Date", sortable=True),
        Column("status", "Status", badge=True, align="center"),
        Column("total_txn", "Total", align="right", money=True, sortable=True),
        Column("open_txn", "Open", align="right", money=True, sortable=True),
    ]

    search_fields = [
        "number",
        "customer__name",
        "customer__code",
        "customer_reference",
    ]
    trigram_search_fields = ["customer__name"]

    filters = [
        ChoiceFilter("status", "Status", list(DocumentStatus.choices)),
        DateRangeFilter("document_date", "Date range"),
    ]

    def get_queryset(self):
        return super().get_queryset().select_related("customer")

    def get_summary(self):
        agg = SalesInvoice.objects.aggregate(
            draft=Count("id", filter=Q(status="DRAFT")),
            submitted=Count("id", filter=Q(status="SUBMITTED")),
            open_value=Sum(
                "total_txn",
                filter=Q(status__in=["DRAFT", "SUBMITTED", "POSTED", "PARTIAL"]),
            ),
        )
        return [
            ("Draft", agg["draft"] or 0),
            ("Submitted", agg["submitted"] or 0),
            ("Open value", f"${agg['open_value'] or 0:,.2f}"),
        ]


class SalesInvoiceCreateView(BackLinkMixin, ActionPermissionMixin, TemplateView):
    """
    Create a sales invoice (SAL-006).

    GET without `?delivery=` shows a picker of POSTED delivery notes that still
    have quantity left to invoice. GET with `?delivery=` renders those lines
    with quantities pre-filled from what remains. POST validates the quantities
    again and drafts the invoice (DRAFT — submit and post are separate steps),
    then redirects to its detail.
    """

    back_url_name = "sales:invoice_list"
    back_label = "Back to invoices"

    template_name = "sales/invoice_form.html"
    required_permission = "sales.add_salesinvoice"

    def get_pickable_deliveries(self):
        candidates = []
        for delivery in (
            DeliveryNote.objects.filter(status=DocumentStatus.POSTED)
            .select_related("customer")
            .order_by("-document_date")
        ):
            if services.build_invoice_lines_from_delivery(delivery):
                candidates.append(delivery)
        return candidates

    def get_delivery(self):
        pk = self.request.GET.get("delivery") or self.request.POST.get("delivery")
        if not pk:
            return None
        delivery = get_object_or_404(DeliveryNote, pk=pk)
        if delivery.status != DocumentStatus.POSTED:
            return None
        return delivery

    def get_initial_rows(self, delivery):
        return services.build_invoice_lines_from_delivery(delivery)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        delivery = self.get_delivery()
        if delivery is None:
            ctx["delivery_choices"] = self.get_pickable_deliveries()
            ctx["page_title"] = "New sales invoice"
            ctx["page_subtitle"] = "Choose the posted delivery note to invoice."
            return ctx

        rows = kwargs.get("rows")
        if rows is None:
            rows = self.get_initial_rows(delivery)
        ctx["delivery"] = delivery
        ctx["rows"] = rows
        ctx["page_title"] = f"Invoice {delivery.number}"
        ctx["page_subtitle"] = f"{delivery.customer} · {delivery.document_date}"
        return ctx

    def get(self, request, *args, **kwargs):
        if request.GET.get("delivery") and self.get_delivery() is not None:
            return self.render_to_response(self.get_context_data())
        return super().get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        delivery = self.get_delivery()
        if delivery is None:
            messages.error(request, "Choose a posted delivery note to invoice.")
            return redirect("sales:invoice_create")

        rows = services.build_invoice_lines_from_delivery(delivery)
        quantities = {}
        for dl, _remaining in rows:
            raw = request.POST.get(f"qty_{dl.pk}")
            if raw:
                try:
                    qty = Decimal(raw)
                except InvalidOperation:
                    qty = ZERO
                if qty > ZERO:
                    quantities[dl.pk] = qty

        if not quantities:
            messages.error(request, "Choose at least one quantity to invoice.")
            return self.render_to_response(self.get_context_data(rows=rows))

        try:
            invoice = services.create_invoice_from_delivery(
                delivery=delivery,
                user=request.user,
                quantities=quantities,
            )
            messages.success(request, f"Invoice {invoice.number} drafted.")
            return redirect("sales:invoice_detail", pk=invoice.pk)
        except ValueError as exc:
            messages.error(request, str(exc))
            return self.render_to_response(self.get_context_data(rows=rows))


class SalesInvoiceDetailView(BackLinkMixin, ActionPermissionMixin, DetailView):
    back_url_name = "sales:invoice_list"
    back_label = "Back to invoices"
    model = SalesInvoice
    template_name = "sales/invoice_detail.html"
    context_object_name = "invoice"
    required_permission = "sales.view_salesinvoice"

    def get_queryset(self):
        return super().get_queryset().select_related("customer", "journal_entry")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = self.object.number
        ctx["page_subtitle"] = f"Invoice for {self.object.customer}"
        ctx["source_delivery"] = (
            DeliveryNote.objects.filter(lines__invoice_lines__invoice=self.object)
            .distinct()
            .first()
        )
        ctx["audit_events"] = AuditEvent.objects.filter(
            content_type__app_label="sales",
            content_type__model="salesinvoice",
            object_id=self.object.pk,
        ).select_related("user")[:20]
        return ctx


class SalesInvoiceSubmitView(ActionPermissionMixin, View):
    """SAL-007: DRAFT -> SUBMITTED, ready for posting."""

    required_permission = "sales.change_salesinvoice"

    def post(self, request, pk):
        invoice = get_object_or_404(SalesInvoice, pk=pk)
        try:
            services.submit_invoice(invoice, request.user)
            messages.success(request, f"Invoice {invoice.number} submitted.")
        except ValueError as exc:
            messages.error(request, str(exc))
        return redirect("sales:invoice_detail", pk=pk)


class SalesInvoicePostView(ActionPermissionMixin, View):
    """
    POST a SUBMITTED invoice through the ledger posting engine (SAL-009).
    Gated on `core.post_sales_invoice` (ACCOUNTANT by default). The engine is
    Member 4's; binding to the Day-1 stub makes a missing engine fail loudly
    (PostingError) instead of silently skipping the journal.
    """

    required_permission = POST_SALES_INVOICE

    def post(self, request, pk):
        invoice = get_object_or_404(SalesInvoice, pk=pk)
        try:
            services.post_invoice(invoice, request.user)
            messages.success(
                request,
                f"Invoice {invoice.number} posted to the ledger.",
            )
        except (ValueError, PostingError) as exc:
            messages.error(request, str(exc))
        return redirect("sales:invoice_detail", pk=pk)


class SalesInvoicePrintView(BackLinkMixin, ActionPermissionMixin, TemplateView):
    """Printable invoice (PTY-003) — snapshots only, no dynamic values."""

    back_url_name = "sales:invoice_list"
    back_label = "Back to invoices"

    template_name = "sales/invoice_print.html"
    required_permission = "sales.view_salesinvoice"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["invoice"] = get_object_or_404(
            SalesInvoice.objects.select_related("customer"), pk=self.kwargs["pk"]
        )
        ctx["company"] = Company.objects.select_related("base_currency").first()
        return ctx
