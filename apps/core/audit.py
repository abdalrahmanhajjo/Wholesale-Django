"""
Audit trail service (ACC-005, NFR-006, RPT-020).

ACC-005 asks for "user, timestamp, action, object and before/after values for
material changes", readable from the related record. This module is the single
place that writes those events, so no view has to remember the shape of an
AuditEvent — and so a change to what we record is one edit, not thirty.

Usage from a view:

    from apps.core import audit

    before = audit.snapshot(customer)          # before you mutate
    form.save()
    audit.record_update(request, customer, before)

Or let `AuditedFormMixin` (apps/core/mixins.py) do it for you, which is what the
CRUD views actually use.

The event table is append-only: the admin registers it read-only and nothing in
the application updates or deletes a row (NFR-017).
"""

import uuid
from datetime import date, datetime
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType

from apps.core.models import AuditAction, AuditEvent

#: Never recorded, whatever model they appear on.
SENSITIVE_FIELDS = frozenset({"password", "secret", "token", "api_key", "salt", "session_key"})

#: Noise: these change on every save and say nothing about intent.
IGNORED_FIELDS = frozenset({"updated_at", "created_at", "updated_by", "created_by"})


def _jsonify(value):
    """Make a field value safe for a JSONField and readable in a diff."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Decimal):
        # str, not float — BR-001 applies to the audit trail too. A float here
        # would show 0.1 + 0.2 problems in the history of a money field.
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return str(value)


def _normalise(field, value):
    """
    Bring a value to the precision the column actually stores.

    A Decimal that came from a form is `Decimal("7500")`; the same value read
    back from the database is `Decimal("7500.0000")`. Comparing them naively
    reports a change where none happened, so every save would litter the audit
    log with false "5000.0000 -> 5000" entries. Quantising to the field's own
    decimal_places makes before and after directly comparable.
    """
    if value is None:
        return None
    if isinstance(value, Decimal) and getattr(field, "decimal_places", None) is not None:
        return value.quantize(Decimal(1).scaleb(-field.decimal_places))
    return value


def snapshot(instance, fields=None):
    """
    Field values as a plain dict, ready to diff.

    Foreign keys are recorded by id (cheap, stable) rather than by str() — the
    related object's name may change later, and the audit trail should say what
    was actually stored.
    """
    data = {}
    for field in instance._meta.fields:
        name = field.name
        if name in IGNORED_FIELDS or name in SENSITIVE_FIELDS:
            continue
        if field.is_relation:
            data[name] = getattr(instance, field.attname)  # the _id value
        else:
            data[name] = _jsonify(_normalise(field, getattr(instance, name, None)))
    if fields is not None:
        data = {k: v for k, v in data.items() if k in fields}
    return data


def diff(before, after):
    """{"field": {"from": x, "to": y}} for the fields that actually changed."""
    changes = {}
    for key in set(before) | set(after):
        old, new = before.get(key), after.get(key)
        if old != new:
            changes[key] = {"from": _jsonify(old), "to": _jsonify(new)}
    return changes


def record(
    action,
    instance=None,
    user=None,
    changes=None,
    reason="",
    request=None,
    object_repr=None,
    correlation_id=None,
):
    """
    Write one audit event. Never raises into the caller's transaction path for a
    formatting problem — but it is inside the caller's `transaction.atomic()`, so
    if the business change rolls back, so does its audit event. That is
    deliberate: an audit trail that records things which did not happen is worse
    than none.
    """
    if request is not None and user is None:
        user = request.user if request.user.is_authenticated else None

    content_type = object_id = None
    if instance is not None and instance.pk is not None:
        # AuditEvent.object_id is a BigIntegerField, so only integer-primary-key
        # models can provide a generic relation. Natural-key models still retain
        # their actor, action, representation and complete field-level changes.
        if isinstance(instance.pk, int):
            content_type = ContentType.objects.get_for_model(instance)
            object_id = instance.pk

    resolved_repr = (
        object_repr
        if object_repr is not None
        else str(instance)
        if instance is not None
        else ""
    )

    return AuditEvent.objects.create(
        action=action,
        user=user,
        content_type=content_type,
        object_id=object_id,
        object_repr=resolved_repr[:255],
        changes=changes or None,
        reason=reason,
        correlation_id=correlation_id or _correlation_id(request),
        ip_address=_client_ip(request),
    )


def record_create(request, instance, reason="", *, user=None):
    return record(
        AuditAction.CREATE,
        instance,
        user=user,
        request=request,
        changes={"created": snapshot(instance)},
        reason=reason,
    )


def record_update(request, instance, before, reason=""):
    changes = diff(before, snapshot(instance))
    if not changes:
        return None  # a save that changed nothing is not a material change
    return record(
        AuditAction.UPDATE, instance, request=request, changes=changes, reason=reason
    )


def record_delete(request, instance, reason=""):
    return record(
        AuditAction.DELETE,
        instance,
        request=request,
        changes={"deleted": snapshot(instance)},
        reason=reason,
    )


def record_action(request, action, instance, reason="", *, user=None, **extra):
    """
    For the non-CRUD verbs — approve, post, reverse, allocate, close period.

    These are the ones ACC-005 and NFR-006 care about most: every approve, post,
    reverse and close must be attributable to a user and a time.
    """
    return record(
        action,
        instance,
        user=user,
        request=request,
        reason=reason,
        changes=extra or None,
    )


def record_export(request, description, row_count=None):
    """UX-007 / ACC-006: exports are recorded, including an auditor's."""
    return record(
        AuditAction.EXPORT,
        user=request.user if request.user.is_authenticated else None,
        request=request,
        object_repr=description[:255],
        changes={"rows": row_count} if row_count is not None else None,
    )


def _client_ip(request):
    if request is None:
        return None
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def _correlation_id(request):
    """
    One id per request, so every event a single action produced can be pulled
    together later (NFR-016). Posting a sales invoice writes several events;
    this is what ties them into one story.
    """
    if request is None:
        return None
    if not hasattr(request, "_audit_correlation_id"):
        request._audit_correlation_id = uuid.uuid4()
    return request._audit_correlation_id
