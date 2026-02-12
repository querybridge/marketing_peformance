"""
DashboardAccessMiddleware — enforces login + group-based permissions.

* Unauthenticated users → redirect to login (HTMX-aware).
* Authenticated users → access-matrix check per resolved URL name.
* POST requests blocked for read_only pages.
* Sets ``request.access_level`` for templates.
"""

from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import redirect, render
from django.urls import resolve, Resolver404

from .auth_config import get_user_access

# Prefixes that skip all auth/permission checks.
EXEMPT_PREFIXES = (
    "/accounts/",
    "/admin/",
    "/static/",
)


class DashboardAccessMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path

        # ── Exempt paths ─────────────────────────────────────────────
        if any(path.startswith(p) for p in EXEMPT_PREFIXES):
            return self.get_response(request)

        # ── Login check ──────────────────────────────────────────────
        if not request.user.is_authenticated:
            login_url = f"/accounts/login/?next={path}"
            if request.headers.get("HX-Request"):
                resp = HttpResponse(status=204)
                resp["HX-Redirect"] = login_url
                return resp
            return redirect(login_url)

        # ── Resolve URL name ─────────────────────────────────────────
        try:
            match = resolve(path)
        except Resolver404:
            return self.get_response(request)

        url_name = match.url_name

        # ── Access check ─────────────────────────────────────────────
        access = get_user_access(request.user, url_name)
        request.access_level = access

        if access == "no_access":
            return render(request, "dashboard/403.html", status=403)

        if access == "read_only" and request.method == "POST":
            return render(request, "dashboard/403.html", status=403)

        return self.get_response(request)
