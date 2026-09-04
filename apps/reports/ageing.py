"""Receivables and payables ageing (RPT-006, RPT-007).

How old is the money owed to us, and by whom - and the same question on the
payables side. Both come from `fn_ar_ageing` / `fn_ap_ageing`, which recompute
each document's settled amount from the allocation history at the chosen date
rather than reading the cached `open_txn` column. That is what makes an ageing
"as at last month end" mean anything: the cached column only knows about now.

**These reports are single-currency on purpose.**

The functions return each document in its own currency, with no base-currency
column. Adding 100 USD to 100 EUR and printing 200 would be worse than useless
on a report whose whole job is to be totalled and chased, so the currency is a
filter rather than a column: pick one, and every figure on the page is in it.
Anything open in another currency is counted and named underneath, so choosing
a currency never hides money.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.db import connection

ZERO = Decimal("0")

#: Ordered oldest-last, which is how an ageing is read and totalled.
BUCKETS = ("CURRENT", "1-30", "31-60", "61-90", "90+")

BUCKET_LABELS = {
    "CURRENT": "Not yet due",
    "1-30": "1–30 days",
    "31-60": "31–60 days",
    "61-90": "61–90 days",
    "90+": "Over 90 days",
}

AR, AP = "AR", "AP"

#: The statement is stored whole rather than assembled from a function name.
#: Interpolating even a trusted name means the next reader has to prove where it
#: came from, and a `noqa` on the line keeps the linter quiet while they do.
#: Two literals cannot be injected into at all.
_SIDE = {
    AR: {
        "sql": "SELECT * FROM fn_ar_ageing(%s)",
        "party_label": "Customer",
        "noun": "customers",
        "title": "Receivables ageing",
        "detail_url": "sales:invoice_detail",
        "party_url": "parties:customer_detail",
    },
    AP: {
        "sql": "SELECT * FROM fn_ap_ageing(%s)",
        "party_label": "Vendor",
        "noun": "vendors",
        "title": "Payables ageing",
        "detail_url": "purchases:bill_detail",
        "party_url": "parties:vendor_detail",
    },
}


@dataclass(frozen=True, slots=True)
class BucketCell:
    """One bucket's amount, carrying its own label.

    Reports render these by iterating rather than by looking a dictionary up
    from the template. A dict lookup needs a custom filter, and a custom filter
    that takes an arbitrary key is a small hole in the template layer that
    tends to get reused for less innocent things.
    """

    key: str
    label: str
    amount: Decimal

    @property
    def is_empty(self) -> bool:
        return self.amount == ZERO


def _cells(amounts: dict) -> tuple[BucketCell, ...]:
    return tuple(
        BucketCell(key=bucket, label=BUCKET_LABELS[bucket], amount=amounts.get(bucket, ZERO))
        for bucket in BUCKETS
    )


@dataclass(frozen=True, slots=True)
class AgeingItem:
    """One open document."""

    party_id: int
    party_name: str
    document_id: int
    document_number: str
    currency_code: str
    document_date: date
    due_date: date | None
    days_overdue: int
    total: Decimal
    settled: Decimal
    open_amount: Decimal
    bucket: str

    @property
    def is_overdue(self) -> bool:
        return self.bucket != "CURRENT"

    @property
    def is_part_paid(self) -> bool:
        return self.settled > ZERO

    @property
    def bucket_cells(self) -> tuple[BucketCell, ...]:
        """This document's amount, in its own column and blank in the others."""
        return _cells({self.bucket: self.open_amount})


@dataclass(frozen=True, slots=True)
class AgeingParty:
    """One customer or vendor, their documents, and their bucket totals."""

    party_id: int
    party_name: str
    items: tuple[AgeingItem, ...]
    buckets: dict
    total: Decimal

    @property
    def overdue(self) -> Decimal:
        return self.total - self.buckets.get("CURRENT", ZERO)

    @property
    def bucket_cells(self) -> tuple[BucketCell, ...]:
        return _cells(self.buckets)


@dataclass(frozen=True, slots=True)
class AgeingReport:
    side: str
    as_of: date
    currency_code: str
    parties: tuple[AgeingParty, ...]
    buckets: dict
    total: Decimal
    #: Currencies with open documents that this run excluded, so that choosing
    #: a currency never quietly hides money.
    other_currencies: tuple[tuple[str, int], ...]

    @property
    def config(self) -> dict:
        return _SIDE[self.side]

    @property
    def title(self) -> str:
        return self.config["title"]

    @property
    def party_label(self) -> str:
        return self.config["party_label"]

    @property
    def overdue(self) -> Decimal:
        return self.total - self.buckets.get("CURRENT", ZERO)

    @property
    def bucket_cells(self) -> tuple[BucketCell, ...]:
        return _cells(self.buckets)

    @property
    def overdue_percent(self) -> Decimal | None:
        if not self.total:
            return None
        return (self.overdue / self.total * 100).quantize(Decimal("0.1"))


def _rows(side: str, as_of: date) -> list[AgeingItem]:
    with connection.cursor() as cursor:
        cursor.execute(_SIDE[side]["sql"], [as_of])
        return [
            AgeingItem(
                party_id=row[0],
                party_name=row[1],
                document_id=row[2],
                document_number=row[3],
                currency_code=row[4],
                document_date=row[5],
                due_date=row[6],
                days_overdue=row[7],
                total=Decimal(row[8] or 0),
                settled=Decimal(row[9] or 0),
                open_amount=Decimal(row[10] or 0),
                bucket=row[11],
            )
            for row in cursor.fetchall()
        ]


def ageing(side: str, as_of: date, currency_code: str) -> AgeingReport:
    """Open documents at a date, bucketed by how overdue they are."""
    if side not in _SIDE:
        raise ValueError(f"Unknown ageing side: {side!r}")

    everything = _rows(side, as_of)
    wanted = [item for item in everything if item.currency_code == currency_code]

    excluded: dict[str, int] = {}
    for item in everything:
        if item.currency_code != currency_code:
            excluded[item.currency_code] = excluded.get(item.currency_code, 0) + 1

    by_party: dict[int, list[AgeingItem]] = {}
    for item in wanted:
        by_party.setdefault(item.party_id, []).append(item)

    parties = []
    for items in by_party.values():
        buckets = {bucket: ZERO for bucket in BUCKETS}
        for item in items:
            buckets[item.bucket] = buckets.get(item.bucket, ZERO) + item.open_amount
        parties.append(
            AgeingParty(
                party_id=items[0].party_id,
                party_name=items[0].party_name,
                items=tuple(items),
                buckets=buckets,
                total=sum((item.open_amount for item in items), ZERO),
            )
        )
    # Worst debt first: an ageing is a work list, not an alphabetical index.
    parties.sort(key=lambda party: (-party.overdue, -party.total, party.party_name))

    totals = {bucket: ZERO for bucket in BUCKETS}
    for party in parties:
        for bucket, amount in party.buckets.items():
            totals[bucket] = totals.get(bucket, ZERO) + amount

    return AgeingReport(
        side=side,
        as_of=as_of,
        currency_code=currency_code,
        parties=tuple(parties),
        buckets=totals,
        total=sum(totals.values(), ZERO),
        other_currencies=tuple(sorted(excluded.items())),
    )
