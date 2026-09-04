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
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.password_validation import validate_password
from django.db.models import Q
from django.utils import timezone

from apps.accounts.models import User
from apps.core.form_ui import UIFormMixin

#: Written onto an account created by the request form, and read back by the
#: sign-in form so a pending account gets a useful answer instead of a generic
#: failure.
PENDING_APPROVAL = "Awaiting administrator approval"


class AccountRequestForm(UIFormMixin, forms.ModelForm):
    """Creates a user who cannot yet sign in."""

    placeholders = {
        "username": "How you'll sign in, e.g. a.hajjo",
        "full_name": "Your name as it should appear on records",
        "email": "name@company.com",
        "job_title": "What you do here, e.g. Accounts payable",
    }

    #: The two data- attributes are the contract with static/js/auth.js:
    #: `data-strength` asks it for the meter and the checklist of rules, and
    #: `data-confirms` names the box this one has to agree with. They live on
    #: the widget rather than in the template so the page cannot be rendered
    #: without them, and so every template rendering this form gets the same
    #: behaviour.
    password1 = forms.CharField(
        label="Password",
        strip=False,
        widget=forms.PasswordInput(
            attrs={
                "autocomplete": "new-password",
                "class": "field",
                "placeholder": "Create a strong password",
                "data-strength": "",
            }
        ),
    )
    password2 = forms.CharField(
        label="Confirm password",
        strip=False,
        widget=forms.PasswordInput(
            attrs={
                "autocomplete": "new-password",
                "class": "field",
                "placeholder": "Type the same password again",
                "data-confirms": "password1",
            }
        ),
        help_text="Typed twice, because a password nobody can reproduce is a lockout.",
    )

    class Meta:
        model = User
        fields = ["username", "full_name", "email", "job_title"]
        #: AbstractUser's own help text — "150 characters or fewer. Letters,
        #: digits and @/./+/-/_ only" — is a paragraph of rules under the first
        #: box on the page, and none of them are the question being asked. The
        #: placeholder shows the shape, and the validator says the rest in the
        #: one case it matters: when the answer breaks it.
        help_texts = {"username": ""}

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
        user.deactivated_reason = PENDING_APPROVAL
        if commit:
            user.save()
        return user


class SignInForm(AuthenticationForm):
    """Django's sign-in form, with an identifier that may be an email.

    The error messages are Django's own and deliberately stay that way: they
    never say whether the account exists, which is what stops the form being
    used to discover who has one (UX-008).
    """

    username = forms.CharField(
        label="Username or email",
        widget=forms.TextInput(
            attrs={
                "autocomplete": "username",
                "autofocus": True,
                "class": "field",
                "autocapitalize": "none",
                "spellcheck": "false",
            }
        ),
    )
    remember_me = forms.BooleanField(
        required=False,
        initial=True,
        label="Keep me signed in",
        help_text="Leave this off on a shared computer.",
    )

    #: One wording for every failed sign-in, whether or not the account exists.
    #: Django's default interpolates the username into the message; this one
    #: deliberately does not, so the reply cannot be read as confirmation.
    error_messages = {
        **AuthenticationForm.error_messages,
        "invalid_login": (
            "Sign-in failed: that username or email and password don't match an "
            "active account. Check both, then try again."
        ),
    }

    def clean(self):
        """Tell a pending account that it is pending — but only if it proves it.

        Someone who requested an account and then tried to sign in would
        otherwise be told their password was wrong, and would go round in
        circles resetting a password that was never the problem.

        `confirm_login_allowed` cannot do this: ModelBackend refuses an
        inactive user inside `authenticate()`, so the hook never runs. Checking
        here would leak which addresses have pending accounts, so the message
        is released only when the password supplied is the right one — which
        tells the person nothing they did not already know.
        """
        try:
            return super().clean()
        except forms.ValidationError:
            identifier = (self.cleaned_data.get("username") or "").strip()
            password = self.data.get("password") or ""
            if identifier and password:
                user = User.objects.filter(
                    Q(username__iexact=identifier) | Q(email__iexact=identifier)
                ).first()
                if (
                    user is not None
                    and not user.is_active
                    and user.deactivated_reason == PENDING_APPROVAL
                    and user.check_password(password)
                ):
                    raise forms.ValidationError(
                        "This account is still waiting for an administrator to "
                        "approve it. You'll be able to sign in once that is done.",
                        code="pending_approval",
                    ) from None
            raise
