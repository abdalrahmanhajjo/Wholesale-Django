"""
Context available to every template.

`base_currency` was previously supplied by the dashboard view alone, so the
sidebar and footer — which render on every page — fell back to the literal
"USD" everywhere else. On a company whose base currency is not USD, every screen
but one stated the wrong currency.

It is cached because it changes roughly never and is read on every request.
"""

from django.core.cache import cache

CACHE_KEY = "company_identity_v1"
CACHE_SECONDS = 300


def company(request):
    identity = cache.get(CACHE_KEY)
    if identity is None:
        from apps.core.models import Company

        row = Company.objects.only("name", "base_currency").first()
        identity = {
            "company_name": row.name if row else "Atlas Wholesale",
            "base_currency": (row.base_currency_id if row else "") or "",
        }
        # The sidebar's company badge used to be the literal "AW", which was
        # right for one installation and wrong for every other.
        identity["company_initials"] = _initials(identity["company_name"])
        cache.set(CACHE_KEY, identity, CACHE_SECONDS)
    return identity


def _initials(name: str) -> str:
    """Up to two letters for the company badge.

    Skips the words that appear in half of all company names and identify
    none of them, so "The Wholesale Company Ltd" reads WC rather than TW.
    """
    noise = {
        "the",
        "a",
        "an",
        "and",
        "of",
        "ltd",
        "limited",
        "llc",
        "inc",
        "co",
        "company",
        "corp",
        "corporation",
        "gmbh",
        "plc",
        "sa",
        "bv",
    }
    words = [
        w
        for w in "".join(c if c.isalnum() or c.isspace() else " " for c in name).split()
        if w.lower() not in noise
    ]
    if not words:
        words = name.split() or ["?"]
    letters = "".join(w[0] for w in words[:2]).upper()
    return letters or "?"


def navigation(request):
    """The sidebar, resolved for this user and this page.

    Built per request rather than cached: it depends on who is asking and where
    they are, and it is a few dozen attribute reads on a tuple of frozen
    dataclasses - cheaper than the cache lookup would be.
    """
    from apps.core.navigation import SECTIONS

    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {"nav_sections": ()}

    match = getattr(request, "resolver_match", None)
    url_name = getattr(match, "url_name", "") or ""
    app_name = getattr(match, "app_name", "") or ""
    path = request.path
    perms = _permission_set(user)

    sections = []
    for section in SECTIONS:
        items = _visible(section.items, perms, user, url_name, app_name, path)
        subgroups = [
            {"label": sub.label, "items": rows}
            for sub in section.subgroups
            if (rows := _visible(sub.items, perms, user, url_name, app_name, path))
        ]
        if not items and not subgroups:
            continue  # every row filtered out; the header would label nothing
        rows = items + [row for group in subgroups for row in group["items"]]
        sections.append(
            {
                "key": section.key,
                "label": section.label,
                "icon": section.icon,
                "items": items,
                "subgroups": subgroups,
                "collapsible": section.collapsible,
                # Where the rail sends someone with no JavaScript: the first row
                # they are allowed to see. Permission filtering has already run,
                # so this is never a door they cannot open.
                "first_url_name": rows[0]["url_name"] if rows else "",
                "count": len(rows),
                # A collapsed section that holds the current page would hide it,
                # so a section containing the active row starts open.
                "has_active": any(row["active"] for row in items)
                or any(r["active"] for g in subgroups for r in g["items"]),
            }
        )
    # Which module the panel opens on. Normally the one holding the current
    # page; on a page the menu does not list - a detail screen reached from a
    # link, say - nothing would be active and every panel would be hidden, so
    # the first module stands in. The sidebar is never blank.
    if sections and not any(section["has_active"] for section in sections):
        sections[0]["has_active"] = True
    return {"nav_sections": sections}


def _permission_set(user):
    """All of the user's permissions, in one query rather than one per row."""
    if user.is_superuser:
        return _AllPermissions()
    return user.get_all_permissions()


class _AllPermissions:
    """A superuser holds every permission; asking the database is pointless."""

    def __contains__(self, _item):
        return True


def _visible(items, perms, user, url_name, app_name, path):
    return [
        {
            "label": item.label,
            "url_name": item.url_name,
            "icon": item.icon,
            "active": item.is_active(url_name, app_name, path),
        }
        for item in items
        if item.visible_to(perms, user)
    ]
