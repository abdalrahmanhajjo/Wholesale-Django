"""Sign in with a username or the email address on the account (ACC-001).

Administrators issue usernames, so usernames keep working exactly as they did.
But the address is the thing people actually remember about a work account, and
the request form already collects it and enforces that it is unique — so
accepting either costs nothing and removes the most common reason someone
cannot get in.

This subclasses ``ModelBackend`` rather than replacing it: permission
resolution, `is_active` enforcement and the rest are inherited untouched. Only
how the identifier is looked up changes.
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.db.models import Q

UserModel = get_user_model()


class EmailOrUsernameModelBackend(ModelBackend):
    """Resolve the identifier against username or email, then defer to Django."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        if username is None:
            username = kwargs.get(UserModel.USERNAME_FIELD)
        if username is None or password is None:
            return None

        identifier = username.strip()
        try:
            user = UserModel._default_manager.get(
                Q(username__iexact=identifier) | Q(email__iexact=identifier)
            )
        except UserModel.DoesNotExist:
            # Run the hasher anyway. Returning early on a missing user makes a
            # wrong username measurably faster than a wrong password, which is
            # a way to discover who has an account without ever signing in.
            UserModel().set_password(password)
            return None
        except UserModel.MultipleObjectsReturned:
            # One person's username is another's email address. Refusing is the
            # only safe answer: there is no way to tell which was meant.
            return None

        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None
