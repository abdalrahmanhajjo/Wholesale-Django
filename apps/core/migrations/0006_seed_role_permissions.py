"""
Fill the seven role groups with permissions (ACC-003, BRD §4.1).

The groups were created by the reference-data seed but were empty shells — a
user in the Cashier group had no more rights than an anonymous visitor. This
migration assigns each group its baseline permission set from the access matrix.

Idempotent: it *sets* each group's permissions rather than adding to them, so
re-running restores the baseline. That also means an administrator's later
customisations are reset if this migration is ever re-applied — which only
happens on a fresh database, since Django records it as applied.
"""

from django.apps import apps as global_apps
from django.contrib.auth.management import create_permissions
from django.db import migrations

from apps.core.permissions import ALL_APPS, ROLE_MATRIX


def ensure_permissions_exist():
    """
    Django creates Permission rows from a `post_migrate` signal, which fires
    only after *every* migration has run. A data migration therefore sees an
    empty auth_permission table and silently assigns nothing — the groups end up
    as empty shells and every role check fails.

    Creating them up front is the documented way around it.
    """
    for app_config in global_apps.get_app_configs():
        app_config.models_module = True
        create_permissions(app_config, verbosity=0)
        app_config.models_module = None


def assign_permissions(apps, schema_editor):
    ensure_permissions_exist()

    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    RoleProfile = apps.get_model("accounts", "RoleProfile")

    def perms_for_apps(app_labels, actions):
        """add/change/delete/view on those apps, restricted to the given verbs."""
        return Permission.objects.filter(
            content_type__app_label__in=app_labels,
            codename__regex=r"^(" + "|".join(actions) + ")_",
        )

    for group_name, spec in ROLE_MATRIX.items():
        group, _ = Group.objects.get_or_create(name=group_name)
        granted = set()

        if spec.get("all_permissions"):
            granted.update(Permission.objects.values_list("id", flat=True))
        else:
            # Full CRUD on the apps this role owns.
            crud_apps = spec.get("crud_apps", [])
            if crud_apps:
                granted.update(
                    perms_for_apps(crud_apps, ["add", "change", "view"]).values_list(
                        "id", flat=True
                    )
                )
                # Delete is granted only on drafts, which is a state check the
                # services enforce (BR-004). The model permission itself is given
                # so the check has something to gate on.
                granted.update(
                    perms_for_apps(crud_apps, ["delete"]).values_list("id", flat=True)
                )

            # Read-only visibility elsewhere (BRD §4.1 "Visibility" column).
            view_apps = spec.get("view_apps", [])
            if view_apps:
                granted.update(
                    perms_for_apps(view_apps, ["view"]).values_list("id", flat=True)
                )

            # Named action permissions.
            actions = spec.get("actions", [])
            if actions:
                granted.update(
                    Permission.objects.filter(
                        content_type__app_label="core", codename__in=actions
                    ).values_list("id", flat=True)
                )

        group.permissions.set(list(granted))

        RoleProfile.objects.update_or_create(
            group=group,
            defaults={
                "description": spec.get("description", ""),
                "is_system": True,
                "can_post": spec.get("can_post", False),
                "can_approve": spec.get("can_approve", False),
                "can_reverse": spec.get("can_reverse", False),
                "can_close_period": spec.get("can_close_period", False),
                "can_configure": spec.get("can_configure", False),
            },
        )


def clear_permissions(apps, schema_editor):
    """Reversing empties the groups again but leaves them in place."""
    Group = apps.get_model("auth", "Group")
    for group_name in ROLE_MATRIX:
        Group.objects.filter(name=group_name).first() and Group.objects.get(
            name=group_name
        ).permissions.clear()


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0005_systempermission"),
        ("accounts", "0002_initial"),
        ("auth", "0012_alter_user_first_name_max_length"),
        # Every app whose model permissions we assign must exist first.
        ("ledger", "0003_posting_guards"),
        ("parties", "0001_initial"),
        ("catalog", "0002_initial"),
        ("inventory", "0002_initial"),
        ("sales", "0001_initial"),
        ("purchases", "0001_initial"),
        ("payments", "0001_initial"),
    ]

    operations = [migrations.RunPython(assign_permissions, clear_permissions)]


# Guard against a typo in ALL_APPS drifting from INSTALLED_APPS.
assert "core" in ALL_APPS
