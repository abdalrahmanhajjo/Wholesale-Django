from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.core.models import Currency, DocumentSequence, DocumentType
from apps.ledger.models import (
    Account,
    AccountSubtype,
    AccountType,
    JournalEntry,
    JournalType,
    NormalBalance,
)
from apps.ledger.services import (
    JournalDraft,
    JournalLineDraft,
    PostingContractError,
    PostingEngineStub,
    PostingEngineUnavailable,
    PostingErrorCode,
    PostingRequest,
    PostingResult,
    PostingService,
)


class ExplodingPostingService(PostingService[DocumentSequence]):
    def _post_locked(self, request, source):
        DocumentSequence.objects.create(
            document_type=DocumentType.JOURNAL_ENTRY,
            series="ROLLBACK-PROBE",
        )
        raise RuntimeError("posting failed")


class CapturingPostingService(PostingService[DocumentSequence]):
    locked_source = None

    def _post_locked(self, request, source):
        self.locked_source = source
        return PostingResult(journal_entry=JournalEntry(), created=True)


class InvalidResultPostingService(PostingService[DocumentSequence]):
    def _post_locked(self, request, source):
        return None


class PostingContractTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(id=910_001, username="member4")
        cls.currency, _ = Currency.objects.update_or_create(
            code="USD",
            defaults={
                "name": "Posting Contract Test Currency",
                "symbol": "T",
                "decimal_places": 2,
                "is_base": True,
            },
        )
        cls.account = Account.objects.create(
            id=910_001,
            code="TEST-1000",
            name="Posting contract account",
            account_type=AccountType.ASSET,
            subtype=AccountSubtype.CURRENT_ASSET,
            normal_balance=NormalBalance.DEBIT,
        )
        cls.source = DocumentSequence.objects.create(
            id=910_001,
            document_type=DocumentType.JOURNAL_ENTRY,
            series="SOURCE",
        )

    def build_journal(self, source, *, user):
        return JournalDraft(
            entry_date=date(2026, 8, 31),
            journal_type=JournalType.GENERAL,
            narration=f"Contract probe by {user}",
            currency=self.currency,
            exchange_rate=Decimal("1"),
            source_doc_type=source.document_type,
            source_doc_number=source.series,
            lines=(
                JournalLineDraft(account=self.account, debit_base=Decimal("1")),
                JournalLineDraft(account=self.account, credit_base=Decimal("1")),
            ),
        )

    def test_request_rejects_unsaved_source(self):
        with self.assertRaisesMessage(PostingContractError, "must be saved"):
            PostingRequest(
                source=DocumentSequence(),
                user=self.user,
                idempotency_key="test:unsaved",
                build_journal=self.build_journal,
            )

    def test_request_rejects_unsaved_source_with_manually_assigned_pk(self):
        with self.assertRaisesMessage(PostingContractError, "must be saved"):
            PostingRequest(
                source=DocumentSequence(pk=999),
                user=self.user,
                idempotency_key="test:fake-pk",
                build_journal=self.build_journal,
            )

    def test_journal_builder_signature_produces_immutable_draft(self):
        draft = self.build_journal(self.source, user=self.user)

        self.assertEqual(draft.source_doc_number, "SOURCE")
        self.assertEqual(len(draft.lines), 2)
        with self.assertRaises(AttributeError):
            draft.narration = "changed"

    def test_preview_builds_draft_without_persisting_builder_writes(self):
        def impure_builder(source, *, user):
            DocumentSequence.objects.create(
                document_type=DocumentType.JOURNAL_ENTRY,
                series="PREVIEW-WRITE",
            )
            return self.build_journal(source, user=user)

        request = PostingRequest(
            source=self.source,
            user=self.user,
            idempotency_key="test:preview",
            build_journal=impure_builder,
        )

        draft = PostingEngineStub().preview(request)

        self.assertIsInstance(draft, JournalDraft)
        self.assertFalse(DocumentSequence.objects.filter(series="PREVIEW-WRITE").exists())

    def test_preview_rejects_wrong_builder_return_type_with_stable_code(self):
        request = PostingRequest(
            source=self.source,
            user=self.user,
            idempotency_key="test:invalid-builder",
            build_journal=lambda source, *, user: object(),
        )

        with self.assertRaises(PostingContractError) as raised:
            PostingEngineStub().preview(request)

        self.assertEqual(raised.exception.code, PostingErrorCode.INVALID_BUILDER_RESULT)

    def test_line_rejects_float_amounts(self):
        with self.assertRaisesMessage(PostingContractError, "must be Decimal"):
            JournalLineDraft(account=self.account, debit_base=1.0)

    def test_line_rejects_two_sided_amounts(self):
        with self.assertRaisesMessage(PostingContractError, "exactly one"):
            JournalLineDraft(
                account=self.account,
                debit_base=Decimal("1"),
                credit_base=Decimal("1"),
            )

    def test_request_rejects_anonymous_or_unsaved_actor(self):
        unsaved_user = get_user_model()(username="unsaved")
        with self.assertRaisesMessage(PostingContractError, "authenticated user"):
            PostingRequest(
                source=self.source,
                user=unsaved_user,
                idempotency_key="test:actor",
                build_journal=self.build_journal,
            )

    def test_stub_fails_explicitly_without_writing(self):
        request = PostingRequest(
            source=self.source,
            user=self.user,
            idempotency_key="test:stub",
            build_journal=self.build_journal,
        )

        with self.assertRaisesMessage(PostingEngineUnavailable, "Day-1 engine stub"):
            PostingEngineStub().post(request)

        self.assertFalse(JournalEntry.objects.exists())

    def test_post_passes_fresh_row_locked_source_to_implementation(self):
        request = PostingRequest(
            source=self.source,
            user=self.user,
            idempotency_key="test:locked-source",
            build_journal=self.build_journal,
        )
        service = CapturingPostingService()

        result = service.post(request)

        self.assertTrue(result.created)
        self.assertIsNot(service.locked_source, self.source)
        self.assertEqual(service.locked_source.pk, self.source.pk)

    def test_template_methods_cannot_be_overridden(self):
        with self.assertRaisesMessage(TypeError, "protected method"):

            class InvalidPostingService(PostingService[DocumentSequence]):
                def post(self, request):
                    return None

                def _post_locked(self, request, source):
                    return None

    def test_post_rejects_invalid_implementation_result(self):
        request = PostingRequest(
            source=self.source,
            user=self.user,
            idempotency_key="test:invalid-result",
            build_journal=self.build_journal,
        )

        with self.assertRaises(PostingContractError) as raised:
            InvalidResultPostingService().post(request)

        self.assertEqual(raised.exception.code, PostingErrorCode.INVALID_SERVICE_RESULT)

    def test_error_exposes_machine_readable_code(self):
        with self.assertRaises(PostingContractError) as raised:
            JournalLineDraft(account=self.account, debit_base=1.0)

        self.assertEqual(raised.exception.code, PostingErrorCode.INVALID_AMOUNT)

    def test_atomic_wrapper_rolls_back_all_work_on_failure(self):
        request = PostingRequest(
            source=self.source,
            user=self.user,
            idempotency_key="test:rollback",
            build_journal=self.build_journal,
        )

        with self.assertRaisesMessage(RuntimeError, "posting failed"):
            ExplodingPostingService().post(request)

        self.assertFalse(DocumentSequence.objects.filter(series="ROLLBACK-PROBE").exists())

    def test_result_distinguishes_new_post_from_retry(self):
        result = PostingResult(journal_entry=JournalEntry(), created=False)

        self.assertFalse(result.created)
