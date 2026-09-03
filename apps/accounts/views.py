"""
Account request (ACC-001, ACC-003).

The one view outside authentication that an anonymous visitor may reach. It
creates a user who cannot sign in — see AccountRequestForm.save — so the worst
an abusive submission achieves is a row an administrator declines.
"""

from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.views.generic import CreateView, TemplateView

from apps.accounts.forms import AccountRequestForm
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
