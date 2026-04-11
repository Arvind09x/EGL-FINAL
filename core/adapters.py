"""
Custom allauth adapter that enforces @ashoka.edu.in domain and
email_verified checks.  Covers task A2-2.
"""

from allauth.account.adapter import DefaultAccountAdapter
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.shortcuts import redirect


class AshokaAccountAdapter(DefaultAccountAdapter):
    """Block non-Ashoka signups through regular (non-social) auth."""

    def is_open_for_signup(self, request):
        # Only allow social (Google) signup
        return False


class AshokaSocialAccountAdapter(DefaultSocialAccountAdapter):
    """Only allow verified @ashoka.edu.in Google accounts."""

    def pre_social_login(self, request, sociallogin):
        email = sociallogin.account.extra_data.get("email", "")
        email_verified = sociallogin.account.extra_data.get("email_verified", False)

        if not email.lower().endswith("@ashoka.edu.in"):
            from django.contrib import messages
            messages.error(
                request,
                "Only @ashoka.edu.in accounts are allowed to sign in.",
            )
            raise ImmediateHttpResponse(redirect("/"))

        if not email_verified:
            from django.contrib import messages
            messages.error(request, "Your email address has not been verified by Google.")
            raise ImmediateHttpResponse(redirect("/"))

    def populate_user(self, request, sociallogin, data):
        user = super().populate_user(request, sociallogin, data)
        user.email = data.get("email", "")
        user.username = user.email.split("@")[0]
        user.first_name = data.get("first_name", "")
        user.last_name = data.get("last_name", "")
        return user

    def is_open_for_signup(self, request, sociallogin):
        return True


# Required import for the exception
from allauth.core.exceptions import ImmediateHttpResponse  # noqa: E402
