"""Stable Day-1 contract for all automatic accounting postings.

Sales, purchasing, inventory, and payments depend on this module.  Callers build
an immutable journal draft and pass it to :meth:`PostingService.post`; they must
never create ``JournalEntry`` or ``JournalLine`` rows themselves.

The persistence rules (mapping validation, balance enforcement, idempotency, and
reversal handling) deliberately remain the posting engine's responsibility.  The
Day-2 implementation can grow behind this interface without changing callers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Generic, Protocol, TypeVar

from django.contrib.auth.models import AbstractBaseUser
from django.db import models, transaction

from apps.ledger.models import Account, JournalEntry, JournalType
from apps.ledger.services.exceptions import PostingContractError, PostingEngineUnavailable

if TYPE_CHECKING:
    from apps.catalog.models import Product
    from apps.core.models import Currency, TaxCode
    from apps.inventory.models import Warehouse
    from apps.parties.models import Customer, Vendor
    from apps.payments.models import MoneyAccount

SourceT = TypeVar("SourceT", bound=models.Model)


def _is_saved_model(value: object, model_type: type[models.Model] = models.Model) -> bool:
    return isinstance(value, model_type) and value.pk is not None and not value._state.adding


def _validate_decimal(value: object, field_name: str) -> None:
    if not isinstance(value, Decimal):
        raise PostingContractError(f"{field_name} must be Decimal")
    if not value.is_finite():
        raise PostingContractError(f"{field_name} must be finite")
    if value < 0:
        raise PostingContractError(f"{field_name} cannot be negative")


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
            raise PostingContractError("Journal line account must be saved")
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


class JournalBuilder(Protocol[SourceT]):
    """Signature Members 2 and 3 implement for bills and invoices.

    Builders calculate a draft only.  They do not save models, change source
    status, allocate numbers, or open their own transaction.
    """

    def __call__(
        self,
        source: SourceT,
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
    reason: str = ""

    def __post_init__(self) -> None:
        if not _is_saved_model(self.source):
            raise PostingContractError("The posting source must be saved before posting")
        if not _is_saved_model(self.user, AbstractBaseUser):
            raise PostingContractError("Posting requires a saved, authenticated user")
        if not self.user.is_authenticated:
            raise PostingContractError("Posting requires a saved, authenticated user")
        if self.idempotency_key != self.idempotency_key.strip():
            raise PostingContractError("idempotency_key cannot have surrounding whitespace")
        if not self.idempotency_key or len(self.idempotency_key) > 120:
            raise PostingContractError("idempotency_key must contain 1 to 120 characters")
        if not callable(self.build_journal):
            raise PostingContractError("build_journal must be callable")
        if not isinstance(self.reason, str):
            raise PostingContractError("reason must be text")


@dataclass(frozen=True, slots=True, kw_only=True)
class PostingResult:
    """Result returned for both a new post and an idempotent retry."""

    journal_entry: JournalEntry
    created: bool


class PostingService(ABC, Generic[SourceT]):
    """Atomic template for the centralized posting engine.

    ``post`` is intentionally final-by-convention.  Implementations override
    :meth:`_post_locked`, which always receives a freshly loaded, row-locked
    source inside the same outer database transaction as every posting effect.
    Exceptions propagate and roll back the complete unit of work (BR-005).
    """

    @transaction.atomic
    def post(self, request: PostingRequest[SourceT]) -> PostingResult:
        source_type = type(request.source)
        locked_source = source_type._default_manager.select_for_update().get(
            pk=request.source.pk
        )
        return self._post_locked(request, locked_source)

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


# Useful for annotations on registries which select a builder by source type.
JournalBuilderFactory = Callable[[SourceT], JournalBuilder[SourceT]]
