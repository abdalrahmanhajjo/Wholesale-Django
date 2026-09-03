"""Stable Day-1 contract for all automatic accounting postings.

Sales, purchasing, inventory, and payments depend on this module.  Callers build
an immutable journal draft and pass it to :meth:`PostingService.post`; they must
never create ``JournalEntry`` or ``JournalLine`` rows themselves.

The persistence rules (mapping validation, balance enforcement, idempotency, and
reversal handling) deliberately remain the posting engine's responsibility.  The
Day-2 implementation can grow behind this interface without changing callers.
"""

from __future__ import annotations

import logging
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Generic, Protocol, TypeVar

from django.contrib.auth.models import AbstractBaseUser
from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, models, transaction
from django.utils import timezone

from apps.core.models import DocumentSequence, DocumentType, FiscalPeriod, PeriodStatus
from apps.ledger.models import (
    Account,
    AccountMapping,
    JournalEntry,
    JournalLine,
    JournalType,
    MappingKey,
    PostingEffect,
    PostingLink,
)
from apps.ledger.services.exceptions import (
    PostingContractError,
    PostingEngineUnavailable,
    PostingError,
    PostingErrorCode,
)

if TYPE_CHECKING:
    from apps.catalog.models import Product
    from apps.core.models import Currency, TaxCode
    from apps.inventory.models import Warehouse
    from apps.parties.models import Customer, Vendor
    from apps.payments.models import MoneyAccount

SourceT = TypeVar("SourceT", bound=models.Model)
BuilderSourceT = TypeVar("BuilderSourceT", bound=models.Model, contravariant=True)
logger = logging.getLogger(__name__)


def _is_saved_model(value: object, model_type: type[models.Model] = models.Model) -> bool:
    return isinstance(value, model_type) and value.pk is not None and not value._state.adding


