"""
Grant the sales-return permissions to the roles that hold them (ACC-003).

`0006_seed_role_permissions` filled the groups from ROLE_MATRIX, but it has
already been applied on every database — editing the matrix alone changes
nothing for groups that already exist. This migration adds the two new
permissions additively, so an administrator's later customisations survive.
"""

from django.apps import apps as global_apps
from django.contrib.auth.management import create_permissions
from django.db import migrations

from apps.core.permissions import ACCOUNTANT, OWNER_ADMIN

CODENAMES = ["post_sales_return", "approve_sales_return"]
ROLES = [OWNER_ADMIN, ACCOUNTANT]


def ensure_permissions_exist():
    """
    Permission rows are created by a post_migrate signal, which fires only after
    every migration has run — so a data migration would otherwise find nothing
    to assign. Same workaround as migration 0006.
    """
    for app_config in global_apps.get_app_configs():
        app_config.models_module = True
        create_permissions(app_config, verbosity=0)
        app_config.models_module = None


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
    dependencies = [("core", "0009_alter_systempermission_options")]
    operations = [migrations.RunPython(grant, revoke)]