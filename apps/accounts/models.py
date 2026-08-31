"""
Users and role-aware access.

BRD coverage: ACC-001..ACC-008, NFR-005, NFR-006, BRD 4.1 access matrix.
Roles are Django Groups, not hard-coded logic (BRD 4.1 "Control principle").
"""

from django.contrib.auth.models import AbstractUser, Group
from django.db import models
from django.db.models import Q


class User(AbstractUser):
    """
    Custom user so the project can extend identity without a painful migration
    later. AUTH_USER_MODEL = "accounts.User".
    """

    email = models.EmailField(unique=True)
    full_name = models.CharField(max_length=150, blank=True)
    phone = models.CharField(max_length=50, blank=True)
    job_title = models.CharField(max_length=100, blank=True)

    # ACC-002 lifecycle
    deactivated_at = models.DateTimeField(null=True, blank=True)
    deactivated_reason = models.CharField(max_length=255, blank=True)
    must_change_password = models.BooleanField(default=False)
    last_password_change = models.DateTimeField(null=True, blank=True)

    # ACC-006: an auditor may read and export but never mutate.
    is_read_only = models.BooleanField(
        default=False, help_text="Auditor mode: blocks all write actions server-side."
    )

    # Scoping hooks for object-aware checks (BRD 4.1 "Visibility" column).
    default_warehouse = models.ForeignKey(
        "inventory.Warehouse", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+",
    )

    class Meta:
        db_table = "app_user"
        constraints = [
            models.CheckConstraint(
                condition=Q(is_active=True) | Q(deactivated_at__isnull=False),
                name="user_inactive_has_timestamp",
            )
        ]
        indexes = [models.Index(fields=["is_active"], name="ix_user_active")]

    def __str__(self):
        return self.full_name or self.username


class RoleProfile(models.Model):
    """
    Optional metadata on a Group so the baseline access matrix in BRD 4.1 is
    documented in data rather than in code comments.
    """

    group = models.OneToOneField(Group, on_delete=models.CASCADE, related_name="profile")
    description = models.TextField(blank=True)
    is_system = models.BooleanField(
        default=False, help_text="System roles cannot be deleted through the UI."
    )
    can_post = models.BooleanField(default=False)
    can_approve = models.BooleanField(default=False)
    can_reverse = models.BooleanField(default=False)
    can_close_period = models.BooleanField(default=False)
    can_configure = models.BooleanField(default=False)

    class Meta:
        db_table = "role_profile"

    def __str__(self):
        return self.group.name
