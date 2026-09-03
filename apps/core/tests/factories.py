"""
Shared test fixtures for the core app.

`make_user` lives here because the convention it encodes was previously copied
into six test modules, and the seventh — written without noticing — created
users with no email. `User.email` is unique, so two of those collide on the
empty string and the whole class errors in setUp with a constraint violation
that names nothing useful. One helper, one place to look.
"""

from django.contrib.auth.models import Group

from apps.accounts.models import User

#: Not a secret — a fixed value so a test can log in as the user it made.
PASSWORD = "testpass-12345"  # noqa: S105


def make_user(username, group_name=None, **kwargs):
    """
    A user with a unique email, optionally in a role group.

    The email is derived from the username rather than passed in, because it
    exists only to satisfy the unique constraint — no test should have to think
    about it.
    """
    kwargs.setdefault("email", f"{username}@example.com")
    kwargs.setdefault("password", PASSWORD)
    user = User.objects.create_user(username=username, **kwargs)
    if group_name:
        user.groups.add(Group.objects.get(name=group_name))
    return user
