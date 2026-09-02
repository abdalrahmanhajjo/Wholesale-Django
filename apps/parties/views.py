"""
Customer and vendor screens (PTY-001..PTY-008).

This module is the worked example for the rest of the team: a list built on
`FilteredListView`, a create/update form that records audit events, and a
detail page. Members 2, 3 and 4 should copy this shape rather than inventing
their own.
"""

from django.contrib import messages
from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views.generic import CreateView, DetailView, UpdateView, View

from apps.core import audit
from apps.core.list_views import BooleanFilter, Column, FilteredListView
from apps.core.mixins import ActionPermissionMixin, AuditedFormMixin
from apps.core.models import AuditEvent
from apps.core.permissions import EXPORT_DATA
from apps.parties.forms import CustomerForm, VendorForm
from apps.parties.models import Customer, Vendor


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------
class CustomerListView(FilteredListView):
    """PTY-001, UX-002, UX-005, UX-007."""

    model = Customer
    required_permission = "parties.view_customer"
    page_title = "Customers"
    page_subtitle = "Everyone you sell to, and their credit position."
    create_url_name = "parties:customer_create"
    create_label = "New customer"
    export_permission = EXPORT_DATA
    export_filename = "customers"
    default_ordering = "code"
    paginate_by = 25

    columns = [
        Column("code", "Code", sortable=True, link=True, css="font-mono text-xs"),
        Column("name", "Name", sortable=True),
        Column("tax_id", "Tax ID", css="font-mono text-xs"),
        Column("currency_id", "Currency", sortable=True, order_by="currency"),
        Column("payment_term", "Terms"),
        Column("credit_limit", "Credit limit", align="right", money=True, sortable=True),
        Column("is_active", "Active", badge=True, align="center"),
    ]

    search_fields = ["code", "name", "legal_name", "tax_id", "email"]
    # PTY-007: a near-miss on the name should still find the record.
    trigram_search_fields = ["name"]

    filters = [
        BooleanFilter("is_active", "Status", true_label="Active", false_label="Inactive"),
        BooleanFilter(
            "credit_hold", "Credit hold", true_label="On hold", false_label="Not on hold"
        ),
    ]

    def get_queryset(self):
        return super().get_queryset().select_related("currency", "payment_term")

    def get_summary(self):
        # One query for the three tiles rather than three.
        totals = Customer.objects.aggregate(
            total=Count("id"),
            active=Count("id", filter=Q(is_active=True)),
            on_hold=Count("id", filter=Q(credit_hold=True)),
        )
        return [
            ("Customers", totals["total"]),
            ("Active", totals["active"]),
            ("On credit hold", totals["on_hold"]),
        ]




class CustomerCreateView(AuditedFormMixin, ActionPermissionMixin, CreateView):
    model = Customer
    form_class = CustomerForm
    template_name = "parties/customer_form.html"
    required_permission = "parties.add_customer"
    extra_context = {"page_title": "New customer"}

    def get_success_url(self):
        return reverse("parties:customer_detail", args=[self.object.pk])


class CustomerUpdateView(AuditedFormMixin, ActionPermissionMixin, UpdateView):
    model = Customer
    form_class = CustomerForm
    template_name = "parties/customer_form.html"
    required_permission = "parties.change_customer"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = f"Edit {self.object.code}"
        return ctx

    def get_success_url(self):
        return reverse("parties:customer_detail", args=[self.object.pk])


class CustomerDetailView(ActionPermissionMixin, DetailView):
    model = Customer
    template_name = "parties/customer_detail.html"
    required_permission = "parties.view_customer"
    context_object_name = "customer"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = self.object.name
        ctx["page_subtitle"] = f"Customer {self.object.code}"
        # ACC-005: "audit history is readable from the related record".
        ctx["audit_events"] = AuditEvent.objects.filter(
            content_type__app_label="parties",
            content_type__model="customer",
            object_id=self.object.pk,
        ).select_related("user")[:20]
        return ctx


class CustomerDeactivateView(ActionPermissionMixin, View):
    """
    PTY-008: deactivate, never delete, once posted transactions reference a
    party. There is deliberately no delete view for customers at all.
    """

    required_permission = "parties.change_customer"

    def post(self, request, pk):
        customer = get_object_or_404(Customer, pk=pk)
        before = audit.snapshot(customer)
        reactivating = not customer.is_active

        with transaction.atomic():
            customer.is_active = reactivating
            customer.deactivated_at = None if reactivating else timezone.now()
            customer.updated_by = request.user
            customer.save(
                update_fields=["is_active", "deactivated_at", "updated_by", "updated_at"]
            )
            audit.record_update(
                request,
                customer,
                before,
                reason=request.POST.get("reason", "").strip(),
            )

        messages.success(
            request, f"{customer} {'reactivated' if reactivating else 'deactivated'}."
        )
        return redirect("parties:customer_detail", pk=pk)


# ---------------------------------------------------------------------------
# Vendors (PTY-002 — the same pattern on the buying side)
# ---------------------------------------------------------------------------
class VendorListView(FilteredListView):
    model = Vendor
    required_permission = "parties.view_vendor"
    page_title = "Vendors"
    page_subtitle = "Everyone you buy from."
    create_url_name = "parties:vendor_create"
    create_label = "New vendor"
    export_permission = EXPORT_DATA
    export_filename = "vendors"
    default_ordering = "code"

    columns = [
        Column("code", "Code", sortable=True, link=True, css="font-mono text-xs"),
        Column("name", "Name", sortable=True),
        Column("tax_id", "Tax ID", css="font-mono text-xs"),
        Column("currency_id", "Currency", sortable=True, order_by="currency"),
        Column("payment_term", "Terms"),
        Column("is_active", "Active", badge=True, align="center"),
    ]
    search_fields = ["code", "name", "legal_name", "tax_id", "email"]
    trigram_search_fields = ["name"]
    filters = [
        BooleanFilter("is_active", "Status", true_label="Active", false_label="Inactive")
    ]

    def get_queryset(self):
        return super().get_queryset().select_related("currency", "payment_term")

    def get_summary(self):
        totals = Vendor.objects.aggregate(
            total=Count("id"), active=Count("id", filter=Q(is_active=True))
        )
        return [("Vendors", totals["total"]), ("Active", totals["active"])]


class VendorCreateView(AuditedFormMixin, ActionPermissionMixin, CreateView):
    model = Vendor
    form_class = VendorForm
    template_name = "parties/customer_form.html"
    required_permission = "parties.add_vendor"
    extra_context = {"page_title": "New vendor", "party_kind": "vendor"}

    def get_success_url(self):
        return reverse("parties:vendor_list")


class VendorUpdateView(AuditedFormMixin, ActionPermissionMixin, UpdateView):
    model = Vendor
    form_class = VendorForm
    template_name = "parties/customer_form.html"
    required_permission = "parties.change_vendor"
    extra_context = {"party_kind": "vendor"}
    success_url = reverse_lazy("parties:vendor_list")
