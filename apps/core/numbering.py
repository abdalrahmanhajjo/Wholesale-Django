"""
Document number allocation (CFG-008, NFR-008).

One place that hands out document numbers, so every module numbers documents
the same way and no two documents can ever receive the same number.

Usage:

    from apps.core import numbering
    from apps.core.models import DocumentType

    with transaction.atomic():
        invoice.number = numbering.next_number(DocumentType.SALES_INVOICE, invoice.doc_date)
        invoice.save()
"""

from django.db import transaction
from django.utils import timezone

from apps.core.models import DocumentSequence, SequenceReset


class SequenceNotConfigured(Exception):
    """No active DocumentSequence row exists for this document type and series."""


def _period_key(sequence, on_date):
    """
    Which counting period this date falls in, for the sequence's reset policy.

    The counter restarts whenever this value changes, so it is what makes
    "restart numbering each year" work.
    """
    if sequence.reset_policy == SequenceReset.MONTHLY:
        return f"{on_date:%Y-%m}"
    if sequence.reset_policy == SequenceReset.YEARLY:
        return f"{on_date:%Y}"
    return ""


@transaction.atomic
def next_number(document_type, on_date=None, series="DEFAULT"):
    """
    Allocate and format the next number for this document type.

    Locks the sequence row for the life of the caller's transaction, so two
    concurrent callers cannot be handed the same number (NFR-008).
    """
    on_date = on_date or timezone.localdate()

    try:
        sequence = DocumentSequence.objects.select_for_update().get(
            document_type=document_type, series=series, is_active=True
        )
    except DocumentSequence.DoesNotExist as exc:
        raise SequenceNotConfigured(
            f"No active number series for {document_type} / {series}. "
            f"Add one in Settings before creating this document."
        ) from exc

    period_key = _period_key(sequence, on_date)
    if sequence.period_key and period_key != sequence.period_key:
        sequence.next_number = 1
    sequence.period_key = period_key

    number = sequence.next_number
    sequence.next_number = number + 1
    sequence.save(update_fields=["next_number", "period_key"])

    return f"{sequence.prefix}{number:0{sequence.padding}d}{sequence.suffix}"
