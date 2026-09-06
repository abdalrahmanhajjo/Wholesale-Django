"""
Read-only JSON endpoints that the form layer calls while someone is typing.

Three of them, all GET, all authenticated, all permission-checked against the
same permission the corresponding list screen requires. That last point is the
important one: a search endpoint without it would be a way to enumerate
customers without being allowed to see the customer list.

Nothing here writes. The server still validates every submission independently —
`check` exists so the browser can say the same thing sooner, never so the form
can skip the real check.
"""

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_GET

from apps.core.suggest import MIN_QUERY, build_registry


def _optional_id(value):
    """A row id from the query string, or None if it is not one.

    These parameters reach the ORM as `pk=` / `*_id=` lookups, and Django
    raises ValueError - not a validation error it would turn into a 400 - when
    the value is not a number. `?exclude=abc` therefore produced a 500 rather
    than being ignored. There is no injection here, since the ORM refuses the
    value long before any SQL is built; it is a robustness gap, and a 500 is
    both a worse answer and noise in the logs.
    """
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


_REGISTRY = None


def registry():
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = build_registry()
    return _REGISTRY


def _suggester(request, kind):
    try:
        suggester = registry()[kind]
    except KeyError:
        raise Http404(f"No suggester for {kind!r}") from None
    if not request.user.has_perm(suggester.permission):
        # The same refusal the list screen would give. Never "no results",
        # which would leak whether records exist.
        raise PermissionDenied(f"You do not have permission to search {kind}.")
    return suggester


@login_required
@require_GET
def suggest(request, kind):
    """Records matching `?q=`, each with a line of context."""
    suggester = _suggester(request, kind)
    term = (request.GET.get("q") or "").strip()
    if len(term) < MIN_QUERY:
        # An empty box offers the first page rather than nothing, so the field
        # is still usable by someone who does not know what to type.
        rows = suggester.queryset()[: MIN_QUERY + 7]
    else:
        rows = suggester.search(term)
    return JsonResponse({"results": [suggester.as_option(obj) for obj in rows]})


@login_required
@require_GET
def prefill(request, kind, pk):
    """What the rest of the form should adopt now this record is chosen."""
    suggester = _suggester(request, kind)
    if suggester.prefill is None:
        return JsonResponse({"values": {}, "notices": []})
    # `pk` arrives as a string because some of these records are keyed by a
    # code rather than a number (a currency, for instance). A number-keyed
    # model given a non-numeric one raises rather than missing, so the lookup
    # failing for that reason is still just "no such record".
    try:
        obj = get_object_or_404(suggester.queryset(), pk=pk)
    except (ValueError, ValidationError) as exc:
        raise Http404("No such record.") from exc
    return JsonResponse(suggester.prefill(obj))


@login_required
@require_GET
def check(request):
    """
    Business rules the browser can ask about before the round trip.

    `?rule=<name>` plus whatever that rule needs. Each returns a level and a
    sentence: "ok", "warning" (proceed, but know this) or "error" (this will be
    refused). The wording is the message the user sees, so it is written here
    rather than assembled in JavaScript.
    """
    rule = request.GET.get("rule") or ""
    handler = CHECKS.get(rule)
    if handler is None:
        raise Http404(f"No such check: {rule!r}")
    return JsonResponse(handler(request))


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------
def _ok(text=""):
    return {"level": "ok", "text": text}


def _party_code_free(request, model, permission, label):
    if not request.user.has_perm(permission):
        raise PermissionDenied("You do not have permission to check this.")
    code = (request.GET.get("value") or "").strip()
    if not code:
        return _ok()
    exclude = _optional_id(request.GET.get("exclude"))
    clash = model.objects.filter(code__iexact=code)
    if exclude:
        clash = clash.exclude(pk=exclude)
    other = clash.first()
    if other:
        return {
            "level": "error",
            "text": (
                f"Code “{code}” already belongs to {other.name}. "
                "Codes are unique, and case does not make them different."
            ),
        }
    return _ok(f"“{code}” is free.")


def customer_code(request):
    from apps.parties.models import Customer

    return _party_code_free(request, Customer, "parties.view_customer", "customer")


def vendor_code(request):
    from apps.parties.models import Vendor

    return _party_code_free(request, Vendor, "parties.view_vendor", "vendor")


def _similar_name(request, model, permission, noun):
    """
    PTY-007: a similar name is a warning, never a block. Two real companies can
    trade under names that look alike, so this says what it found and lets the
    person decide.
    """
    if not request.user.has_perm(permission):
        raise PermissionDenied("You do not have permission to check this.")

    name = (request.GET.get("value") or "").strip()
    if len(name) < 4:
        return _ok()
    exclude = _optional_id(request.GET.get("exclude"))
    matches = model.objects.filter(name__icontains=name)
    if exclude:
        matches = matches.exclude(pk=exclude)
    found = list(matches[:3])
    if not found:
        return _ok("No existing record with a name like this.")
    listed = ", ".join(f"{o.code} — {o.name}" for o in found)
    return {
        "level": "warning",
        "text": (
            f"Similar {noun} already on file: {listed}. "
            "Continue if this is a different company."
        ),
    }


def similar_customer_name(request):
    from apps.parties.models import Customer

    return _similar_name(request, Customer, "parties.view_customer", "customer")


def similar_vendor_name(request):
    from apps.parties.models import Vendor

    return _similar_name(request, Vendor, "parties.view_vendor", "vendor")


def stock_available(request):
    """
    Warn, do not block. Whether negative stock is allowed is a per-warehouse
    policy the posting engine enforces (BR-017); this only tells the person
    entering the line what they are about to ask for.
    """
    from apps.inventory.models import StockBalance

    if not request.user.has_perm("inventory.view_stockbalance"):
        raise PermissionDenied("You do not have permission to check stock.")
    product = _optional_id(request.GET.get("product"))
    warehouse = _optional_id(request.GET.get("warehouse"))
    wanted = request.GET.get("value")
    if not (product and warehouse and wanted):
        return _ok()
    try:
        wanted_qty = float(wanted)
    except (TypeError, ValueError):
        return _ok()

    row = StockBalance.objects.filter(product_id=product, warehouse_id=warehouse).first()
    on_hand = float(row.quantity_on_hand) if row else 0.0
    if wanted_qty <= on_hand:
        return _ok(f"{on_hand:,.4g} in stock.")
    return {
        "level": "warning",
        "text": (
            f"Only {on_hand:,.4g} in stock at this warehouse, and {wanted_qty:,.4g} "
            "is being ordered. The shortfall has to be received before it can be delivered."
        ),
    }


CHECKS = {
    "customer-code": customer_code,
    "vendor-code": vendor_code,
    "similar-customer-name": similar_customer_name,
    "similar-vendor-name": similar_vendor_name,
    "stock": stock_available,
}
