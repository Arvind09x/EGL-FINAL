"""
Role-based access decorators for the Eeshto Games Library.
Covers task A2-5.
"""

from functools import wraps

from django.http import HttpResponseForbidden
from django.shortcuts import redirect


def borrower_required(view_func):
    """Require the user to be authenticated."""
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect("account_login")
        return view_func(request, *args, **kwargs)
    return _wrapped


def member_required(view_func):
    """Require the user to be an Eeshto member (desk staff) or superuser."""
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect("account_login")
        if not (request.user.is_member or request.user.is_superuser):
            return HttpResponseForbidden(
                "You must be an Eeshto member to access this page."
            )
        return view_func(request, *args, **kwargs)
    return _wrapped


def senior_admin_required(view_func):
    """Require the user to be a senior admin or superuser."""
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect("account_login")
        if not (request.user.is_senior_admin or request.user.is_superuser):
            return HttpResponseForbidden(
                "You must be a senior admin to access this page."
            )
        return view_func(request, *args, **kwargs)
    return _wrapped
