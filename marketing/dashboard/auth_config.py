"""
Access-control configuration: email domain whitelist, group names,
per-view access matrix, and helper to resolve a user's effective access.
"""

ALLOWED_EMAIL_DOMAINS = [
    "belami.com",
    "belamibvm.com",
    "belamiecommerce.com",
    "getsidecar.com",
    "missiononemedia.com",
]

GROUP_NAMES = [
    "Campaign Manager",
    "General Manager",
    "Admin",
    "Reporting",
    "Agency",
    "SuperUser",
]

# Access levels: "full" (GET+POST), "read_only" (GET only), "no_access" (403)
ACCESS_MATRIX = {
    # ── Overview ──────────────────────────────────────────────────────────
    "index":                {"Campaign Manager": "full", "General Manager": "full", "Admin": "full", "Reporting": "full", "Agency": "full", "SuperUser": "full"},
    "drill_brand":          {"Campaign Manager": "full", "General Manager": "full", "Admin": "full", "Reporting": "full", "Agency": "full", "SuperUser": "full"},
    "drill_source":         {"Campaign Manager": "full", "General Manager": "full", "Admin": "full", "Reporting": "full", "Agency": "full", "SuperUser": "full"},
    "drill_type":           {"Campaign Manager": "full", "General Manager": "full", "Admin": "full", "Reporting": "full", "Agency": "full", "SuperUser": "full"},
    "export_pdf":           {"Campaign Manager": "full", "General Manager": "full", "Admin": "full", "Reporting": "full", "Agency": "full", "SuperUser": "full"},

    # ── Budgets ───────────────────────────────────────────────────────────
    "budgets":              {"Campaign Manager": "read_only", "General Manager": "full", "Admin": "full", "Reporting": "read_only", "Agency": "read_only", "SuperUser": "full"},

    # ── Campaign Data ─────────────────────────────────────────────────────
    "upload_csv":           {"Campaign Manager": "full", "General Manager": "no_access", "Admin": "full", "Reporting": "full", "Agency": "full", "SuperUser": "full"},

    # ── Revenue Data ──────────────────────────────────────────────────────
    "upload_revenue":       {"Campaign Manager": "full", "General Manager": "no_access", "Admin": "full", "Reporting": "full", "Agency": "read_only", "SuperUser": "full"},

    # ── Weekly Optimization ───────────────────────────────────────────────
    "optimization":         {"Campaign Manager": "full", "General Manager": "full", "Admin": "full", "Reporting": "full", "Agency": "full", "SuperUser": "full"},
    "optimization_export":  {"Campaign Manager": "full", "General Manager": "full", "Admin": "full", "Reporting": "full", "Agency": "full", "SuperUser": "full"},

    # ── Verticals ─────────────────────────────────────────────────────────
    "verticals":            {"Campaign Manager": "read_only", "General Manager": "full", "Admin": "full", "Reporting": "read_only", "Agency": "read_only", "SuperUser": "full"},

    # ── Brands ────────────────────────────────────────────────────────────
    "brands":               {"Campaign Manager": "read_only", "General Manager": "full", "Admin": "full", "Reporting": "read_only", "Agency": "read_only", "SuperUser": "full"},

    # ── Data Dictionary ──────────────────────────────────────────────────
    "data_dictionary":      {"Campaign Manager": "full", "General Manager": "full", "Admin": "full", "Reporting": "full", "Agency": "full", "SuperUser": "full"},

    # ── Alert Rules ──────────────────────────────────────────────────────
    "alert_spec":           {"Campaign Manager": "full", "General Manager": "full", "Admin": "full", "Reporting": "full", "Agency": "full", "SuperUser": "full"},

    # ── Match Campaigns ──────────────────────────────────────────────────
    "match_campaigns":      {"Campaign Manager": "full", "General Manager": "read_only", "Admin": "full", "Reporting": "full", "Agency": "full", "SuperUser": "full"},

    # ── Site ID Mapping ──────────────────────────────────────────────────
    "upload_site_mapping":  {"Campaign Manager": "read_only", "General Manager": "full", "Admin": "full", "Reporting": "read_only", "Agency": "no_access", "SuperUser": "full"},

    # ── Scoring ──────────────────────────────────────────────────────────
    "scoring_config":       {"Campaign Manager": "full", "General Manager": "no_access", "Admin": "full", "Reporting": "read_only", "Agency": "no_access", "SuperUser": "full"},

    # ── Help ─────────────────────────────────────────────────────────────
    "help":                 {"Campaign Manager": "full", "General Manager": "full", "Admin": "full", "Reporting": "full", "Agency": "full", "SuperUser": "full"},

    # ── Add User ─────────────────────────────────────────────────────────
    "add_user":             {"Campaign Manager": "no_access", "General Manager": "no_access", "Admin": "full", "Reporting": "no_access", "Agency": "no_access", "SuperUser": "full"},

    # ── Weekly Report ────────────────────────────────────────────────────
    "weekly_report":        {"Campaign Manager": "full", "General Manager": "read_only", "Admin": "full", "Reporting": "full", "Agency": "read_only", "SuperUser": "full"},
    "weekly_report_export": {"Campaign Manager": "full", "General Manager": "read_only", "Admin": "full", "Reporting": "full", "Agency": "read_only", "SuperUser": "full"},

    # ── Promotional Report ────────────────────────────────────────────
    "promotion_dates":              {"Campaign Manager": "read_only", "General Manager": "full", "Admin": "full", "Reporting": "read_only", "Agency": "read_only", "SuperUser": "full"},
    "promotional_report":           {"Campaign Manager": "full", "General Manager": "read_only", "Admin": "full", "Reporting": "full", "Agency": "read_only", "SuperUser": "full"},
    "promotional_report_export":    {"Campaign Manager": "full", "General Manager": "read_only", "Admin": "full", "Reporting": "full", "Agency": "read_only", "SuperUser": "full"},
    "promotion_detail":             {"Campaign Manager": "full", "General Manager": "full", "Admin": "full", "Reporting": "full", "Agency": "full", "SuperUser": "full"},
}

# Priority order for resolving multi-group membership (higher is better).
_LEVEL_PRIORITY = {"no_access": 0, "read_only": 1, "full": 2}


def get_user_access(user, view_name):
    """Return the effective access level for *user* on *view_name*.

    * Superusers always get "full".
    * Multi-group users get the best (most permissive) level.
    * Unknown views default to "no_access".
    """
    if user.is_superuser:
        return "full"

    view_perms = ACCESS_MATRIX.get(view_name)
    if view_perms is None:
        return "no_access"

    user_groups = set(user.groups.values_list("name", flat=True))
    best = "no_access"
    for group_name in user_groups:
        level = view_perms.get(group_name, "no_access")
        if _LEVEL_PRIORITY.get(level, 0) > _LEVEL_PRIORITY.get(best, 0):
            best = level
    return best
