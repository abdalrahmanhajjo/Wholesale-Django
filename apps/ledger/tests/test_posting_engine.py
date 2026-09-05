from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import TransactionTestCase

from apps.core.models import (
    Currency,
    DocumentSequence,
    DocumentType,
    FiscalPeriod,
    FiscalYear,
)
from apps.ledger.models import (
    Account,
    AccountMapping,
    AccountSubtype,
    AccountType,
    JournalEntry,
    MappingKey,
    NormalBalance,
    PostingLink,
)
from apps.ledger.services import (
    JournalDraft,
    JournalLineDraft,
    PostingContractError,
    PostingEngine,
    PostingErrorCode,
    PostingRequest,
)


class PostingEngineTests(TransactionTestCase):
    def setUp(self):
        # TransactionTestCase flushes and recreates content types between tests;
        # discard IDs cached by a preceding case before the posting service uses
        # them for its generic source relationship.
        ContentType.objects.clear_cache()
        self.user = get_user_model().objects.create_user(
            id=900_001, username="posting-engine-member4"
        )
        self.currency = Currency.objects.create(
            code="TST", name="Test Currency", symbol="T", decimal_places=2
        )
        fiscal_year = FiscalYear.objects.create(
            id=900_001,
            code="T-FY-2099",
            start_date=date(2099, 1, 1),
            end_date=date(2099, 12, 31),
        )
        FiscalPeriod.objects.create(
            id=900_001,
            fiscal_year=fiscal_year,
            period_no=8,
            name="Test August 2099",
            start_date=date(2099, 8, 1),
            end_date=date(2099, 8, 31),
        )
        DocumentSequence.objects.create(
            id=900_001,
            document_type=DocumentType.JOURNAL_ENTRY,
            series="DAY2-JOURNAL",
            prefix="T-JV-",
            reset_policy="YEARLY",
        )
        self.debit_account = Account.objects.create(
            id=900_001,
            code="T-DAY2-DR",
            name="Day 2 debit account",
            account_type=AccountType.EXPENSE,
            subtype=AccountSubtype.OPERATING_EXPENSE,
            normal_balance=NormalBalance.DEBIT,
        )
        self.credit_account = Account.objects.create(
            id=900_002,
            code="T-DAY2-CR",
            name="Day 2 credit account",
            account_type=AccountType.INCOME,
            subtype=AccountSubtype.REVENUE,
            normal_balance=NormalBalance.CREDIT,
        )
        AccountMapping.objects.filter(
            key__in=(MappingKey.PURCHASE_EXPENSE, MappingKey.SALES_REVENUE)
        ).delete()
        AccountMapping.objects.create(
            id=900_001,
            key=MappingKey.PURCHASE_EXPENSE,
            account=self.debit_account,
        )
        AccountMapping.objects.create(
            id=900_002,
            key=MappingKey.SALES_REVENUE,
            account=self.credit_account,
        )
        self.source = DocumentSequence.objects.create(
            id=900_002,
            document_type=DocumentType.SALES_ORDER,
            series="DAY2-SOURCE",
        )
        self.engine = PostingEngine()

    def build_journal(self, source, *, user):
        return JournalDraft(
            entry_date=date(2099, 8, 31),
            journal_type="GENERAL",
            narration=f"Production posting for {source.series} by {user.username}",
            currency=self.currency,
            exchange_rate=Decimal("1"),
            source_doc_type="SO",
            source_doc_number=source.series,
            lines=(
                JournalLineDraft(
                    account=self.debit_account,
                    debit_base=Decimal("125.50"),
                ),
                JournalLineDraft(
                    account=self.credit_account,
                    credit_base=Decimal("125.50"),
                ),
            ),
        )

    def request(self, *, key="day2:source:post:v1", builder=None, mappings=None):
        return PostingRequest(
            source=self.source,
            user=self.user,
            idempotency_key=key,
            build_journal=builder or self.build_journal,
            required_mappings=mappings
            or (MappingKey.PURCHASE_EXPENSE, MappingKey.SALES_REVENUE),
        )

    def test_balanced_journal_is_persisted_with_traceability(self):
        result = self.engine.post(self.request())

        self.assertTrue(result.created)
        self.assertEqual(result.journal_entry.total_debit_base, Decimal("125.50"))
        self.assertEqual(result.journal_entry.total_credit_base, Decimal("125.50"))
        self.assertEqual(result.journal_entry.lines.count(), 2)
        self.assertEqual(result.journal_entry.source, self.source)
        self.assertEqual(result.journal_entry.posted_by, self.user)
        self.assertTrue(
            PostingLink.objects.filter(journal_entry=result.journal_entry).exists()
        )

    def test_retry_returns_original_journal_without_duplicate_rows(self):
        first = self.engine.post(self.request())
        second = self.engine.post(self.request())

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.journal_entry.pk, second.journal_entry.pk)
        self.assertEqual(JournalEntry.objects.count(), 1)
        self.assertEqual(PostingLink.objects.count(), 1)

    def test_idempotency_key_cannot_be_reused_for_another_source(self):
        self.engine.post(self.request())
        other_source = DocumentSequence.objects.create(
            id=900_003,
            document_type=DocumentType.PURCHASE_ORDER,
            series="DAY2-OTHER",
        )
        request = PostingRequest(
            source=other_source,
            user=self.user,
            idempotency_key="day2:source:post:v1",
            build_journal=self.build_journal,
            required_mappings=(MappingKey.PURCHASE_EXPENSE, MappingKey.SALES_REVENUE),
        )

        with self.assertRaises(PostingContractError) as raised:
            self.engine.post(request)

        self.assertEqual(raised.exception.code, PostingErrorCode.IDEMPOTENCY_CONFLICT)

    def test_unbalanced_draft_is_rejected_before_any_write(self):
        def unbalanced_builder(source, *, user):
            draft = self.build_journal(source, user=user)
            return JournalDraft(
                entry_date=draft.entry_date,
                journal_type=draft.journal_type,
                narration=draft.narration,
                currency=draft.currency,
                exchange_rate=draft.exchange_rate,
                source_doc_type=draft.source_doc_type,
                source_doc_number=draft.source_doc_number,
                lines=(draft.lines[0],),
            )

        with self.assertRaises(PostingContractError) as raised:
            self.engine.post(self.request(builder=unbalanced_builder))

        self.assertEqual(raised.exception.code, PostingErrorCode.UNBALANCED_JOURNAL)
        self.assertFalse(JournalEntry.objects.exists())

    def test_missing_mapping_is_rejected_before_builder_runs(self):
        AccountMapping.objects.filter(key=MappingKey.SALES_REVENUE).delete()
        builder_called = False

        def builder(source, *, user):
            nonlocal builder_called
            builder_called = True
            return self.build_journal(source, user=user)

        with self.assertRaises(PostingContractError) as raised:
            self.engine.post(self.request(builder=builder))

        self.assertEqual(raised.exception.code, PostingErrorCode.MISSING_ACCOUNT_MAPPING)
        self.assertFalse(builder_called)
        self.assertFalse(JournalEntry.objects.exists())

    def test_required_mapping_must_be_used_by_journal(self):
        unused_account = Account.objects.create(
            id=900_003,
            code="T-DAY2-UNUSED",
            name="Unused mapped account",
            account_type=AccountType.ASSET,
            subtype=AccountSubtype.CURRENT_ASSET,
            normal_balance=NormalBalance.DEBIT,
        )
        AccountMapping.objects.filter(key=MappingKey.PURCHASE_EXPENSE).update(
            account=unused_account
        )

        with self.assertRaises(PostingContractError) as raised:
            self.engine.post(self.request())

        self.assertEqual(raised.exception.code, PostingErrorCode.INVALID_ACCOUNT_MAPPING)
        self.assertFalse(JournalEntry.objects.exists())
