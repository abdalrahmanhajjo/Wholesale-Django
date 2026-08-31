"""Users and roles in the admin (ACC-002, ACC-003)."""

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.models import Group

from apps.accounts.models import RoleProfile, User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    list_display = ("username", "full_name", "email", "is_active", "is_read_only", "is_staff")
    list_filter = ("is_active", "is_read_only", "is_staff", "groups")
    search_fields = ("username", "full_name", "email")
    fieldsets = DjangoUserAdmin.fieldsets + (
        (
            "Business profile",
            {
                "fields": (
                    "full_name",
                    "phone",
                    "job_title",
                    "default_warehouse",
                    "is_read_only",
                    "must_change_password",
                    "deactivated_at",
                    "deactivated_reason",
                )
            },
        ),
    )
    add_fieldsets = DjangoUserAdmin.add_fieldsets + (
        ("Business profile", {"fields": ("email", "full_name")}),
    )


class RoleProfileInline(admin.StackedInline):
    model = RoleProfile
    can_delete = False
    extra = 0


class GroupAdmin(admin.ModelAdmin):
    """
    Roles are Django Groups (BRD 4.1 "Control principle" — permissions are data,
    not hard-coded role logic). The inline shows what the group is for.
    """

    list_display = ("name", "_description", "_permission_count")
    inlines = [RoleProfileInline]
    search_fields = ("name",)
    filter_horizontal = ("permissions",)

    @admin.display(description="Description")
    def _description(self, obj):
        profile = getattr(obj, "profile", None)
        return profile.description if profile else ""

    @admin.display(description="Permissions")
    def _permission_count(self, obj):
        return obj.permissions.count()


admin.site.unregister(Group)
admin.site.register(Group, GroupAdmin)

admin.site.site_header = "Wholesale Accounting & BMS"
admin.site.site_title = "Wholesale Accounting"
admin.site.index_title = "Internal configuration"
