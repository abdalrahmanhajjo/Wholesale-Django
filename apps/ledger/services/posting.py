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
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Generic, Protocol, TypeVar

from django.contrib.auth.models import AbstractBaseUser
from django.db import models, transaction

from apps.ledger.models import JournalEntry, JournalType

if TYPE_CHECKING:
    from apps.catalog.models import Product
    from apps.core.models import Currency, TaxCode
    from apps.inventory.models import Warehouse
    from apps.ledger.models import Account
    from apps.parties.models import Customer, Vendor
    from apps.payments.models import MoneyAccount

SourceT = TypeVar("SourceT", bound=models.Model)


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
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.journal_type not in JournalType.values:
            raise ValueError(f"Unsupported journal type: {self.journal_type!r}")
        if not isinstance(self.exchange_rate, Decimal):
            raise TypeError("exchange_rate must be Decimal")
        if not self.lines:
            raise ValueError("A journal draft needs at least one line")


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
        if self.source.pk is None:
            raise ValueError("The posting source must be saved before posting")
        if not self.idempotency_key or len(self.idempotency_key) > 120:
            raise ValueError("idempotency_key must contain 1 to 120 characters")


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


# Useful for annotations on registries which select a builder by source type.
JournalBuilderFactory = Callable[[SourceT], JournalBuilder[SourceT]]
