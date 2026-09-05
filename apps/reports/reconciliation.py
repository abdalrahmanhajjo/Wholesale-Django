"""Subledger reconciliation (GL-011, RPT-021).

Every control account is backed by a subledger: accounts receivable by the open
customer invoices, accounts payable by the open vendor bills, inventory by the
stock balances. Those two numbers are maintained by different code paths - one
by the posting engine, one by the document workflows - and they are supposed to
agree. This is the screen that says whether they do.

A difference here is not a rounding curiosity. It means the ledger and the
subledger disagree about what the business is owed or owes, and every statement
built on either one is suspect until it is explained. That is why it is a step
in closing a period rather than a report someone runs when curious.

**These balances are "as things stand", not "as at a date".** The underlying
view compares today's control-account balance with today's open documents.
That is the right question for an integrity check - the two should agree at
every instant - but it does mean a difference found while closing an old period
may have been introduced last week rather than during the period being closed.
"""

from dataclasses import dataclass
from decimal import Decimal

from django.db import connection

ZERO = Decimal("0")

#: Anything smaller than this is presentation noise rather than a real
#: disagreement. Both sides are stored to four decimal places, so a genuine
#: difference cannot hide beneath it.
TOLERANCE = Decimal("0.0001")

CONTROL_LABELS = {
    "AR": "Accounts receivable",
    "AP": "Accounts payable",
    "INVENTORY": "Inventory",
}

SUBLEDGER_SOURCE = {
    "AR": "open customer invoices",
    "AP": "open vendor bills",
    "INVENTORY": "stock balances",
}


@dataclass(frozen=True, slots=True)
class ControlAccountCheck:
    control_type: str
    account_code: str
    gl_balance: Decimal
    subledger_balance: Decimal
    difference: Decimal

    @property
    def label(self) -> str:
        return CONTROL_LABELS.get(self.control_type, self.control_type)

    @property
    def source(self) -> str:
        return SUBLEDGER_SOURCE.get(self.control_type, "the subledger")

    @property
    def reconciles(self) -> bool:
        return abs(self.difference) <= TOLERANCE


def subledger_reconciliation() -> tuple[ControlAccountCheck, ...]:
    """Each control account beside the subledger that is supposed to explain it."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT control_type, account_code,
                   gl_balance_base, subledger_balance_base, difference_base
            FROM v_subledger_reconciliation
            ORDER BY control_type
            """
        )
        rows = cursor.fetchall()

    return tuple(
        ControlAccountCheck(
            control_type=row[0],
            account_code=row[1],
            gl_balance=Decimal(row[2] or 0),
            subledger_balance=Decimal(row[3] or 0),
            difference=Decimal(row[4] or 0),
        )
        for row in rows
    )


def unevaluated_control_types(checks=None) -> tuple[str, ...]:
    """Control types the view returned no row for at all.

    v_subledger_reconciliation builds each row from v_control_account_balance,
    which only has a row for a control account that some journal line has
    touched. So a control account with no ledger activity does not come back
    reconciling *or* differing - it simply is not there, and a screen that
    listed only what it returned would quietly imply everything was checked.

    Naming the gap is the honest half of a reconciliation: "agrees" and "was not
    examined" are very different statements to put in front of an accountant.

    Pass an already-fetched result to avoid a second round trip. The argument
    exists because the obvious call pattern - ask for the checks, then ask what
    was missing - otherwise queries the view twice for one answer.
    """
    if checks is None:
        checks = subledger_reconciliation()
    seen = {check.control_type for check in checks}
    return tuple(sorted(set(CONTROL_LABELS) - seen))
