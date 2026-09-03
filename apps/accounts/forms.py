"""
Account request form (ACC-001, ACC-003).

A wholesale ledger is not a product people should be able to join. Anyone who
reaches the sign-in page can request an account here, but the account that is
created has no role, no permissions and cannot sign in: it is inactive until an
administrator activates it and puts it in a group. That is the difference
between a request and a registration, and it is the whole reason this form is
safe to expose.
"""

from django import forms
from django.contrib.auth.password_validation import validate_password
from django.utils import timezone

from apps.accounts.models import User
from apps.core.form_ui import UIFormMixin


class AccountRequestForm(UIFormMixin, forms.ModelForm):
    """Creates a user who cannot yet sign in."""

    placeholders = {
        "username": "How you'll sign in, e.g. a.hajjo",
        "full_name": "Your name as it should appear on records",
        "email": "name@company.com",
        "job_title": "What you do here, e.g. Accounts payable",
    }

    password1 = forms.CharField(
        label="Password",
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password", "class": "field"}),
    )
    password2 = forms.CharField(
        label="Confirm password",
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password", "class": "field"}),
        help_text="Typed twice, because a password nobody can reproduce is a lockout.",
    )

    class Meta:
        model = User
        fields = ["username", "full_name", "email", "job_title"]

    def clean_email(self):
        """
        Email is unique on this model, so a clash has to be caught — but the
        message must not confirm who already has an account. It says what to do
        instead of what exists.
        """
        email = (self.cleaned_data["email"] or "").strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError(
                "An account request already exists for this address. "
                "Ask your administrator to check on it rather than requesting again."
            )
        return email

    def clean_username(self):
        username = (self.cleaned_data["username"] or "").strip()
        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError(
                f"“{username}” is taken. Try another, such as adding your surname."
            )
        return username

    def clean_password2(self):
        first = self.cleaned_data.get("password1")
        second = self.cleaned_data.get("password2")
        if first and second and first != second:
            raise forms.ValidationError("The two passwords don't match. Retype both.")
        return second

    def clean(self):
        cleaned = super().clean()
        password = cleaned.get("password1")
        if password:
            # Django's own validators decide what is acceptable; running them
            # here attaches their messages to the field rather than raising an
            # unhandled error later.
            user = User(
                username=cleaned.get("username") or "",
                email=cleaned.get("email") or "",
                full_name=cleaned.get("full_name") or "",
            )
            try:
                validate_password(password, user)
            except forms.ValidationError as exc:
                self.add_error("password1", exc)
        return cleaned

    def save(self, commit=True):
        """
        The account exists; it cannot be used.

        No group, no permissions, `is_active=False`. Django's ModelBackend
        refuses an inactive user, so this cannot sign in even with the right
        password. `deactivated_at` is set because the model requires an
        inactive user to carry one (user_inactive_has_timestamp), and the
        reason is written so an administrator reviewing the list can see at a
        glance that this is a pending request rather than a revoked account.
        """
        user = super().save(commit=False)
        user.set_password(self.cleaned_data["password1"])
        user.is_active = False
        user.is_staff = False
        user.is_superuser = False
        user.deactivated_at = timezone.now()
        user.deactivated_reason = "Awaiting administrator approval"
        if commit:
            user.save()
        return user
