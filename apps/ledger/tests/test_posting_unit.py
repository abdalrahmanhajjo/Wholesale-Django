from datetime import date
from decimal import Decimal

from django.test import SimpleTestCase

from apps.accounts.models import User
from apps.core.models import Currency, DocumentSequence
from apps.ledger.models import Account, JournalEntry, JournalType
from apps.ledger.services import (
    JournalDraft,
    JournalLineDraft,
    PostingContractError,
    PostingErrorCode,
    PostingResult,
    PostingService,
)


def mark_as_loaded(instance):
    """Create a database-free model double which satisfies the saved-object contract."""
    instance._state.adding = False
    instance._state.db = "default"
    return instance


class PostingContractUnitTests(SimpleTestCase):
    def setUp(self):
        self.account = mark_as_loaded(Account(pk=1))
        self.currency = mark_as_loaded(Currency(code="USD"))
        self.user = mark_as_loaded(User(pk=1, username="member4"))
        self.source = mark_as_loaded(DocumentSequence(pk=1))

    def build_draft(self):
        return JournalDraft(
            entry_date=date(2026, 9, 1),
            journal_type=JournalType.GENERAL,
            narration="Unit-test draft",
            currency=self.currency,
            exchange_rate=Decimal("1"),
            source_doc_type="JE",
            source_doc_number="UNIT-1",
            lines=(
                JournalLineDraft(account=self.account, debit_base=Decimal("1")),
                JournalLineDraft(account=self.account, credit_base=Decimal("1")),
            ),
        )

    def test_valid_draft_is_immutable(self):
        draft = self.build_draft()

        self.assertEqual(len(draft.lines), 2)
        with self.assertRaises(AttributeError):
            draft.narration = "mutated"

    def test_float_amount_has_stable_error_code(self):
        with self.assertRaises(PostingContractError) as raised:
            JournalLineDraft(account=self.account, debit_base=1.0)

        self.assertEqual(raised.exception.code, PostingErrorCode.INVALID_AMOUNT)

    def test_unsaved_account_is_rejected_even_with_pk(self):
        with self.assertRaises(PostingContractError) as raised:
            JournalLineDraft(account=Account(pk=99), debit_base=Decimal("1"))

        self.assertEqual(raised.exception.code, PostingErrorCode.UNSAVED_OBJECT)

    def test_service_result_models_idempotent_retry(self):
        result = PostingResult(journal_entry=JournalEntry(), created=False)

        self.assertFalse(result.created)

    def test_template_method_cannot_be_replaced(self):
        with self.assertRaisesMessage(TypeError, "protected method"):

            class InvalidService(PostingService[DocumentSequence]):
                def preview(self, request):
                    return self.build_draft()

                def _post_locked(self, request, source):
                    return PostingResult(journal_entry=JournalEntry(), created=True)
