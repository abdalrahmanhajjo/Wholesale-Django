"""
Root URL configuration.

Member 1 owns this file. Other members add one `include()` line for their app
and keep their own routes in their app's urls.py — see CONTRIBUTING.md.
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from apps.core import views as core_views

urlpatterns = [
    path("", core_views.dashboard, name="dashboard"),
    # ACC-001: Django's session authentication, with our own templates.
    path(
        "login/", auth_views.LoginView.as_view(redirect_authenticated_user=True), name="login"
    ),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path(
        "password-change/",
        auth_views.PasswordChangeView.as_view(success_url="/"),
        name="password_change",
    ),
    path("parties/", include("apps.parties.urls")),
    path("purchases/", include("apps.purchases.urls")),
    path("inventory/", include("apps.inventory.urls")),
    path("admin/", admin.site.urls),
    # Members 3-4 add their includes here:
    # path("sales/", include("apps.sales.urls")),
    # path("payments/", include("apps.payments.urls")),
    path("sales/", include("apps.sales.urls")),
    path("payments/", include("apps.payments.urls")),
    path("settings/", include("apps.core.urls")),
    path("admin/", admin.site.urls),
    # Member 2 adds the purchasing routes here when their screens are ready:
    # path("purchases/", include("apps.purchases.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
