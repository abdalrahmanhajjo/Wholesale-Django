"""
View mixins for server-side authorisation.

Every member's views use these. The BRD is emphatic on the point (§4.1):

    "The system must authorize actions server-side through Django permissions
     and object-aware checks; hiding a button is not sufficient security."

and ACC-004's acceptance evidence is:

    "A user lacking a permission receives a denial even when calling the URL
     directly."

So a template that hides a button is presentation, never protection. The view
must refuse.

Usage
-----
    from apps.core.mixins import ActionPermissionMixin
    from apps.core.permissions import POST_SALES_INVOICE

    class SalesInvoicePostView(ActionPermissionMixin, View):
        required_permission = POST_SALES_INVOICE
        ...

For a plain function view, use `@require_action(POST_SALES_INVOICE)`.
"""

from functools import wraps

from django.contrib import messages
from django.contrib.auth.mixins import AccessMixin, LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import redirect
from django.urls import reverse

from apps.core import audit

#: HTTP methods that do not change state. Everything else is a write.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


class ReadOnlyGuardMixin(AccessMixin):
    """
    Blocks every write for a user flagged `is_read_only` (ACC-006).

    An auditor may hold `view` permissions across the whole system and still
    must never mutate anything. Relying on not granting them change permissions
    is fragile — one careless group edit and the guarantee is gone. This makes
    it a property of the account instead.
    """

    def dispatch(self, request, *args, **kwargs):
        user = request.user
        if (
            user.is_authenticated
            and getattr(user, "is_read_only", False)
            and request.method not in SAFE_METHODS
        ):
            raise PermissionDenied(
                "This account is read-only and cannot change data (ACC-006)."
            )
        return super().dispatch(request, *args, **kwargs)


class ActionPermissionMixin(LoginRequiredMixin, ReadOnlyGuardMixin):
    """
    Requires authentication, enforces the read-only flag, then checks a named
    permission.

    Set `required_permission` to a constant from apps.core.permissions, or
    `required_permissions` for several (all are required). Override
    `has_action_permission()` for object-aware checks — a Sales user who may
    only see their own customers, say.
    """

    required_permission = None
    required_permissions = ()
    #: Set False to check the permission on writes only, leaving reads open.
    enforce_on_safe_methods = True

    def get_required_permissions(self):
        perms = list(self.required_permissions)
        if self.required_permission:
            perms.append(self.required_permission)
        return perms

    def has_action_permission(self, request):
        perms = self.get_required_permissions()
        if not perms:
            return True
        if not self.enforce_on_safe_methods and request.method in SAFE_METHODS:
            return True
        return request.user.has_perms(perms)

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not self.has_action_permission(request):
            raise PermissionDenied(self.permission_denied_message())
        return super().dispatch(request, *args, **kwargs)

    def permission_denied_message(self):
        perms = ", ".join(self.get_required_permissions())
        # NFR-016 / UX-008: say which permission is missing, and nothing else.
        # No stack traces, no internals.
        return f"You do not have permission to perform this action ({perms})."


class PostingPermissionMixin(ActionPermissionMixin):
    """
    For views that post a document.

    Posting is the highest-impact action in the system: it writes journals and
    stock movements, and it cannot be undone except by a reversal (BR-004). This
    subclass exists so posting views are greppable and so a future requirement —
    re-authentication before posting, say (ACC-008) — lands in one place.
    """

    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)


class ConfirmationRequiredMixin(ActionPermissionMixin):
    """
    ACC-008: high-impact operations need explicit confirmation and a reason.

    Applies to closing or reopening a period and to reversing a posted document.
    The view must receive `confirm=yes` and a non-empty `reason`, and the reason
    goes into the audit event.
    """

    reason_field = "reason"
    confirm_field = "confirm"

    def get_confirmation_reason(self, request):
        if request.method in SAFE_METHODS:
            return None
        if request.POST.get(self.confirm_field) != "yes":
            raise PermissionDenied("This action must be explicitly confirmed (ACC-008).")
        reason = (request.POST.get(self.reason_field) or "").strip()
        if not reason:
            raise PermissionDenied("A reason is required for this action (ACC-008).")
        return reason


def require_action(*permissions):
    """
    Function-view equivalent of ActionPermissionMixin.

        @require_action(CLOSE_PERIOD)
        def close_period(request, pk): ...
    """

    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            user = request.user
            if not user.is_authenticated:
                raise PermissionDenied("Authentication required.")
            if getattr(user, "is_read_only", False) and request.method not in SAFE_METHODS:
                raise PermissionDenied(
                    "This account is read-only and cannot change data (ACC-006)."
                )
            if not user.has_perms(permissions):
                raise PermissionDenied(
                    "You do not have permission to perform this action "
                    f"({', '.join(permissions)})."
                )
            return view(request, *args, **kwargs)

        return wrapped

    return decorator


class AuditedFormMixin:
    """
    Saves the object and writes the ACC-005 audit event in one transaction.

    Doing it here rather than in each view means no screen can forget, and a
    failed save never leaves an audit event claiming a change that did not
    happen (BR-005).
    """

    audit_reason = ""

    def form_valid(self, form):
        # `self.object` is None on a CreateView and the loaded row on an
        # UpdateView. Never infer this from the pk: a model with a natural
        # primary key (Currency.code) already has one before it is ever saved.
        is_create = self.object is None
        # Re-read from the database rather than snapshotting `self.object` —
        # the form has already applied cleaned_data to that instance, so it
        # holds the *after* values by the time we get here.
        before = (
            None if is_create else audit.snapshot(self.model.objects.get(pk=self.object.pk))
        )

        with transaction.atomic():
            form.instance.updated_by = self.request.user
            if is_create:
                form.instance.created_by = self.request.user
            self.object = form.save()

            if is_create:
                audit.record_create(self.request, self.object, reason=self.audit_reason)
                messages.success(self.request, f"{self.object} created.")
            else:
                event = audit.record_update(
                    self.request, self.object, before, reason=self.audit_reason
                )
                if event:
                    changed = ", ".join(event.changes.keys())
                    messages.success(self.request, f"{self.object} updated ({changed}).")
                else:
                    messages.info(self.request, "No changes to save.")

        return redirect(self.get_success_url())

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        form = ctx["form"]
        # PTY-007 soft warnings, only once the form has been validated.
        ctx["duplicate_warnings"] = (
            form.duplicate_warnings()
            if hasattr(form, "duplicate_warnings") and form.is_bound and form.is_valid()
            else []
        )
        return ctx


class BackLinkMixin:
    """
    Supplies the return arrow that base.html renders above the page heading.

    Declared rather than hand-written per template, so a screen with no obvious
    way back is a missing attribute rather than something nobody noticed. Two
    forms:

        back_url_name = "parties:customer_list"   # a named route
        back_to_object = True                     # the record being edited

    `back_label` names the destination. "Back" on its own is useless to anyone
    reading a list of links out of context, so it is always a real place.
    """

    back_url_name = None
    back_to_object = False
    back_label = "Back"

    def get_back_url(self):
        if self.back_to_object:
            obj = getattr(self, "object", None)
            if obj is not None and hasattr(obj, "get_absolute_url"):
                return obj.get_absolute_url()
        if self.back_url_name:
            return reverse(self.back_url_name)
        return None

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        # setdefault, so a view with a more specific answer keeps it.
        ctx.setdefault("back_url", self.get_back_url())
        ctx.setdefault("back_label", self.back_label)
        return ctx
