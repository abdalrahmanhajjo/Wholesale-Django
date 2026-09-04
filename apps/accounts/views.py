"""
Account request (ACC-001, ACC-003).

The one view outside authentication that an anonymous visitor may reach. It
creates a user who cannot sign in — see AccountRequestForm.save — so the worst
an abusive submission achieves is a row an administrator declines.
"""

from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import LoginView
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.views.generic import CreateView, TemplateView

from apps.accounts.forms import AccountRequestForm, SignInForm
from apps.accounts.models import User
from apps.core import audit


class AccountRequestView(CreateView):
    """Create an inactive account, and say what happens next."""

    model = User
    form_class = AccountRequestForm
    template_name = "registration/signup.html"
    success_url = reverse_lazy("signup_done")

    def dispatch(self, request, *args, **kwargs):
        # Someone already signed in has an account; sending them here would only
        # invite creating a second one under their own nose.
        if request.user.is_authenticated:
            messages.info(request, "You are already signed in.")
            return redirect("dashboard")
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        response = super().form_valid(form)
        # ACC-005: an account appearing is something an auditor will ask about.
        # Recorded with no actor, because there was not one.
        audit.record_action(
            self.request,
            audit.AuditAction.CREATE,
            self.object,
            reason="Self-service account request, pending approval",
            user=None,
        )
        return response


class AccountRequestDoneView(TemplateView):
    """What happens next, on its own page so a refresh cannot resubmit."""

    template_name = "registration/signup_done.html"


class SignInView(LoginView):
    """Sign in, honouring the "keep me signed in" choice.

    Unchecked means the session ends when the browser does. Checked keeps the
    project's configured SESSION_COOKIE_AGE. Nothing else about authentication
    changes: Django's LoginView still does the work.
    """

    template_name = "registration/login.html"
    authentication_form = SignInForm
    redirect_authenticated_user = True

    def form_valid(self, form):
        response = super().form_valid(form)
        if not form.cleaned_data.get("remember_me"):
            self.request.session.set_expiry(0)
        return response


class PasswordChangeView(auth_views.PasswordChangeView):
    """Change your own password (ACC-002).

    The route existed and pointed at a template that was never written, so
    every signed-in user who followed it got a server error. It also sent
    people back to the dashboard with no confirmation that anything had
    happened; it now ends on a page that says so.
    """

    template_name = "registration/password_change_form.html"
    success_url = reverse_lazy("password_change_done")


class PasswordChangeDoneView(LoginRequiredMixin, TemplateView):
    template_name = "registration/password_change_done.html"
