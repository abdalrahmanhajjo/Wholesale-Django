"""
Grant the sales credit-note approve permission to the roles that hold it
(ACC-003).

`0006_seed_role_permissions` filled the groups from ROLE_MATRIX, but it has
already been applied on every database — editing the matrix alone changes
nothing for groups that already exist. This migration adds the new permission
additively, so an administrator's later customisations survive.
"""

from django.apps import apps as global_apps
from django.contrib.auth.management import create_permissions
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.db import migrations

from apps.core.permissions import ACCOUNTANT, OWNER_ADMIN

CODENAMES = ["approve_sales_credit_note"]
ROLES = [OWNER_ADMIN, ACCOUNTANT]


def ensure_permissions_exist():
    """
    Permission rows are created by a post_migrate signal, which fires only after
    every migration has run — so a data migration would otherwise find nothing
    to assign. Same workaround as migration 0006.

    create_permissions only materialises codenames present in
    SystemPermission.Meta.permissions (ACTION_PERMISSIONS), and this permission
    is new there, so additionally get_or_create the row directly to guarantee
    the grant below has something to assign.
    """
    for app_config in global_apps.get_app_configs():
        app_config.models_module = True
        create_permissions(app_config, verbosity=0)
        app_config.models_module = None
    content_type = ContentType.objects.get_for_model(
        global_apps.get_model("core", "SystemPermission")
    )
    for codename in CODENAMES:
        Permission.objects.get_or_create(
            codename=codename,
            content_type=content_type,
            defaults={"name": "Can approve a sales credit note"},
        )


def grant(apps, schema_editor):
    ensure_permissions_exist()
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    permissions = Permission.objects.filter(
        codename__in=CODENAMES, content_type__app_label="core"
    )
    for name in ROLES:
        group = Group.objects.filter(name=name).first()
        if group is not None:
            group.permissions.add(*permissions)


def revoke(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    permissions = Permission.objects.filter(
        codename__in=CODENAMES, content_type__app_label="core"
    )
    for name in ROLES:
        group = Group.objects.filter(name=name).first()
        if group is not None:
            group.permissions.remove(*permissions)


class Migration(migrations.Migration):
    dependencies = [("core", "0012_company_logo_size_limit")]
    operations = [migrations.RunPython(grant, revoke)]
