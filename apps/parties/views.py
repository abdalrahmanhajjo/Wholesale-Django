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
from django.urls import reverse
from django.utils import timezone
from django.views.generic import CreateView, DetailView, UpdateView, View

from apps.core import audit
from apps.core.list_views import BooleanFilter, Column, FilteredListView
from apps.core.mixins import ActionPermissionMixin, AuditedFormMixin, BackLinkMixin
from apps.core.models import AuditEvent
from apps.core.permissions import EXPORT_DATA
from apps.parties.forms import CustomerForm, VendorForm, AddressForm, ContactForm
from apps.parties.models import Customer, Vendor, Address, Contact


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
        totals = self.get_queryset().aggregate(
            total=Count("id"),
            active=Count("id", filter=Q(is_active=True)),
            on_hold=Count("id", filter=Q(credit_hold=True)),
        )
        return [
            ("Customers", totals["total"]),
            ("Active", totals["active"]),
            ("On credit hold", totals["on_hold"]),
        ]


class CustomerCreateView(BackLinkMixin, AuditedFormMixin, ActionPermissionMixin, CreateView):
    back_url_name = "parties:customer_list"
    back_label = "Back to customers"
    model = Customer
    form_class = CustomerForm
    template_name = "parties/customer_form.html"
    required_permission = "parties.add_customer"
    extra_context = {"page_title": "New customer"}

    def get_success_url(self):
        return reverse("parties:customer_detail", args=[self.object.pk])


class CustomerUpdateView(BackLinkMixin, AuditedFormMixin, ActionPermissionMixin, UpdateView):
    back_to_object = True
    back_label = "Back to this customer"
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


class CustomerDetailView(BackLinkMixin, ActionPermissionMixin, DetailView):
    back_url_name = "parties:customer_list"
    back_label = "Back to customers"
    model = Customer
    template_name = "parties/customer_detail.html"
    required_permission = "parties.view_customer"
    context_object_name = "customer"

    def get_queryset(self):
        return super().get_queryset().select_related("currency", "payment_term", "salesperson")

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
        reason = request.POST.get("reason", "").strip()
        if not reason:
            messages.error(request, "Give a reason for the customer status change.")
            return redirect("parties:customer_detail", pk=pk)

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
                reason=reason,
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
        totals = self.get_queryset().aggregate(
            total=Count("id"), active=Count("id", filter=Q(is_active=True))
        )
        return [("Vendors", totals["total"]), ("Active", totals["active"])]


class VendorCreateView(BackLinkMixin, AuditedFormMixin, ActionPermissionMixin, CreateView):
    back_url_name = "parties:vendor_list"
    back_label = "Back to vendors"
    model = Vendor
    form_class = VendorForm
    template_name = "parties/customer_form.html"
    required_permission = "parties.add_vendor"
    extra_context = {"page_title": "New vendor", "party_kind": "vendor"}

    def get_success_url(self):
        return reverse("parties:vendor_detail", args=[self.object.pk])


class VendorDetailView(BackLinkMixin, ActionPermissionMixin, DetailView):
    back_url_name = "parties:vendor_list"
    back_label = "Back to vendors"
    model = Vendor
    template_name = "parties/vendor_detail.html"
    required_permission = "parties.view_vendor"
    context_object_name = "vendor"

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related(
                "currency",
                "payment_term",
                "payable_account",
                "advance_account",
                "default_tax_code",
                "default_expense_account",
            )
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            page_title=self.object.name,
            page_subtitle=f"Vendor {self.object.code}",
            audit_events=AuditEvent.objects.filter(
                content_type__app_label="parties",
                content_type__model="vendor",
                object_id=self.object.pk,
            ).select_related("user")[:20],
        )
        return context


class VendorUpdateView(BackLinkMixin, AuditedFormMixin, ActionPermissionMixin, UpdateView):
    back_to_object = True
    back_label = "Back to this vendor"
    model = Vendor
    form_class = VendorForm
    template_name = "parties/customer_form.html"
    required_permission = "parties.change_vendor"
    extra_context = {"party_kind": "vendor"}

    def get_success_url(self):
        return reverse("parties:vendor_detail", args=[self.object.pk])


class PartyChildMixin(AuditedFormMixin, ActionPermissionMixin):
    """
    Shared behaviour for the address and contact forms: set the owning party,
    keep the "only one default" rule true, and audit the change.
    """

    template_name = "core/settings_form.html"
    required_permission = "parties.change_customer"
    #: Field that marks the default row, and the fields it is unique within.
    flag_field = ""
    scope_fields = ()

    def get_customer(self):
        if "customer_pk" in self.kwargs:
            return get_object_or_404(Customer, pk=self.kwargs["customer_pk"])
        return self.object.customer if self.object else None

    def clear_previous_flag(self, instance):
        """
        Only one default per party (and per address type) is allowed, and the
        constraint is checked on INSERT — so the previous holder must lose the
        flag *before* the new row is written, not after.
        """
        if not getattr(instance, self.flag_field, False):
            return
        siblings = self.model.objects.filter(customer=instance.customer)
        if instance.pk:
            siblings = siblings.exclude(pk=instance.pk)
        for field in self.scope_fields:
            siblings = siblings.filter(**{field: getattr(instance, field)})
        siblings.filter(**{self.flag_field: True}).update(**{self.flag_field: False})

    def form_valid(self, form):
        form.instance.customer = self.get_customer()
        form.instance.vendor = None
        with transaction.atomic():
            self.clear_previous_flag(form.instance)
            return super().form_valid(form)

    def get_success_url(self):
        return reverse("parties:customer_detail", args=[self.object.customer_id])


class CustomerAddressCreateView(PartyChildMixin, CreateView):
    model = Address
    form_class = AddressForm
    flag_field = "is_default"
    scope_fields = ("address_type",)
    extra_context = {"page_title": "New address"}


class AddressUpdateView(PartyChildMixin, UpdateView):
    model = Address
    form_class = AddressForm
    flag_field = "is_default"
    scope_fields = ("address_type",)
    extra_context = {"page_title": "Edit address"}


class CustomerContactCreateView(PartyChildMixin, CreateView):
    model = Contact
    form_class = ContactForm
    flag_field = "is_primary"
    extra_context = {"page_title": "New contact"}


class ContactUpdateView(PartyChildMixin, UpdateView):
    model = Contact
    form_class = ContactForm
    flag_field = "is_primary"
    extra_context = {"page_title": "Edit contact"}


class PartyChildDeleteView(ActionPermissionMixin, View):
    """
    POST-only. Addresses and contacts may be deleted — documents snapshot the
    address text at posting, so removing one never rewrites history (PTY-003).
    """

    model = None
    required_permission = "parties.change_customer"

    def post(self, request, pk):
        obj = get_object_or_404(self.model, pk=pk)
        customer_id = obj.customer_id
        with transaction.atomic():
            audit.record_delete(request, obj)
            obj.delete()
        messages.success(request, f"{obj} removed.")
        return redirect("parties:customer_detail", pk=customer_id)


class AddressDeleteView(PartyChildDeleteView):
    model = Address


class ContactDeleteView(PartyChildDeleteView):
    model = Contact

