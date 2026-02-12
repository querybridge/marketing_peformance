"""
Context processor that injects per-view access levels and user display name
into every template so nav links can be shown/hidden.
"""

from .auth_config import ACCESS_MATRIX, get_user_access


def nav_permissions(request):
    if not hasattr(request, "user") or not request.user.is_authenticated:
        return {}

    user = request.user
    nav_perms = {
        view_name: get_user_access(user, view_name)
        for view_name in ACCESS_MATRIX
    }

    display = user.get_full_name() or user.username

    return {
        "nav_perms": nav_perms,
        "user_display_name": display,
    }