def _validate_decimal(value: object, field_name: str) -> None:
    if not isinstance(value, Decimal):
        raise PostingContractError(
            f"{field_name} must be Decimal", code=PostingErrorCode.INVALID_AMOUNT
        )
    if not value.is_finite():
        raise PostingContractError(
            f"{field_name} must be finite", code=PostingErrorCode.INVALID_AMOUNT
        )
    if value < 0:
        raise PostingContractError(
            f"{field_name} cannot be negative", code=PostingErrorCode.INVALID_AMOUNT
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class JournalLineDraft:
    """One proposed journal line, before any database row is written.

    Amounts use :class:`Decimal`.  Exactly-one-side and balance validation belong
    to the posting engine so every source type receives identical enforcement.
    Optional dimensions support the AR/AP, inventory, tax, and cash subledgers.
    """

    account: Account
    description: str = ""
    debit_base: Decimal = Decimal("0")
    credit_base: Decimal = Decimal("0")
    debit_txn: Decimal = Decimal("0")
    credit_txn: Decimal = Decimal("0")
    customer: Customer | None = None
    vendor: Vendor | None = None
    product: Product | None = None
    warehouse: Warehouse | None = None
    tax_code: TaxCode | None = None
    money_account: MoneyAccount | None = None

    def __post_init__(self) -> None:
        if not _is_saved_model(self.account, Account):
            raise PostingContractError(
                "Journal line account must be saved", code=PostingErrorCode.UNSAVED_OBJECT
            )
        if len(self.description) > 255:
            raise PostingContractError("Journal line description exceeds 255 characters")

        amount_fields = (
            "debit_base",
            "credit_base",
            "debit_txn",
            "credit_txn",
        )
        for field_name in amount_fields:
            _validate_decimal(getattr(self, field_name), field_name)

        has_debit = self.debit_base > 0 and self.credit_base == 0
        has_credit = self.credit_base > 0 and self.debit_base == 0
        if not (has_debit ^ has_credit):
            raise PostingContractError(
                "Journal line must contain exactly one positive base-currency side"
            )
        if has_debit and self.credit_txn != 0:
            raise PostingContractError("Debit line cannot contain transaction credit")
        if has_credit and self.debit_txn != 0:
            raise PostingContractError("Credit line cannot contain transaction debit")
        if self.customer is not None and self.vendor is not None:
            raise PostingContractError("Journal line cannot reference customer and vendor")


@dataclass(frozen=True, slots=True, kw_only=True)
class JournalDraft:
    """Complete journal proposed by a source-specific builder."""

    entry_date: date
    journal_type: str
    narration: str
    currency: Currency
    exchange_rate: Decimal
    source_doc_type: str
    source_doc_number: str
    lines: tuple[JournalLineDraft, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.entry_date, date):
            raise PostingContractError("entry_date must be a date")
        if self.journal_type not in JournalType.values:
            raise PostingContractError(f"Unsupported journal type: {self.journal_type!r}")
        if not _is_saved_model(self.currency):
            raise PostingContractError("Journal currency must be saved")
        _validate_decimal(self.exchange_rate, "exchange_rate")
        if self.exchange_rate == 0:
            raise PostingContractError("exchange_rate must be greater than zero")
        if not 1 <= len(self.source_doc_type) <= 4:
            raise PostingContractError("source_doc_type must contain 1 to 4 characters")
        if not 1 <= len(self.source_doc_number) <= 32:
            raise PostingContractError("source_doc_number must contain 1 to 32 characters")
        if not self.lines:
            raise PostingContractError("A journal draft needs at least one line")
        if not isinstance(self.lines, tuple):
            raise PostingContractError("Journal draft lines must be an immutable tuple")
        if not all(isinstance(line, JournalLineDraft) for line in self.lines):
            raise PostingContractError("Journal draft contains an invalid line")


class JournalBuilder(Protocol[BuilderSourceT]):
    """Signature Members 2 and 3 implement for bills and invoices.

    Builders calculate a draft only.  They do not save models, change source
    status, allocate numbers, or open their own transaction.
    """

    def __call__(
        self,
        source: BuilderSourceT,
        *,
        user: AbstractBaseUser,
    ) -> JournalDraft: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class PostingRequest(Generic[SourceT]):
    """Inputs shared by every operational posting call."""

    source: SourceT
    user: AbstractBaseUser
    idempotency_key: str
    build_journal: JournalBuilder[SourceT]
    required_mappings: tuple[str, ...] = ()
    reason: str = ""
    correlation_id: uuid.UUID = field(default_factory=uuid.uuid4)

    def __post_init__(self) -> None:
        if not _is_saved_model(self.source):
            raise PostingContractError(
                "The posting source must be saved before posting",
                code=PostingErrorCode.UNSAVED_OBJECT,
            )
        if not _is_saved_model(self.user, AbstractBaseUser):
            raise PostingContractError(
                "Posting requires a saved, authenticated user",
                code=PostingErrorCode.UNAUTHENTICATED_ACTOR,
            )
        if not self.user.is_authenticated:
            raise PostingContractError(
                "Posting requires a saved, authenticated user",
                code=PostingErrorCode.UNAUTHENTICATED_ACTOR,
            )
        if self.idempotency_key != self.idempotency_key.strip():
            raise PostingContractError("idempotency_key cannot have surrounding whitespace")
        if not self.idempotency_key or len(self.idempotency_key) > 120:
            raise PostingContractError("idempotency_key must contain 1 to 120 characters")
        if not callable(self.build_journal):
            raise PostingContractError("build_journal must be callable")
        if not isinstance(self.required_mappings, tuple):
            raise PostingContractError("required_mappings must be an immutable tuple")
        invalid_mappings = set(self.required_mappings).difference(MappingKey.values)
        if invalid_mappings:
            raise PostingContractError(
                f"Unsupported account mapping key(s): {', '.join(sorted(invalid_mappings))}"
            )
        if len(set(self.required_mappings)) != len(self.required_mappings):
            raise PostingContractError("required_mappings cannot contain duplicates")
        if not isinstance(self.reason, str):
            raise PostingContractError("reason must be text")
        if not isinstance(self.correlation_id, uuid.UUID):
            raise PostingContractError("correlation_id must be a UUID")


@dataclass(frozen=True, slots=True, kw_only=True)
class PostingResult:
    """Result returned for both a new post and an idempotent retry."""

    journal_entry: JournalEntry
    created: bool


class PostingService(ABC, Generic[SourceT]):
    """Atomic template for the centralized posting engine.

    ``post`` and ``preview`` are protected template methods. Implementations override
    :meth:`_post_locked`, which always receives a freshly loaded, row-locked
    source inside the same outer database transaction as every posting effect.
    Exceptions propagate and roll back the complete unit of work (BR-005).
    """

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        protected = {"post", "preview"}.intersection(cls.__dict__)
        if protected:
            names = ", ".join(sorted(protected))
            raise TypeError(f"Override _post_locked(), not protected method(s): {names}")

    def preview(self, request: PostingRequest[SourceT]) -> JournalDraft:
        """Build a validated draft and guarantee that the preview writes nothing.

        The rollback-only atomic block protects the database even if a team member
        accidentally puts a save inside a builder. Preview does not lock the source
        because it creates no financial effect and is allowed to be advisory.
        """
        with transaction.atomic():
            source_type = type(request.source)
            fresh_source = source_type._default_manager.get(pk=request.source.pk)
            draft = request.build_journal(fresh_source, user=request.user)
            if not isinstance(draft, JournalDraft):
                raise PostingContractError(
                    "Journal builder must return JournalDraft",
                    code=PostingErrorCode.INVALID_BUILDER_RESULT,
                )
            transaction.set_rollback(True)

        logger.info(
            "Posting preview built",
            extra=self._log_context(request, source=fresh_source),
        )
        return draft

    @transaction.atomic
    def post(self, request: PostingRequest[SourceT]) -> PostingResult:
        source_type = type(request.source)
        locked_source = source_type._default_manager.select_for_update().get(
            pk=request.source.pk
        )
        context = self._log_context(request, source=locked_source)
        logger.info("Posting started", extra=context)
        try:
            result = self._post_locked(request, locked_source)
            if not isinstance(result, PostingResult):
                raise PostingContractError(
                    "Posting implementation must return PostingResult",
                    code=PostingErrorCode.INVALID_SERVICE_RESULT,
                )
        except PostingError as exc:
            logger.warning(
                "Posting rejected",
                extra={**context, "posting_error_code": str(exc.code)},
            )
            raise
        except Exception:
            logger.exception("Posting failed", extra=context)
            raise
        logger.info("Posting completed", extra=context)
        return result

    @staticmethod
    def _log_context(
        request: PostingRequest[SourceT], *, source: SourceT
    ) -> dict[str, object]:
        """Non-sensitive structured context shared by all posting log records."""
        return {
            "correlation_id": str(request.correlation_id),
            "posting_source_type": source._meta.label_lower,
            "posting_source_id": source.pk,
            "posting_actor_id": request.user.pk,
        }

    @abstractmethod
    def _post_locked(
        self,
        request: PostingRequest[SourceT],
        source: SourceT,
    ) -> PostingResult:
        """Persist one post; called only by the atomic, locking wrapper."""


class PostingEngineStub(PostingService[SourceT]):
    """Fail-fast Day-1 implementation used until the real engine lands.

    It deliberately writes nothing.  Integrating teams can construct and test
    requests now, while accidental production calls receive a precise error
    instead of silently pretending a journal was posted.
    """

    def _post_locked(
        self,
        request: PostingRequest[SourceT],
        source: SourceT,
    ) -> PostingResult:
        raise PostingEngineUnavailable(
            "Posting persistence is not available in the Day-1 engine stub"
        )


class PostingEngine(PostingService[SourceT]):
    """Production journal persistence with defense-in-depth financial controls."""

    def _post_locked(
        self,
        request: PostingRequest[SourceT],
        source: SourceT,
    ) -> PostingResult:
        existing = self._find_idempotent_result(request, source)
        if existing is not None:
            return existing

        mappings = self._validate_account_mappings(request.required_mappings)
        draft = request.build_journal(source, user=request.user)
        if not isinstance(draft, JournalDraft):
            raise PostingContractError(
                "Journal builder must return JournalDraft",
                code=PostingErrorCode.INVALID_BUILDER_RESULT,
            )
        self._validate_draft(draft, mappings)
        fiscal_period = self._get_open_period(draft.entry_date)
        content_type = ContentType.objects.get_for_model(source, for_concrete_model=False)

        try:
            with transaction.atomic():
                entry = JournalEntry.objects.create(
                    number=self._allocate_journal_number(draft.entry_date),
                    entry_date=draft.entry_date,
                    fiscal_period=fiscal_period,
                    journal_type=draft.journal_type,
                    narration=draft.narration,
                    currency=draft.currency,
                    exchange_rate=draft.exchange_rate,
                    total_debit_base=sum(
                        (line.debit_base for line in draft.lines), Decimal("0")
                    ),
                    total_credit_base=sum(
                        (line.credit_base for line in draft.lines), Decimal("0")
                    ),
                    source_content_type=content_type,
                    source_object_id=source.pk,
                    source_doc_type=draft.source_doc_type,
                    source_doc_number=draft.source_doc_number,
                    idempotency_key=request.idempotency_key,
                    posted_at=timezone.now(),
                    posted_by=request.user,
                    created_by=request.user,
                    updated_by=request.user,
                )
                JournalLine.objects.bulk_create(
                    [
                        self._make_line(entry, line_no, line, draft)
                        for line_no, line in enumerate(draft.lines, 1)
                    ]
                )
                PostingLink.objects.create(
                    source_content_type=content_type,
                    source_object_id=source.pk,
                    source_doc_type=draft.source_doc_type,
                    source_doc_number=draft.source_doc_number,
                    effect_type=PostingEffect.JOURNAL,
                    journal_entry=entry,
                    idempotency_key=request.idempotency_key,
                )
        except IntegrityError:
            retry = self._find_idempotent_result(request, source)
            if retry is not None:
                return retry
            raise
        return PostingResult(journal_entry=entry, created=True)

    @staticmethod
    def _validate_account_mappings(keys: tuple[str, ...]) -> dict[str, Account]:
        if not keys:
            raise PostingContractError(
                "Production posting requires at least one account mapping",
                code=PostingErrorCode.MISSING_ACCOUNT_MAPPING,
            )
        rows = AccountMapping.objects.select_related("account").filter(key__in=keys)
        mappings = {row.key: row.account for row in rows}
        missing = set(keys).difference(mappings)
        if missing:
            raise PostingContractError(
                f"Missing account mapping(s): {', '.join(sorted(missing))}",
                code=PostingErrorCode.MISSING_ACCOUNT_MAPPING,
            )
        invalid = [
            key
            for key, account in mappings.items()
            if not account.is_active or not account.is_postable
        ]
        if invalid:
            raise PostingContractError(
                f"Account mapping(s) target an inactive or non-postable account: {', '.join(sorted(invalid))}",
                code=PostingErrorCode.INVALID_ACCOUNT_MAPPING,
            )
        return mappings

    @staticmethod
    def _validate_draft(draft: JournalDraft, mappings: dict[str, Account]) -> None:
        debit = sum((line.debit_base for line in draft.lines), Decimal("0"))
        credit = sum((line.credit_base for line in draft.lines), Decimal("0"))
        if debit != credit or debit == 0:
            raise PostingContractError(
                f"Journal is not balanced in base currency (debit={debit}, credit={credit})",
                code=PostingErrorCode.UNBALANCED_JOURNAL,
            )
        mapped_account_ids = {account.pk for account in mappings.values()}
        draft_account_ids = {line.account.pk for line in draft.lines}
        unused = mapped_account_ids.difference(draft_account_ids)
        if unused:
            keys = [key for key, account in mappings.items() if account.pk in unused]
            raise PostingContractError(
                f"Required account mapping(s) are not used by the journal: {', '.join(sorted(keys))}",
                code=PostingErrorCode.INVALID_ACCOUNT_MAPPING,
            )
        accounts = Account.objects.only(
            "code", "is_active", "is_postable", "requires_party", "currency_id"
        ).in_bulk(draft_account_ids)
        for line in draft.lines:
            account = accounts.get(line.account.pk)
            if account is None or not account.is_active or not account.is_postable:
                raise PostingContractError(
                    f"Journal account {line.account.pk} is missing, inactive, or non-postable",
                    code=PostingErrorCode.INVALID_ACCOUNT_MAPPING,
                )
            if account.requires_party and line.customer is None and line.vendor is None:
                raise PostingContractError(
                    f"Control account {account.code} requires a customer or vendor",
                    code=PostingErrorCode.INVALID_DRAFT,
                )
            if account.currency_id and account.currency_id != draft.currency.pk:
                raise PostingContractError(
                    f"Account {account.code} does not accept {draft.currency.pk}",
                    code=PostingErrorCode.INVALID_DRAFT,
                )

    @staticmethod
    def _get_open_period(entry_date: date) -> FiscalPeriod:
        periods = list(
            FiscalPeriod.objects.select_for_update().filter(
                start_date__lte=entry_date,
                end_date__gte=entry_date,
                status=PeriodStatus.OPEN,
            )[:2]
        )
        if len(periods) != 1:
            raise PostingContractError(
                f"Posting date {entry_date.isoformat()} must belong to exactly one open fiscal period",
                code=PostingErrorCode.CLOSED_FISCAL_PERIOD,
            )
        return periods[0]

    @staticmethod
    def _allocate_journal_number(entry_date: date) -> str:
        sequence = (
            DocumentSequence.objects.select_for_update()
            .filter(document_type=DocumentType.JOURNAL_ENTRY, is_active=True)
            .order_by("pk")
            .first()
        )
        if sequence is None:
            raise PostingContractError(
                "No active journal-entry number sequence is configured",
                code=PostingErrorCode.JOURNAL_SEQUENCE_UNAVAILABLE,
            )
        period_key = str(entry_date.year)
        if sequence.reset_policy == "MONTHLY":
            period_key = entry_date.strftime("%Y-%m")
        if sequence.reset_policy != "NEVER" and sequence.period_key != period_key:
            sequence.next_number = 1
            sequence.period_key = period_key
        number = (
            f"{sequence.prefix}{sequence.next_number:0{sequence.padding}d}{sequence.suffix}"
        )
        if len(number) > 32:
            raise PostingContractError(
                "Configured journal number exceeds 32 characters",
                code=PostingErrorCode.JOURNAL_SEQUENCE_UNAVAILABLE,
            )
        sequence.next_number += 1
        sequence.save(update_fields=["next_number", "period_key"])
        return number

    @staticmethod
    def _make_line(
        entry: JournalEntry,
        line_no: int,
        line: JournalLineDraft,
        draft: JournalDraft,
    ) -> JournalLine:
        return JournalLine(
            entry=entry,
            line_no=line_no,
            account=line.account,
            description=line.description,
            debit_base=line.debit_base,
            credit_base=line.credit_base,
            debit_txn=line.debit_txn,
            credit_txn=line.credit_txn,
            currency=draft.currency,
            exchange_rate=draft.exchange_rate,
            customer=line.customer,
            vendor=line.vendor,
            product=line.product,
            warehouse=line.warehouse,
            tax_code=line.tax_code,
            money_account=line.money_account,
        )

    @staticmethod
    def _find_idempotent_result(
        request: PostingRequest[SourceT], source: SourceT
    ) -> PostingResult | None:
        entry = (
            JournalEntry.objects.select_related("source_content_type")
            .filter(idempotency_key=request.idempotency_key)
            .first()
        )
        if entry is None:
            return None
        expected_type = ContentType.objects.get_for_model(source, for_concrete_model=False)
        if (
            entry.source_content_type_id != expected_type.pk
            or entry.source_object_id != source.pk
        ):
            raise PostingContractError(
                "Idempotency key is already associated with a different posting source",
                code=PostingErrorCode.IDEMPOTENCY_CONFLICT,
            )
        return PostingResult(journal_entry=entry, created=False)


# Useful for annotations on registries which select a builder by source type.
JournalBuilderFactory = Callable[[SourceT], JournalBuilder[SourceT]]
