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

from django.contrib.auth.mixins import AccessMixin, LoginRequiredMixin
from django.core.exceptions import PermissionDenied

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
