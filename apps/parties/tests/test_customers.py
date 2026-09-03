"""
Customer screens: the list pattern, duplicate detection and the audit trail.

These also serve as the acceptance tests for the shared list component, since
customers are the first module to use it.

BRD coverage: PTY-001, PTY-004, PTY-005, PTY-007, PTY-008, ACC-005,
UX-002, UX-007.
"""

from decimal import Decimal

from django.contrib.auth.models import Group, Permission
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.core.models import AuditAction, AuditEvent, Currency, PaymentTerm
from apps.parties.models import Address, AddressType, Contact, Customer, Vendor


class CustomerScreenTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.usd = Currency.objects.get(code="USD")
        cls.term = PaymentTerm.objects.get(code="NET30")
        for i in range(1, 31):
            Customer.objects.create(
                code=f"C-{i:04d}",
                name=f"Customer {i}",
                currency=cls.usd,
                payment_term=cls.term,
                credit_limit=Decimal("1000") * i,
                is_active=(i % 5 != 0),
            )
        Customer.objects.create(
            code="ACME-01",
            name="Acme Retail Limited",
            tax_id="TX-999",
            currency=cls.usd,
            credit_limit=Decimal("5000"),
        )

    def setUp(self):
        self.user = User.objects.create_user(
            username="m1", email="m1@example.com", password="testpass-12345"
        )
        self.user.groups.add(Group.objects.get(name="Owner/Admin"))
        self.client.force_login(self.user)

    # -- list, search, filter, sort, paginate (UX-002) ----------------------
    def test_list_renders_and_paginates(self):
        response = self.client.get(reverse("parties:customer_list"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total_count"], 31)
        self.assertEqual(len(response.context["rows"]), 25)  # paginate_by
        self.assertTrue(response.context["is_paginated"])

    def test_second_page(self):
        response = self.client.get(reverse("parties:customer_list"), {"page": 2})
        self.assertEqual(len(response.context["rows"]), 6)

    def test_search_matches_substring(self):
        response = self.client.get(reverse("parties:customer_list"), {"q": "ACME"})
        self.assertEqual(response.context["total_count"], 1)

    def test_search_finds_a_misspelling(self):
        """PTY-007: trigram similarity, not just substring matching."""
        response = self.client.get(reverse("parties:customer_list"), {"q": "Acme Retale"})
        codes = [r["object"].code for r in response.context["rows"]]
        self.assertIn("ACME-01", codes)

    def test_filter_by_active(self):
        response = self.client.get(reverse("parties:customer_list"), {"is_active": "0"})
        self.assertEqual(response.context["total_count"], 6)  # every 5th of 30

    def test_sorting_is_restricted_to_declared_columns(self):
        ok = self.client.get(reverse("parties:customer_list"), {"sort": "-credit_limit"})
        self.assertEqual(ok.context["rows"][0]["object"].code, "C-0030")

        # An undeclared column must be ignored, not passed to the ORM.
        safe = self.client.get(
            reverse("parties:customer_list"), {"sort": "salesperson__password"}
        )
        self.assertEqual(safe.status_code, 200)
        self.assertEqual(safe.context["rows"][0]["object"].code, "ACME-01")  # default order

    def test_filter_state_survives_in_the_querystring(self):
        response = self.client.get(
            reverse("parties:customer_list"), {"q": "Customer", "is_active": "1"}
        )
        self.assertIn("q=Customer", response.context["querystring"])
        self.assertIn("is_active=1", response.context["querystring"])

    # -- export (UX-007) ---------------------------------------------------
    def test_export_matches_the_filtered_list(self):
        response = self.client.get(
            reverse("parties:customer_list"), {"is_active": "0", "export": "csv"}
        )
        self.assertEqual(response["Content-Type"], "text/csv")
        body = response.content.decode()
        rows = [r for r in body.strip().split("\r\n") if r]
        self.assertEqual(len(rows), 7)  # header + 6 inactive

    def test_export_is_recorded(self):
        self.client.get(reverse("parties:customer_list"), {"export": "csv"})
        self.assertTrue(AuditEvent.objects.filter(action=AuditAction.EXPORT).exists())

    def test_export_neutralizes_spreadsheet_formulas(self):
        Customer.objects.create(
            code="CSV-SAFE",
            name='=HYPERLINK("https://malicious.example", "open")',
            currency=self.usd,
        )

        response = self.client.get(
            reverse("parties:customer_list"), {"q": "CSV-SAFE", "export": "csv"}
        )

        self.assertContains(response, "'=HYPERLINK", status_code=200)

    def test_export_requires_permission(self):
        no_export = User.objects.create_user(
            username="noexport", email="n@example.com", password="testpass-12345"
        )
        no_export.groups.add(Group.objects.get(name="Sales"))
        no_export.user_permissions.clear()
        self.client.force_login(no_export)
        # Sales does hold export_data, so strip it to prove the gate works.
        no_export.groups.clear()
        no_export.user_permissions.add(*_view_customer_permissions())
        response = self.client.get(reverse("parties:customer_list"), {"export": "csv"})
        self.assertEqual(response.status_code, 403)

    # -- create / update / audit (ACC-005) ---------------------------------
    def test_create_writes_an_audit_event(self):
        response = self.client.post(
            reverse("parties:customer_create"),
            {
                "code": "NEW-01",
                "name": "Brand New Co",
                "legal_name": "",
                "tax_id": "",
                "email": "",
                "phone": "",
                "website": "",
                "currency": self.usd.pk,
                "payment_term": self.term.pk,
                "credit_limit": "0",
                "credit_hold_reason": "",
                "notes": "",
                "is_active": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        customer = Customer.objects.get(code="NEW-01")
        event = AuditEvent.objects.get(object_id=customer.pk, action=AuditAction.CREATE)
        self.assertEqual(event.user, self.user)
        self.assertIn("created", event.changes)

    def test_update_records_only_what_changed(self):
        customer = Customer.objects.get(code="ACME-01")
        self.client.post(
            reverse("parties:customer_edit", args=[customer.pk]),
            {
                "code": "ACME-01",
                "name": "Acme Retail Limited",
                "legal_name": "",
                "tax_id": "TX-999",
                "email": "",
                "phone": "",
                "website": "",
                "currency": self.usd.pk,
                "payment_term": "",
                "credit_limit": "7500",
                "credit_hold_reason": "",
                "notes": "",
                "is_active": "on",
            },
        )
        event = AuditEvent.objects.filter(
            object_id=customer.pk, action=AuditAction.UPDATE
        ).latest("occurred_at")
        self.assertEqual(set(event.changes), {"credit_limit"})
        self.assertEqual(event.changes["credit_limit"]["from"], "5000.0000")
        self.assertEqual(event.changes["credit_limit"]["to"], "7500.0000")

    # -- duplicate detection (PTY-007) -------------------------------------
    def test_duplicate_code_is_blocked_case_insensitively(self):
        response = self.client.post(
            reverse("parties:customer_create"),
            {
                "code": "acme-01",
                "name": "Something Else",
                "legal_name": "",
                "tax_id": "",
                "email": "",
                "phone": "",
                "website": "",
                "currency": self.usd.pk,
                "payment_term": "",
                "credit_limit": "0",
                "credit_hold_reason": "",
                "notes": "",
                "is_active": "on",
            },
        )
        self.assertEqual(response.status_code, 200)  # redisplayed, not saved
        self.assertFormError(
            response.context["form"],
            "code",
            "Code “acme-01” is already in use by Acme Retail Limited. "
            "Codes must be unique, and case does not make them different.",
        )

    def test_similar_name_is_a_warning_not_a_block(self):
        self.client.post(
            reverse("parties:customer_create"),
            {
                "code": "ACME-02",
                "name": "Acme Retail Ltd",
                "legal_name": "",
                "tax_id": "TX-999",
                "email": "",
                "phone": "",
                "website": "",
                "currency": self.usd.pk,
                "payment_term": "",
                "credit_limit": "0",
                "credit_hold_reason": "",
                "notes": "",
                "is_active": "on",
            },
            follow=True,
        )
        # It saved — a warning must not prevent a legitimate record.
        self.assertTrue(Customer.objects.filter(code="ACME-02").exists())

    def test_credit_hold_requires_a_reason(self):
        response = self.client.post(
            reverse("parties:customer_create"),
            {
                "code": "HOLD-01",
                "name": "On Hold Co",
                "legal_name": "",
                "tax_id": "",
                "email": "",
                "phone": "",
                "website": "",
                "currency": self.usd.pk,
                "payment_term": "",
                "credit_limit": "0",
                "credit_hold": "on",
                "credit_hold_reason": "",
                "notes": "",
                "is_active": "on",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("credit_hold_reason", response.context["form"].errors)

    # -- deactivate, never delete (PTY-008) --------------------------------
    def test_deactivate_sets_the_flag_and_records_the_reason(self):
        customer = Customer.objects.get(code="ACME-01")
        self.client.post(
            reverse("parties:customer_deactivate", args=[customer.pk]),
            {"reason": "Account closed at customer request"},
        )
        customer.refresh_from_db()
        self.assertFalse(customer.is_active)
        self.assertIsNotNone(customer.deactivated_at)
        event = AuditEvent.objects.filter(object_id=customer.pk).latest("occurred_at")
        self.assertEqual(event.reason, "Account closed at customer request")

    def test_there_is_no_delete_route(self):
        from django.urls import NoReverseMatch

        with self.assertRaises(NoReverseMatch):
            reverse("parties:customer_delete", args=[1])

    def test_status_change_requires_an_audit_reason(self):
        customer = Customer.objects.get(code="ACME-01")
        response = self.client.post(
            reverse("parties:customer_deactivate", args=[customer.pk]), {}
        )
        self.assertEqual(response.status_code, 302)
        customer.refresh_from_db()
        self.assertTrue(customer.is_active)

    # -- permissions -------------------------------------------------------
    def test_read_only_user_cannot_create(self):
        auditor = User.objects.create_user(
            username="ro",
            email="ro@example.com",
            password="testpass-12345",
            is_read_only=True,
        )
        auditor.groups.add(Group.objects.get(name="Owner/Admin"))
        self.client.force_login(auditor)
        response = self.client.post(reverse("parties:customer_create"), {})
        self.assertEqual(response.status_code, 403)

    def test_detail_shows_audit_history(self):
        customer = Customer.objects.get(code="ACME-01")
        self.client.post(
            reverse("parties:customer_deactivate", args=[customer.pk]), {"reason": "test"}
        )
        response = self.client.get(reverse("parties:customer_detail", args=[customer.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(len(response.context["audit_events"]), 1)

    def test_customer_urls_enforce_their_declared_permissions(self):
        customer = Customer.objects.get(code="ACME-01")
        no_access = User.objects.create_user(
            username="customer-no-access",
            email="customer-no-access@example.com",
            password="testpass-12345",
        )
        self.client.force_login(no_access)

        requests = [
            ("get", reverse("parties:customer_list")),
            ("get", reverse("parties:customer_detail", args=[customer.pk])),
            ("post", reverse("parties:customer_create")),
            ("post", reverse("parties:customer_edit", args=[customer.pk])),
            ("post", reverse("parties:customer_deactivate", args=[customer.pk])),
        ]
        for method, url in requests:
            with self.subTest(method=method, url=url):
                response = getattr(self.client, method)(url, {})
                self.assertEqual(response.status_code, 403)

    def test_export_permission_does_not_bypass_customer_view_permission(self):
        export_only = User.objects.create_user(
            username="export-only",
            email="export-only@example.com",
            password="testpass-12345",
        )
        export_only.user_permissions.add(Permission.objects.get(codename="export_data"))
        self.client.force_login(export_only)
        response = self.client.get(reverse("parties:customer_list"), {"export": "csv"})
        self.assertEqual(response.status_code, 403)

    def test_vendor_detail_supports_view_only_users(self):
        vendor = Vendor.objects.create(
            code="V-DETAIL",
            name="Detail Vendor",
            currency=self.usd,
            payment_term=self.term,
        )
        viewer = User.objects.create_user(
            username="vendor-viewer",
            email="vendor-viewer@example.com",
            password="testpass-12345",
        )
        viewer.user_permissions.add(
            Permission.objects.get(content_type__app_label="parties", codename="view_vendor")
        )
        self.client.force_login(viewer)

        response = self.client.get(vendor.get_absolute_url())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, vendor.name)
        self.assertNotContains(response, reverse("parties:vendor_edit", args=[vendor.pk]))


            # -- addresses and contacts (PTY-003) ----------------------------------
    def test_a_second_default_address_moves_the_flag(self):
        """
        Ticking "default" means "make this the default", so the previous holder
        loses the flag rather than the save being refused. The database allows
        only one per customer and type, and checks it on insert.
        """
        customer = Customer.objects.get(code="ACME-01")
        first = Address.objects.create(
            customer=customer,
            label="Old depot",
            address_type=AddressType.SHIPPING,
            line1="1 Old Road",
            is_default=True,
        )

        response = self.client.post(
            reverse("parties:customer_address_create", args=[customer.pk]),
            {
                "label": "New depot",
                "address_type": AddressType.SHIPPING,
                "line1": "2 New Road",
                "line2": "",
                "city": "",
                "state": "",
                "postal_code": "",
                "country": "",
                "is_default": "on",
            },
        )

        self.assertEqual(response.status_code, 302)
        first.refresh_from_db()
        self.assertFalse(first.is_default, "the previous default should have been cleared")
        self.assertEqual(
            customer.addresses.filter(
                address_type=AddressType.SHIPPING, is_default=True
            ).count(),
            1,
        )

    def test_a_second_primary_contact_moves_the_flag(self):
        customer = Customer.objects.get(code="ACME-01")
        first = Contact.objects.create(
            customer=customer, name="Old contact", is_primary=True
        )

        response = self.client.post(
            reverse("parties:customer_contact_create", args=[customer.pk]),
            {
                "name": "New contact",
                "job_title": "",
                "email": "",
                "phone": "",
                "is_primary": "on",
            },
        )

        self.assertEqual(response.status_code, 302)
        first.refresh_from_db()
        self.assertFalse(first.is_primary)
        self.assertEqual(customer.contacts.filter(is_primary=True).count(), 1)


def _view_customer_permissions():
    from django.contrib.auth.models import Permission

    return Permission.objects.filter(
        content_type__app_label="parties", codename="view_customer"
    )
