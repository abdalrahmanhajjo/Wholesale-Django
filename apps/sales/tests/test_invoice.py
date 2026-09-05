"""
Tests for Day 4 + Day 5 sales-invoice services (SAL-006..SAL-012).

Covers numbering, remaining-to-invoice, draft creation from a posted
delivery, totals recalculation, submit, the CFG-007 account resolution, the
balanced journal draft, the Day-5 required_mappings contract, and an
end-to-end post against the real PostingEngine (Member 4's Day-2 engine),
including BR-005 atomicity and GL-002 idempotency.

Run:  python manage.py test apps.sales.tests.test_invoice --keepdb
"""

from datetime import date
from decimal import Decimal

from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse

from apps.core.audit import AuditEvent
from apps.core.models import (
    DocumentSequence,
    DocumentStatus,
    FiscalPeriod,
    FiscalYear,
)
from apps.ledger.models import Account, AccountMapping, JournalEntry, MappingKey
from apps.ledger.services import PostingError
from apps.sales import services
from apps.sales.models import SalesInvoice
from apps.sales.tests.factories import (
    make_company,
    make_customer,
    make_line,
    make_order,
    make_product,
    make_sequence,
    make_tax,
    make_user,
    make_warehouse,
    seed_stock,
)

ZERO = Decimal("0")


def _ensure_account_mappings():
    """CFG-007 rows used by the sales journal. Defensive: the seed migration
    already creates them; this keeps the tests self-contained if that changes.
    """
    specs = {
        MappingKey.ACCOUNTS_RECEIVABLE: (
            "1210",
            "ASSET",
            "CURRENT_ASSET",
            "DEBIT",
            True,
            "AR",
        ),
        MappingKey.SALES_REVENUE: ("4100", "INCOME", "REVENUE", "CREDIT", False, ""),
        MappingKey.OUTPUT_TAX: (
            "2310",
            "LIABILITY",
            "CURRENT_LIABILITY",
            "CREDIT",
            True,
            "OUTPUT_TAX",
        ),
        MappingKey.INVENTORY: ("1310", "ASSET", "CURRENT_ASSET", "DEBIT", True, "INVENTORY"),
        MappingKey.COGS: ("5010", "EXPENSE", "COGS", "DEBIT", False, ""),
        MappingKey.ROUNDING_GAIN: ("4820", "INCOME", "OTHER_INCOME", "CREDIT", False, ""),
        MappingKey.ROUNDING_LOSS: ("6920", "EXPENSE", "OTHER_EXPENSE", "DEBIT", False, ""),
    }
    for key, (code, atype, subtype, nb, control, ctype) in specs.items():
        account, _ = Account.objects.get_or_create(
            code=code,
            defaults=dict(
                name=f"{key} account",
                account_type=atype,
                subtype=subtype,
                normal_balance=nb,
                is_control=control,
                control_type=ctype,
                is_postable=True,
                is_active=True,
            ),
        )
        AccountMapping.objects.get_or_create(key=key, defaults={"account": account})


class InvoiceServicesTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        make_sequence("SO", prefix="SO-")
        make_sequence("DN", prefix="DN-")
        make_sequence("SI", prefix="INV-")
        _ensure_account_mappings()
        cls._ensure_open_posting_period()
        make_company()
        cls.warehouse = make_warehouse("WH-INV1")
        cls.customer = make_customer("INV-C1")
        cls.product_a = make_product(sku="INV-A", price=Decimal("100"))
        cls.product_b = make_product(sku="INV-B", price=Decimal("250"))
        cls.user = make_user("invoice-user")
        seed_stock(
            cls.product_a, cls.warehouse, Decimal("100"), Decimal("50"), cls.user, "SEED-INV-A"
        )
        seed_stock(
            cls.product_b, cls.warehouse, Decimal("100"), Decimal("80"), cls.user, "SEED-INV-B"
        )

    @classmethod
    def _ensure_open_posting_period(cls):
        """Open fiscal period + journal-entry sequence for the real PostingEngine
        (entry_date = invoice.posting_date = timezone.localdate()). Reuses an
        existing open period if one already covers today (avoids creating a
        competing fiscal year, which violates the global no-overlap constraint)."""
        make_sequence("JE", prefix="JV-")
        today = date.today()
        if FiscalPeriod.objects.filter(
            start_date__lte=today, end_date__gte=today, status="OPEN"
        ).exists():
            return
        fiscal_year = (
            FiscalYear.objects.filter(start_date__lte=today, end_date__gte=today)
            .order_by("pk")
            .first()
        )
        if fiscal_year is None:
            fiscal_year, _ = FiscalYear.objects.get_or_create(
                code=f"INV-FY-{today.year}",
                defaults=dict(
                    start_date=date(today.year, 1, 1),
                    end_date=date(today.year, 12, 31),
                ),
            )
        FiscalPeriod.objects.get_or_create(
            fiscal_year=fiscal_year,
            period_no=9,
            defaults=dict(
                name="INV-OPEN",
                start_date=date(today.year, 9, 1),
                end_date=date(today.year, 9, 30),
            ),
        )

    def _make_approved_order(self, customer=None, warehouse=None):
        order = make_order(
            customer=customer or self.customer, warehouse=warehouse or self.warehouse
        )
        make_line(
            order, product=self.product_a, qty=Decimal("10"), price=Decimal("100"), line_no=1
        )
        make_line(
            order, product=self.product_b, qty=Decimal("4"), price=Decimal("250"), line_no=2
        )
        order.status = DocumentStatus.SUBMITTED
        order.save(update_fields=["status"])
        order.status = DocumentStatus.APPROVED
        order.approved_at = "2026-08-15T10:00:00Z"
        order.save(update_fields=["status", "approved_at"])
        return order

    def _make_posted_delivery(self, full=True):
        order = self._make_approved_order()
        quantities = {}
        for ln in order.lines.all():
            quantities[ln.pk] = ln.quantity if full else (ln.quantity / 2)
        note = services.draft_delivery_from_order(
            order=order, user=self.user, quantities=quantities
        )
        services.post_delivery(note, self.user)
        return note

    def _make_invoice(self, note):
        quantities = {
            dl.pk: dl.quantity
            for dl in note.lines.select_related("product")
            if services.remaining_to_invoice(dl) > ZERO
        }
        return services.create_invoice_from_delivery(
            delivery=note, user=self.user, quantities=quantities
        )

    # ------------------------------------------------------------------
    # allocate_invoice_number
    # ------------------------------------------------------------------
    def test_allocate_invoice_number(self):
        number = services.allocate_invoice_number()
        self.assertTrue(number.startswith("INV-"))
        self.assertEqual(len(number), 9)  # INV- + 5 digits

    def test_allocate_invoice_number_raises_without_sequence(self):
        DocumentSequence.objects.filter(document_type="SI").delete()
        with self.assertRaises(ValueError) as ctx:
            services.allocate_invoice_number()
        self.assertIn("SI", str(ctx.exception))

    # ------------------------------------------------------------------
    # remaining_to_invoice / build_invoice_lines_from_delivery
    # ------------------------------------------------------------------
    def test_remaining_before_invoice(self):
        note = self._make_posted_delivery()
        dl = note.lines.get(line_no=1)
        self.assertEqual(services.remaining_to_invoice(dl), Decimal("10"))

    def test_remaining_after_partial_invoice(self):
        note = self._make_posted_delivery()
        dl = note.lines.get(line_no=1)
        dl.quantity_invoiced = Decimal("3")
        dl.save(update_fields=["quantity_invoiced"])
        self.assertEqual(services.remaining_to_invoice(dl), Decimal("7"))

    def test_build_lines_excludes_fully_invoiced(self):
        note = self._make_posted_delivery()
        dl = note.lines.get(line_no=1)
        dl.quantity_invoiced = dl.quantity
        dl.save(update_fields=["quantity_invoiced"])
        rows = services.build_invoice_lines_from_delivery(note)
        self.assertEqual([dl.pk for dl, _r in rows], [note.lines.get(line_no=2).pk])

    # ------------------------------------------------------------------
    # create_invoice_from_delivery
    # ------------------------------------------------------------------
    def test_create_invoice_from_delivery(self):
        note = self._make_posted_delivery()
        invoice = self._make_invoice(note)
        self.assertEqual(invoice.status, DocumentStatus.DRAFT)
        self.assertTrue(invoice.number.startswith("INV-"))
        self.assertEqual(invoice.customer, self.customer)
        self.assertEqual(invoice.sales_order, note.sales_order)
        self.assertEqual(invoice.lines.count(), 2)
        self.assertEqual(invoice.customer_name_snapshot, self.customer.name)
        self.assertEqual(invoice.shipping_address_text, note.shipping_address_text)

    def test_create_invoice_copies_line_values_from_order(self):
        note = self._make_posted_delivery()
        invoice = self._make_invoice(note)
        first = invoice.lines.get(line_no=1)
        self.assertEqual(first.product, self.product_a)
        self.assertEqual(first.unit_price, Decimal("100"))
        self.assertEqual(first.quantity, Decimal("10"))

    def test_create_invoice_rejects_unposted_delivery(self):
        order = self._make_approved_order()
        note = services.draft_delivery_from_order(
            order=order,
            user=self.user,
            quantities={ln.pk: ln.quantity for ln in order.lines.all()},
        )
        with self.assertRaises(ValueError) as ctx:
            services.create_invoice_from_delivery(
                delivery=note,
                user=self.user,
                quantities={dl.pk: dl.quantity for dl in note.lines.all()},
            )
        self.assertIn("POSTED", str(ctx.exception))

    def test_create_invoice_rejects_over_invoice(self):
        note = self._make_posted_delivery()
        dl = note.lines.get(line_no=1)
        with self.assertRaises(ValueError) as ctx:
            services.create_invoice_from_delivery(
                delivery=note,
                user=self.user,
                quantities={dl.pk: Decimal("11")},
            )
        self.assertIn("double-invoicing", str(ctx.exception))

    def test_create_invoice_rejects_empty_quantities(self):
        note = self._make_posted_delivery()
        with self.assertRaises(ValueError) as ctx:
            services.create_invoice_from_delivery(
                delivery=note,
                user=self.user,
                quantities={},
            )
        self.assertIn("No quantities", str(ctx.exception))

    def test_create_invoice_records_audit(self):
        note = self._make_posted_delivery()
        before = AuditEvent.objects.count()
        self._make_invoice(note)
        self.assertGreater(AuditEvent.objects.count(), before)

    # ------------------------------------------------------------------
    # recalculate_invoice totals
    # ------------------------------------------------------------------
    def test_recalculate_invoice_totals(self):
        note = self._make_posted_delivery()
        invoice = self._make_invoice(note)
        self.assertEqual(invoice.subtotal_txn, Decimal("2000.0000"))
        self.assertEqual(invoice.total_txn, Decimal("2000.0000"))

    # ------------------------------------------------------------------
    # submit_invoice
    # ------------------------------------------------------------------
    def test_submit_moves_draft_to_submitted(self):
        note = self._make_posted_delivery()
        invoice = self._make_invoice(note)
        services.submit_invoice(invoice, self.user)
        self.assertEqual(invoice.status, DocumentStatus.SUBMITTED)
        self.assertIsNotNone(invoice.submitted_at)

    def test_submit_rejects_approved(self):
        note = self._make_posted_delivery()
        invoice = self._make_invoice(note)
        invoice.status = DocumentStatus.APPROVED
        invoice.save(update_fields=["status"])
        with self.assertRaises(ValueError) as ctx:
            services.submit_invoice(invoice, self.user)
        self.assertIn("DRAFT", str(ctx.exception))

    # ------------------------------------------------------------------
    # _resolve_account (CFG-007)
    # ------------------------------------------------------------------
    def test_resolve_account_fails_loudly_when_unmapped(self):
        AccountMapping.objects.filter(key=MappingKey.COGS).delete()
        with self.assertRaises(PostingError) as ctx:
            services._resolve_account(MappingKey.COGS)
        self.assertIn("CFG-007", str(ctx.exception))

    # ------------------------------------------------------------------
    # build_sales_invoice_journal (SAL-009)
    # ------------------------------------------------------------------
    def test_journal_is_balanced_and_typed(self):
        note = self._make_posted_delivery()
        invoice = self._make_invoice(note)
        services.submit_invoice(invoice, self.user)
        draft = services.build_sales_invoice_journal(invoice, user=self.user)
        self.assertEqual(draft.journal_type, "SALES")
        self.assertEqual(draft.source_doc_type, "SI")
        self.assertEqual(draft.source_doc_number, invoice.number)
        self.assertEqual(draft.entry_date, invoice.posting_date)

        total_debit = sum(ln.debit_base for ln in draft.lines)
        total_credit = sum(ln.credit_base for ln in draft.lines)
        self.assertEqual(total_debit, total_credit)
        # AR control line carries the full invoice total; COGS/inventory legs
        # (SAL-010) add to the debit side when the delivery was costed.
        ar = next(ln for ln in draft.lines if ln.account.code == "1210")
        self.assertEqual(ar.debit_base, invoice.total_base)

    def test_journal_drives_ar_and_revenue(self):
        note = self._make_posted_delivery()
        invoice = self._make_invoice(note)
        draft = services.build_sales_invoice_journal(invoice, user=self.user)
        ar = next(ln for ln in draft.lines if ln.account.code == "1210")
        revenue = next(ln for ln in draft.lines if ln.account.code == "4100")
        self.assertEqual(ar.debit_base, invoice.total_base)
        self.assertEqual(ar.customer, invoice.customer)
        self.assertEqual(revenue.credit_base, invoice.taxable_base_base)

    def test_journal_adds_output_tax_line(self):
        tax = make_tax(code="INV-VAT", rate=Decimal("11.0"))
        product = make_product(sku="INV-TAX", price=Decimal("100"), tax=tax)
        seed_stock(
            product, self.warehouse, Decimal("100"), Decimal("50"), self.user, "SEED-INV-TAX"
        )
        order = make_order(customer=self.customer, warehouse=self.warehouse)
        make_line(
            order, product=product, qty=Decimal("10"), price=Decimal("100"), tax=tax, line_no=1
        )
        order.status = DocumentStatus.APPROVED
        order.approved_at = "2026-08-15T10:00:00Z"
        order.save(update_fields=["status", "approved_at"])
        note = services.draft_delivery_from_order(
            order=order, user=self.user, quantities={order.lines.get().pk: Decimal("10")}
        )
        services.post_delivery(note, self.user)
        dl_post = note.lines.get(line_no=1)
        dl_post.unit_cost = Decimal("0")
        dl_post.total_cost = Decimal("0")
        dl_post.save(update_fields=["unit_cost", "total_cost"])
        invoice = services.create_invoice_from_delivery(
            delivery=note,
            user=self.user,
            quantities={note.lines.get().pk: Decimal("10")},
        )
        self.assertGreater(invoice.tax_base, ZERO)

        draft = services.build_sales_invoice_journal(invoice, user=self.user)
        self.assertEqual(len(draft.lines), 3)
        output_tax = draft.lines[2]
        self.assertEqual(output_tax.account.code, "2310")
        self.assertEqual(output_tax.credit_base, invoice.tax_base)
        self.assertEqual(
            sum(ln.debit_base for ln in draft.lines),
            sum(ln.credit_base for ln in draft.lines),
        )

    def test_journal_adds_cogs_and_inventory_when_costed(self):
        note = self._make_posted_delivery()
        invoice = self._make_invoice(note)
        dl = note.lines.get(line_no=1)
        dl.unit_cost = Decimal("80")
        dl.total_cost = Decimal("800")
        dl.save(update_fields=["unit_cost", "total_cost"])
        # Zero the other costed line so exactly one COGS leg is produced; the
        # seeded stock would otherwise cost it too and collapse to the last
        # 5010 line in the account map below.
        other = note.lines.exclude(pk=dl.pk).first()
        other.unit_cost = Decimal("0")
        other.total_cost = Decimal("0")
        other.save(update_fields=["unit_cost", "total_cost"])

        draft = services.build_sales_invoice_journal(invoice, user=self.user)
        accounts = {ln.account.code: ln for ln in draft.lines}
        self.assertIn("5010", accounts)  # COGS
        self.assertIn("1310", accounts)  # INVENTORY
        self.assertEqual(accounts["5010"].debit_base, Decimal("800"))
        self.assertEqual(accounts["1310"].credit_base, Decimal("800"))

    def test_journal_skips_zero_cost_lines(self):
        note = self._make_posted_delivery()
        for dl in note.lines.all():
            dl.unit_cost = Decimal("0")
            dl.total_cost = Decimal("0")
            dl.save(update_fields=["unit_cost", "total_cost"])
        invoice = self._make_invoice(note)
        draft = services.build_sales_invoice_journal(invoice, user=self.user)
        self.assertEqual(len(draft.lines), 2)

    # ------------------------------------------------------------------
    # _posting_required_mappings — conditional mapping declaration
    # ------------------------------------------------------------------
    def test_required_mappings_always_includes_ar_and_revenue(self):
        note = self._make_posted_delivery()
        invoice = self._make_invoice(note)
        keys = services._posting_required_mappings(invoice)
        self.assertIn(MappingKey.ACCOUNTS_RECEIVABLE, keys)
        self.assertIn(MappingKey.SALES_REVENUE, keys)

    def test_required_mappings_includes_output_tax_when_tax_base(self):
        tax = make_tax(code="RM-VAT", rate=Decimal("11.0"))
        product = make_product(sku="RM-TAX", price=Decimal("100"), tax=tax)
        seed_stock(
            product, self.warehouse, Decimal("100"), Decimal("50"), self.user, "SEED-RM-TAX"
        )
        order = make_order(customer=self.customer, warehouse=self.warehouse)
        make_line(
            order, product=product, qty=Decimal("10"), price=Decimal("100"), tax=tax, line_no=1
        )
        order.status = DocumentStatus.APPROVED
        order.approved_at = "2026-08-15T10:00:00Z"
        order.save(update_fields=["status", "approved_at"])
        note = services.draft_delivery_from_order(
            order=order, user=self.user, quantities={order.lines.get().pk: Decimal("10")}
        )
        services.post_delivery(note, self.user)
        invoice = services.create_invoice_from_delivery(
            delivery=note,
            user=self.user,
            quantities={note.lines.get().pk: Decimal("10")},
        )
        self.assertGreater(invoice.tax_base, ZERO)
        keys = services._posting_required_mappings(invoice)
        self.assertIn(MappingKey.OUTPUT_TAX, keys)

    def test_required_mappings_excludes_output_tax_when_no_tax(self):
        note = self._make_posted_delivery()
        invoice = self._make_invoice(note)
        self.assertEqual(invoice.tax_base, ZERO)
        keys = services._posting_required_mappings(invoice)
        self.assertNotIn(MappingKey.OUTPUT_TAX, keys)

    def test_required_mappings_includes_cogs_when_costed(self):
        note = self._make_posted_delivery()
        invoice = self._make_invoice(note)
        dl = note.lines.get(line_no=1)
        dl.unit_cost = Decimal("80")
        dl.total_cost = Decimal("800")
        dl.save(update_fields=["unit_cost", "total_cost"])
        keys = services._posting_required_mappings(invoice)
        self.assertIn(MappingKey.COGS, keys)
        self.assertIn(MappingKey.INVENTORY, keys)

    def test_required_mappings_excludes_cogs_when_no_cost(self):
        note = self._make_posted_delivery()
        for dl in note.lines.all():
            dl.unit_cost = Decimal("0")
            dl.total_cost = Decimal("0")
            dl.save(update_fields=["unit_cost", "total_cost"])
        invoice = self._make_invoice(note)
        keys = services._posting_required_mappings(invoice)
        self.assertNotIn(MappingKey.COGS, keys)
        self.assertNotIn(MappingKey.INVENTORY, keys)

    def test_required_mappings_tuple_matches_journal_accounts(self):
        """Every key in required_mappings resolves to an account present in the journal."""
        note = self._make_posted_delivery()
        invoice = self._make_invoice(note)
        draft = services.build_sales_invoice_journal(invoice, user=self.user)
        journal_account_ids = {ln.account.pk for ln in draft.lines}
        for key in services._posting_required_mappings(invoice):
            mapping = AccountMapping.objects.get(key=key)
            self.assertIn(mapping.account.pk, journal_account_ids)

    # ------------------------------------------------------------------
    # post_invoice — real engine end-to-end (SAL-009, BR-005, GL-002)
    # ------------------------------------------------------------------
    def test_post_requires_submitted(self):
        note = self._make_posted_delivery()
        invoice = self._make_invoice(note)
        with self.assertRaises(ValueError) as ctx:
            services.post_invoice(invoice, self.user)
        self.assertIn("SUBMITTED", str(ctx.exception))

    def test_post_persists_journal_and_flips_status(self):
        note = self._make_posted_delivery()
        invoice = self._make_invoice(note)
        services.submit_invoice(invoice, self.user)
        journal_count_before = JournalEntry.objects.count()

        posted = services.post_invoice(invoice, self.user)

        self.assertEqual(posted.status, DocumentStatus.POSTED)
        self.assertIsNotNone(posted.journal_entry_id)
        self.assertIsNotNone(posted.posted_by)
        self.assertEqual(JournalEntry.objects.count(), journal_count_before + 1)
        entry = posted.journal_entry
        self.assertEqual(entry.entry_date, invoice.posting_date)
        self.assertEqual(entry.source_doc_type, "SI")
        self.assertEqual(entry.source_doc_number, invoice.number)
        # The AR control line carries the full invoice total; the journal also
        # carries COGS/inventory legs (SAL-010) since the delivery was costed.
        ar_line = entry.lines.filter(customer=invoice.customer).first()
        self.assertIsNotNone(ar_line)
        self.assertEqual(ar_line.debit_base, invoice.total_base)
        self.assertEqual(entry.total_debit_base, entry.total_credit_base)
        # Customer dimension carried to the control-account line.
        self.assertTrue(entry.lines.filter(customer=invoice.customer).exists())

    def test_post_is_idempotent_across_retry(self):
        note = self._make_posted_delivery()
        invoice = self._make_invoice(note)
        services.submit_invoice(invoice, self.user)

        first = services.post_invoice(invoice, self.user)
        first_entry = first.journal_entry
        # Re-post the same already-POSTED invoice: status guard rejects it.
        with self.assertRaises(ValueError):
            services.post_invoice(invoice, self.user)
        # The engine idempotency key would also dedupe on the exact retry.
        self.assertEqual(
            JournalEntry.objects.filter(
                idempotency_key=f"sales-invoice:{invoice.pk}:post:v1"
            ).count(),
            1,
        )
        self.assertEqual(first_entry.pk, invoice.journal_entry_id)

    def test_post_failure_rolls_back_fully(self):
        """A posting error leaves the invoice SUBMITTED with no journal and no
        quantity_invoiced bump (BR-005 atomicity)."""
        note = self._make_posted_delivery()
        invoice = self._make_invoice(note)
        services.submit_invoice(invoice, self.user)
        # Break a required mapping so the engine rejects before/after building.
        AccountMapping.objects.filter(key=MappingKey.SALES_REVENUE).delete()

        with self.assertRaises(PostingError):
            services.post_invoice(invoice, self.user)

        invoice.refresh_from_db()
        self.assertEqual(invoice.status, DocumentStatus.SUBMITTED)
        self.assertIsNone(invoice.journal_entry_id)
        self.assertFalse(
            JournalEntry.objects.filter(
                idempotency_key=f"sales-invoice:{invoice.pk}:post:v1"
            ).exists()
        )
        for dl in note.lines.all():
            dl.refresh_from_db()
            self.assertEqual(dl.quantity_invoiced, ZERO)


class InvoiceViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        make_sequence("SO", prefix="SO-")
        make_sequence("DN", prefix="DN-")
        make_sequence("SI", prefix="INV-")
        _ensure_account_mappings()
        cls._ensure_open_posting_period()
        cls.warehouse = make_warehouse("WH-INV2")
        cls.customer = make_customer("INV-C2")
        cls.product = make_product(sku="INV-VIEW", price=Decimal("100"))
        make_company()
        cls.creator = _make_user(
            "invoice-creator",
            permissions=[
                "sales.add_salesinvoice",
                "sales.view_salesinvoice",
                "sales.change_salesinvoice",
            ],
        )
        seed_stock(
            cls.product,
            cls.warehouse,
            Decimal("100"),
            Decimal("50"),
            cls.creator,
            "SEED-INV-VIEW",
        )
        cls.poster = _make_user(
            "invoice-poster",
            permissions=[
                "sales.view_salesinvoice",
                "sales.change_salesinvoice",
                "core.post_sales_invoice",
            ],
        )

    @classmethod
    def _ensure_open_posting_period(cls):
        make_sequence("JE", prefix="JV-")
        today = date.today()
        if FiscalPeriod.objects.filter(
            start_date__lte=today, end_date__gte=today, status="OPEN"
        ).exists():
            return
        fiscal_year = (
            FiscalYear.objects.filter(start_date__lte=today, end_date__gte=today)
            .order_by("pk")
            .first()
        )
        if fiscal_year is None:
            fiscal_year, _ = FiscalYear.objects.get_or_create(
                code=f"INV-FY-{today.year}",
                defaults=dict(
                    start_date=date(today.year, 1, 1),
                    end_date=date(today.year, 12, 31),
                ),
            )
        FiscalPeriod.objects.get_or_create(
            fiscal_year=fiscal_year,
            period_no=9,
            defaults=dict(
                name="INV-OPEN",
                start_date=date(today.year, 9, 1),
                end_date=date(today.year, 9, 30),
            ),
        )

    def setUp(self):
        order = make_order(customer=self.customer, warehouse=self.warehouse)
        ln = make_line(
            order, product=self.product, qty=Decimal("5"), price=Decimal("100"), line_no=1
        )
        order.status = DocumentStatus.APPROVED
        order.approved_at = "2026-08-15T10:00:00Z"
        order.save(update_fields=["status", "approved_at"])
        self.note = services.draft_delivery_from_order(
            order=order, user=self.creator, quantities={ln.pk: Decimal("5")}
        )
        services.post_delivery(self.note, self.creator)

    def _invoice(self):
        dl = self.note.lines.get(line_no=1)
        return services.create_invoice_from_delivery(
            delivery=self.note,
            user=self.creator,
            quantities={dl.pk: Decimal("5")},
        )

    def test_list_renders(self):
        self.client.force_login(self.creator)
        response = self.client.get(reverse("sales:invoice_list"))
        self.assertEqual(response.status_code, 200)

    def test_create_picker_shows_delivery(self):
        self.client.force_login(self.creator)
        response = self.client.get(reverse("sales:invoice_create"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.note.number)

    def test_create_posts_and_drafts_invoice(self):
        self.client.force_login(self.creator)
        dl = self.note.lines.get(line_no=1)
        response = self.client.post(
            reverse("sales:invoice_create"),
            {"delivery": self.note.pk, f"qty_{dl.pk}": "5"},
        )
        self.assertEqual(response.status_code, 302)
        invoice = SalesInvoice.objects.get(customer_name_snapshot=self.customer.name)
        self.assertEqual(invoice.status, DocumentStatus.DRAFT)
        self.assertEqual(invoice.lines.get().quantity, Decimal("5"))

    def test_create_rejects_over_invoice_from_form(self):
        self.client.force_login(self.creator)
        dl = self.note.lines.get(line_no=1)
        response = self.client.post(
            reverse("sales:invoice_create"),
            {"delivery": self.note.pk, f"qty_{dl.pk}": "99"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(SalesInvoice.objects.count(), 0)

    def test_submit_view(self):
        invoice = self._invoice()
        self.client.force_login(self.creator)
        response = self.client.post(reverse("sales:invoice_submit", args=[invoice.pk]))
        self.assertEqual(response.status_code, 302)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, DocumentStatus.SUBMITTED)

    def test_post_view_denies_without_permission(self):
        invoice = self._invoice()
        services.submit_invoice(invoice, self.creator)
        self.client.force_login(self.creator)  # no core.post_sales_invoice
        response = self.client.post(reverse("sales:invoice_post", args=[invoice.pk]))
        self.assertEqual(response.status_code, 403)

    def test_post_view_success_via_real_engine(self):
        invoice = self._invoice()
        services.submit_invoice(invoice, self.creator)
        self.client.force_login(self.poster)
        response = self.client.post(reverse("sales:invoice_post", args=[invoice.pk]))
        self.assertEqual(response.status_code, 302)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, DocumentStatus.POSTED)
        self.assertIsNotNone(invoice.journal_entry_id)

    def test_detail_renders(self):
        invoice = self._invoice()
        self.client.force_login(self.creator)
        response = self.client.get(reverse("sales:invoice_detail", args=[invoice.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, invoice.number)

    def test_get_absolute_url_resolves_to_detail(self):
        """The list 'View' link and Number column use get_absolute_url — it must exist and resolve."""
        invoice = self._invoice()
        self.assertEqual(
            invoice.get_absolute_url(), reverse("sales:invoice_detail", args=[invoice.pk])
        )

    def test_print_renders(self):
        invoice = self._invoice()
        self.client.force_login(self.creator)
        response = self.client.get(reverse("sales:invoice_print", args=[invoice.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "INVOICE")


def _make_user(username, permissions=()):
    user = make_user(username)
    for codename in permissions:
        user.user_permissions.add(Permission.objects.get(codename=codename.split(".")[-1]))
    user.save()
    return user
