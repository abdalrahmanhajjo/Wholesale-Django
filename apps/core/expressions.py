"""
Small expression helpers shared across model constraints.

`NumNonnulls` is adopted from the colleague's SQL schema — writing
`num_nonnulls(a, b, c) = 1` is shorter, obviously correct, and easier to extend
than spelling out every combination of IS NULL / IS NOT NULL by hand.
"""

from django.db.models import F, Func, IntegerField, Value
from django.db.models.lookups import Exact, LessThanOrEqual


class NumNonnulls(Func):
    """PostgreSQL num_nonnulls(): counts how many of its arguments are not NULL."""

    function = "num_nonnulls"
    output_field = IntegerField()


def exactly_one(*field_names):
    """CHECK that exactly one of these columns is populated."""
    return Exact(NumNonnulls(*[F(n) for n in field_names]), Value(1))


def at_most_one(*field_names):
    """CHECK that no more than one of these columns is populated."""
    return LessThanOrEqual(NumNonnulls(*[F(n) for n in field_names]), Value(1))
