import csv
import io
import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation

from django.core.paginator import Paginator
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.text import slugify
from . import services
from django.db.models import Sum
from .models import (
    DimBrand, DimCampaign, DimCampaignType, DimDate, DimSource, DimVertical,
    FactBudget, FactMediaDaily, FactOrdersDaily, FactVerticalBudget,
)


# ───────────────────────────────────────────────────────────────────────────
# Request helpers
# ───────────────────────────────────────────────────────────────────────────

def _parse_date(val):
    if not val:
        return None
    try:
        return date.fromisoformat(val)
    except (ValueError, TypeError):
        return None


def _params(request):
    """Extract filter / period params shared by every view."""
    g = request.GET
    preset = g.get("preset", "this_week")
    comparison = g.get("cmp", "wow")
    rev_type = g.get("rev", "net")
    vertical_id = int(g["vertical"]) if g.get("vertical") else None

    period = services.resolve_period(
        preset=preset,
        comparison=comparison,
        custom_start=_parse_date(g.get("start")),
        custom_end=_parse_date(g.get("end")),
        compare_start=_parse_date(g.get("cmp_start")),
        compare_end=_parse_date(g.get("cmp_end")),
    )

    return {
        "preset": preset,
        "comparison": comparison,
        "rev_type": rev_type,
        "vertical_id": vertical_id,
        "period": period,
        "filter_qs": request.GET.urlencode(),
        "filter_qs_no_page": "&".join(
            f"{k}={v}" for k, v in request.GET.items() if k != "page"
        ),
    }


# ───────────────────────────────────────────────────────────────────────────
# Weekly overview
# ───────────────────────────────────────────────────────────────────────────

def index(request):
    p = _params(request)
    period = p["period"]
    vid = p["vertical_id"]
    rev = p["rev_type"]

    all_brand_rows = services.brand_table(period, vid, rev)
    exceptions = services.exceptions_summary(all_brand_rows)
    trend = services.daily_trend(period, vid, rev, preset=p["preset"])

    # Compute aggregate MTS budget (spend-weighted average across brands)
    total_spend = sum(float(r.get("spend") or 0) for r in all_brand_rows)
    mts_budget_agg = None
    if total_spend > 0:
        weighted = sum(
            float(r.get("mts_budget") or 0) * float(r.get("spend") or 0)
            for r in all_brand_rows
            if r.get("mts_budget")
        )
        brands_with_budget = sum(
            1 for r in all_brand_rows if r.get("mts_budget")
        )
        if brands_with_budget:
            mts_budget_agg = weighted / total_spend

    paginator = Paginator(all_brand_rows, 20)
    page_number = request.GET.get("page", 1)
    page_obj = paginator.get_page(page_number)

    ctx = {
        **p,
        "verticals": DimVertical.objects.all(),
        "presets": services.PRESET_CHOICES,
        "comparisons": services.CMP_CHOICES,
        "brand_rows": page_obj,
        "page_obj": page_obj,
        "exceptions": exceptions,
        "alert_meta": services.ALERT_META,
        "trend_json": json.dumps(trend),
        "mts_budget_json": json.dumps(mts_budget_agg),
        "period_label": period.label,
        "current_start": period.current.start,
        "current_end": period.current.end,
        "current_days": period.current.days,
        "compare_start": period.compare.start,
        "compare_end": period.compare.end,
    }

    if request.headers.get("HX-Request"):
        return render(request, "dashboard/partials/dashboard_content.html", ctx)
    return render(request, "dashboard/index.html", ctx)


# ───────────────────────────────────────────────────────────────────────────
# HTMX drill-down partials
# ───────────────────────────────────────────────────────────────────────────

def drill_brand(request, brand_id):
    """Source-level breakdown for a brand."""
    p = _params(request)
    rows = services.drill_table(
        p["period"], "source", p["rev_type"], brand_id=brand_id,
    )
    brand = DimBrand.objects.get(id=brand_id)
    return render(request, "dashboard/partials/drill_rows.html", {
        **p,
        "drill_level": "source",
        "parent_name": brand.name,
        "brand_id": brand_id,
        "rows": rows,
    })


def drill_source(request, brand_id, source_id):
    """Campaign-type breakdown within brand + source."""
    p = _params(request)
    rows = services.drill_table(
        p["period"], "campaign_type", p["rev_type"],
        brand_id=brand_id, source_id=source_id,
    )
    source = DimSource.objects.get(id=source_id)
    return render(request, "dashboard/partials/drill_rows.html", {
        **p,
        "drill_level": "campaign_type",
        "parent_name": source.name,
        "brand_id": brand_id,
        "source_id": source_id,
        "rows": rows,
    })


def drill_type(request, brand_id, source_id, type_id):
    """Campaign-level breakdown within brand + source + type."""
    p = _params(request)
    rows = services.drill_table(
        p["period"], "campaign", p["rev_type"],
        brand_id=brand_id, source_id=source_id, type_id=type_id,
    )
    ctype = DimCampaignType.objects.get(id=type_id)
    return render(request, "dashboard/partials/drill_rows.html", {
        **p,
        "drill_level": "campaign",
        "parent_name": ctype.name,
        "brand_id": brand_id,
        "source_id": source_id,
        "type_id": type_id,
        "rows": rows,
    })


# ───────────────────────────────────────────────────────────────────────────
# Data dictionary
# ───────────────────────────────────────────────────────────────────────────

METRICS_DICT = [
    # ── Platform media metrics (Google Ads / Bing Ads) ────────
    {
        "name": "Spend",
        "definition": "Total media cost charged by the ad platform for the period.",
        "source": "Google Ads / Bing Ads",
        "grain": "campaign × day",
        "additive": "Yes",
        "rollup": "SUM(cost)",
        "nulls": "0 when campaign has no delivery. Never NULL.",
        "display": "Table + Chart",
    },
    {
        "name": "Impressions",
        "definition": "Number of times an ad was shown to a user.",
        "source": "Google Ads / Bing Ads",
        "grain": "campaign × day",
        "additive": "Yes",
        "rollup": "SUM(impressions)",
        "nulls": "0 when no delivery. Never NULL.",
        "display": "Table only",
    },
    {
        "name": "Impression Share",
        "definition": "Fraction of eligible impressions actually captured. "
                       "Reported by platform as a value 0.00–1.00.",
        "source": "Google Ads / Bing Ads",
        "grain": "campaign × day",
        "additive": "No",
        "rollup": "SUM(impression_share × impressions) ÷ SUM(impressions)  "
                  "(impression-weighted average)",
        "nulls": "NULL when platform does not report (e.g. Display, low volume). "
                 "Exclude NULLs from weighted average denominator.",
        "display": "Table only",
    },
    {
        "name": "Click Share",
        "definition": "Fraction of estimated eligible clicks actually received. "
                       "Reported by platform as a value 0.00–1.00.",
        "source": "Google Ads / Bing Ads",
        "grain": "campaign × day",
        "additive": "No",
        "rollup": "SUM(click_share × clicks) ÷ SUM(clicks)  "
                  "(click-weighted average)",
        "nulls": "NULL when platform does not report. "
                 "Exclude NULLs from weighted average denominator.",
        "display": "Table only",
    },
    {
        "name": "Clicks",
        "definition": "Number of user clicks on ads.",
        "source": "Google Ads / Bing Ads",
        "grain": "campaign × day",
        "additive": "Yes",
        "rollup": "SUM(clicks)",
        "nulls": "0 when no delivery. Never NULL.",
        "display": "Table only",
    },
    {
        "name": "CTR",
        "definition": "Click-through rate. Percentage of impressions that resulted in a click.",
        "source": "Calculated",
        "grain": "Recompute at any rollup level",
        "additive": "No — ratio",
        "rollup": "SUM(clicks) ÷ SUM(impressions)  "
                  "(never average pre-computed CTRs)",
        "nulls": "NULL when impressions = 0. Display as '—'.",
        "display": "Table only",
    },
    {
        "name": "CPC",
        "definition": "Cost per click. Average price paid for each click.",
        "source": "Calculated",
        "grain": "Recompute at any rollup level",
        "additive": "No — ratio",
        "rollup": "SUM(cost) ÷ SUM(clicks)  "
                  "(never average pre-computed CPCs)",
        "nulls": "NULL when clicks = 0. Display as '—'.",
        "display": "Table only",
    },
    {
        "name": "Conversion Value",
        "definition": "Platform-reported monetary value of conversions. "
                       "This is the platform's attribution — not the revenue source of truth.",
        "source": "Google Ads / Bing Ads",
        "grain": "campaign × day",
        "additive": "Yes",
        "rollup": "SUM(conversion_value)",
        "nulls": "0 when no conversions. Never NULL.",
        "display": "Table only",
    },
    {
        "name": "Conversion Rate",
        "definition": "Platform conversion rate. Percentage of clicks that "
                       "resulted in a platform-tracked conversion.",
        "source": "Calculated",
        "grain": "Recompute at any rollup level",
        "additive": "No — ratio",
        "rollup": "SUM(conversions) ÷ SUM(clicks)  "
                  "(never average pre-computed rates)",
        "nulls": "NULL when clicks = 0. Display as '—'.",
        "display": "Table only",
    },
    {
        "name": "Source Conversion Rate",
        "definition": "Platform-reported conversions divided by ad platform clicks. "
                       "Measures what fraction of clicks convert according to the platform. "
                       "Only shown below brand level (Source, Campaign Type, Campaign).",
        "source": "Calculated (platform data)",
        "grain": "Below brand only (Source / Campaign Type / Campaign)",
        "additive": "No — ratio",
        "rollup": "SUM(conversions) ÷ SUM(clicks)  "
                  "(recompute at each rollup level, never average pre-computed rates)",
        "nulls": "0 when clicks = 0. Not NULL — forced to zero.",
        "display": "Drill-down table only",
    },
    {
        "name": "Avg Conversion Value",
        "definition": "Platform-reported conversion_value divided by platform-reported conversions. "
                       "Average monetary value per conversion as attributed by the platform. "
                       "Only shown below brand level (Source, Campaign Type, Campaign).",
        "source": "Calculated (platform data)",
        "grain": "Below brand only (Source / Campaign Type / Campaign)",
        "additive": "No — ratio",
        "rollup": "SUM(conversion_value) ÷ SUM(conversions)  "
                  "(recompute at each rollup level, never average pre-computed values)",
        "nulls": "0 when conversions = 0. Not NULL — forced to zero.",
        "display": "Drill-down table only",
    },
    # ── CSV-uploaded order / revenue data ─────────────────────
    {
        "name": "Orders",
        "definition": "Count of completed orders attributed to this brand for the day. "
                       "Uploaded via CSV from the order management system.",
        "source": "CSV upload",
        "grain": "brand × day",
        "additive": "Yes",
        "rollup": "SUM(orders)",
        "nulls": "0 when no orders. Missing CSV upload → entire row absent. "
                 "Triggers 'Missing Revenue' data-quality alert.",
        "display": "Table + Chart",
    },
    {
        "name": "New Revenue",
        "definition": "Gross revenue from new customer orders for this brand, before returns/cancellations. "
                       "Selectable as the Revenue toggle value.",
        "source": "CSV upload",
        "grain": "brand × day",
        "additive": "Yes",
        "rollup": "SUM(new_revenue)",
        "nulls": "Same as Orders — row absent triggers data-quality alert.",
        "display": "Table + Chart (when toggle = New)",
    },
    {
        "name": "Net Revenue",
        "definition": "Revenue after returns, cancellations, and chargebacks. "
                       "Default Revenue toggle value.",
        "source": "CSV upload",
        "grain": "brand × day",
        "additive": "Yes",
        "rollup": "SUM(net_revenue)",
        "nulls": "Same as Orders — row absent triggers data-quality alert.",
        "display": "Table + Chart (when toggle = Net)",
    },
    {
        "name": "Revenue",
        "definition": "At brand level: the active revenue figure (New or Net) from CSV upload, "
                       "used in MTS, AOV, and all delta calculations. "
                       "Below brand level (Source, Campaign Type, Campaign): platform-reported "
                       "last-click conversion_value from FactMediaDaily. Sub-brand revenue will "
                       "generally not sum to brand-level revenue (different data sources).",
        "source": "CSV upload (brand); platform conversion_value (below brand)",
        "grain": "brand × day (native); campaign × day (below brand)",
        "additive": "Yes",
        "rollup": "SUM(selected_revenue_field) at brand level.  "
                  "Below brand: SUM(conversion_value).",
        "nulls": "0 when no CSV data uploaded (brand) or no platform data (sub-brand). "
                 "MTS and AOV become NULL.",
        "display": "Table + Chart",
    },
    # ── Calculated business metrics ───────────────────────────
    {
        "name": "MTS (Cost ÷ Revenue)",
        "definition": "Marketing-to-Sales ratio. Total ad spend divided by total revenue. "
                       "Lower is better — a 25% MTS means $0.25 spent per $1 of revenue. "
                       "Displayed as a percentage. Follows the Revenue toggle (New or Net).",
        "source": "Calculated",
        "grain": "brand × day (native); spend-allocated revenue below brand",
        "additive": "No — ratio",
        "rollup": "SUM(cost) ÷ SUM(revenue)  "
                  "(never average pre-computed MTS values across brands)",
        "nulls": "NULL when revenue = 0 (no CSV data or zero orders). Display as '—'. "
                 "Do not default to 0 — that would imply perfect efficiency.",
        "display": "Table + Chart",
    },
    {
        "name": "AOV",
        "definition": "Average order value. Revenue divided by order count. "
                       "Follows the Revenue toggle (New or Net).",
        "source": "Calculated",
        "grain": "brand × day",
        "additive": "No — ratio",
        "rollup": "SUM(revenue) ÷ SUM(orders)  "
                  "(never average pre-computed AOVs)",
        "nulls": "NULL when orders = 0. Display as '—'.",
        "display": "Table only",
    },
    {
        "name": "Net Conversion Rate",
        "definition": "Percentage of ad clicks that became completed orders. "
                       "Bridges platform click data with business order data. "
                       "Only meaningful at brand level because orders and clicks "
                       "live in different fact tables at different grains.",
        "source": "Calculated (cross-fact)",
        "grain": "brand level only",
        "additive": "No — ratio",
        "rollup": "SUM(orders) ÷ SUM(clicks)  "
                  "(orders from FactOrdersDaily, clicks from FactMediaDaily, "
                  "joined at brand level)",
        "nulls": "NULL when clicks = 0 or orders data missing. Display as '—'. "
                 "Not shown below brand level.",
        "display": "Table only",
    },
    # ── Budget / goal inputs ──────────────────────────────────
    {
        "name": "Revenue Budget",
        "definition": "Target revenue for the brand for the month. Entered via form input. "
                       "Prorated to the selected date window by overlap days: "
                       "displayed_budget = monthly_budget × (overlap_days ÷ days_in_month).",
        "source": "Form input (manual)",
        "grain": "brand × month",
        "additive": "Yes (after proration)",
        "rollup": "SUM of prorated monthly values across overlapping months",
        "nulls": "Absent when no budget entered. Triggers 'Missing Budget' alert. "
                 "Δ vs Budget shows '—'.",
        "display": "Table only",
    },
    {
        "name": "MTS Budget",
        "definition": "Target MTS ratio (Cost ÷ Revenue) for the brand. "
                       "Entered as a percentage (e.g. 25% = spend $0.25 per $1 revenue). "
                       "Used for Doing Well / Overly Efficient classification.",
        "source": "Form input (manual)",
        "grain": "brand × month",
        "additive": "No — target ratio",
        "rollup": "Not rolled up. Applies per brand. "
                  "When multiple months overlap, use the budget-weighted average.",
        "nulls": "Absent when no budget entered. "
                 "Alert classification skips MTS-based rules.",
        "display": "Table only",
    },
    # ── Deltas ────────────────────────────────────────────────
    {
        "name": "Δ vs Prior Period (all metrics)",
        "definition": "Percentage change from the comparison window to the current window. "
                       "For MTS, the delta is expressed in basis points (absolute difference × 10,000) "
                       "because percentage-of-a-percentage is misleading. "
                       "Comparison windows use equal elapsed days for pacing periods "
                       "(This Week, This Month, This Quarter).",
        "source": "Calculated",
        "grain": "Same as the underlying metric",
        "additive": "No — always computed after aggregation",
        "rollup": "(SUM_current − SUM_compare) ÷ |SUM_compare|  for additive metrics.  "
                  "For ratios: compute ratio for current, compute ratio for compare, "
                  "then subtract (MTS) or divide (others).",
        "nulls": "NULL when compare-period value is 0 or absent. Display as '—'. "
                 "Never show +∞ or −∞.",
        "display": "Table only",
    },
    {
        "name": "Δ vs Budget",
        "definition": "Variance between actual and budgeted values. "
                       "Revenue: (actual − budget) ÷ budget as a percentage. "
                       "MTS: actual_mts − mts_budget as basis points.",
        "source": "Calculated",
        "grain": "brand level only (budget grain)",
        "additive": "No — always computed after aggregation",
        "rollup": "Revenue: (SUM(revenue) − prorated_budget) ÷ prorated_budget.  "
                  "MTS: (SUM(cost) ÷ SUM(revenue)) − mts_budget, shown as bps.",
        "nulls": "NULL when budget is missing. Display as '—'.",
        "display": "Table only",
    },
]


def data_dictionary(request):
    q = request.GET.get("q", "").lower()
    metrics = METRICS_DICT
    if q:
        metrics = [
            m for m in metrics
            if q in m["name"].lower()
            or q in m["definition"].lower()
            or q in m["source"].lower()
        ]
    return render(request, "dashboard/data_dictionary.html", {
        "metrics": metrics,
        "query": q,
    })


# ───────────────────────────────────────────────────────────────────────────
# Alert spec
# ───────────────────────────────────────────────────────────────────────────

# Python rule logic — mirrors classify_brand() in services.py.
# Kept here as render-ready strings so the template stays logic-free.
ALERT_RULES = {
    "needs_attention": {
        "python": (
            "yoy_rev > 0 and cur_rev < yoy_rev * 0.80"
        ),
        "sql": (
            "CASE WHEN yoy_rev > 0\n"
            "      AND cur_rev < yoy_rev * 0.80\n"
            "     THEN 1 ELSE 0 END"
        ),
    },
    "doing_well": {
        "python": (
            "yoy_growth >= 0.10 and abs(cur_mts - mts_budget) <= 0.0050"
        ),
        "sql": (
            "CASE WHEN yoy_rev > 0\n"
            "      AND mts_budget > 0\n"
            "      AND cur_mts IS NOT NULL\n"
            "      AND (cur_rev - yoy_rev) / yoy_rev >= 0.10\n"
            "      AND ABS(cur_mts - mts_budget) <= 0.0050\n"
            "     THEN 1 ELSE 0 END"
        ),
    },
    "overly_efficient": {
        "python": (
            "mts_budget - cur_mts >= 0.0050"
        ),
        "sql": (
            "CASE WHEN mts_budget > 0\n"
            "      AND cur_mts IS NOT NULL\n"
            "      AND mts_budget - cur_mts >= 0.0050\n"
            "     THEN 1 ELSE 0 END"
        ),
    },
    "under_efficient": {
        "python": (
            "cur_mts - mts_budget >= 0.0050"
        ),
        "sql": (
            "CASE WHEN mts_budget > 0\n"
            "      AND cur_mts IS NOT NULL\n"
            "      AND cur_mts - mts_budget >= 0.0050\n"
            "     THEN 1 ELSE 0 END"
        ),
    },
    "pacing_risk": {
        "python": (
            "rev_chg < -0.10 and spd_chg > -0.10"
        ),
        "sql": (
            "CASE WHEN cmp_rev > 0 AND cmp_spend > 0\n"
            "      AND (cur_rev - cmp_rev) / cmp_rev < -0.10\n"
            "      AND (cur_spend - cmp_spend) / cmp_spend > -0.10\n"
            "     THEN 1 ELSE 0 END"
        ),
    },
    "missing_budget": {
        "python": "not has_budget",
        "sql": "CASE WHEN has_budget = 0 THEN 1 ELSE 0 END",
    },
    "missing_revenue": {
        "python": "not has_revenue",
        "sql": "CASE WHEN has_revenue = 0 THEN 1 ELSE 0 END",
    },
}

# Ordered list — controls the display order on the spec page.
ALERT_DISPLAY_ORDER = [
    "needs_attention",
    "pacing_risk",
    "overly_efficient",
    "under_efficient",
    "doing_well",
    "missing_revenue",
    "missing_budget",
]


def alert_spec(request):
    rows = []
    for key in ALERT_DISPLAY_ORDER:
        meta = services.ALERT_META[key]
        rule = ALERT_RULES[key]
        rows.append({
            "key": key,
            "name": meta["label"],
            "severity": meta["severity"],
            "color": meta["color"],
            "icon": meta["icon"],
            "tooltip": meta["tooltip"],
            "cta": meta["cta"],
            "drill_target": meta["drill_target"],
            "python": rule["python"],
            "sql": rule["sql"],
        })
    return render(request, "dashboard/alert_spec.html", {"alerts": rows})


# ───────────────────────────────────────────────────────────────────────────
# PDF export
# ───────────────────────────────────────────────────────────────────────────

def export_pdf(request):
    from .pdf_report import build_pdf, compute_totals

    p = _params(request)
    period = p["period"]
    brand_rows = services.brand_table(period, p["vertical_id"], p["rev_type"])
    exceptions = services.exceptions_summary(brand_rows)
    trend = services.daily_trend(period, p["vertical_id"], p["rev_type"], preset=p["preset"])
    totals = compute_totals(brand_rows)

    vertical_name = None
    if p["vertical_id"]:
        vert = DimVertical.objects.filter(id=p["vertical_id"]).first()
        vertical_name = vert.name if vert else None

    title = "Paid Marketing Performance"
    if vertical_name:
        title += f" — {vertical_name}"

    period_info = {
        "title": title,
        "date_range": f"{period.current.start} \u2013 {period.current.end}",
        "period_label": period.label,
        "pacing_note": period.pacing_note,
    }

    pdf_bytes = build_pdf(brand_rows, exceptions, trend, totals, period_info)
    slug = f"-{vertical_name.lower().replace(' ', '-')}" if vertical_name else ""
    resp = HttpResponse(pdf_bytes, content_type="application/pdf")
    resp["Content-Disposition"] = (
        f'attachment; filename="weekly-summary{slug}-{period.current.start}.pdf"'
    )
    return resp


# ───────────────────────────────────────────────────────────────────────────
# Budgets
# ───────────────────────────────────────────────────────────────────────────

_MONTH_LABELS = {
    1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr",
    5: "May", 6: "Jun", 7: "Jul", 8: "Aug",
    9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec",
}


def budgets(request):
    verticals = DimVertical.objects.all()

    this_year = date.today().year
    year_choices = [this_year, this_year + 1, this_year + 2]

    selected_vertical = request.GET.get("vertical") or request.POST.get("vertical")
    selected_year = request.GET.get("year") or request.POST.get("year")

    vertical = None
    brands = []
    brand_rows = []
    month_headers = []
    saved = False
    ly_rev_json = "{}"

    if selected_vertical:
        vertical = DimVertical.objects.filter(id=selected_vertical).first()

    if vertical and selected_year:
        year = int(selected_year)
        brands = list(DimBrand.objects.filter(vertical=vertical).order_by("name"))

        # 12 first-of-month DimDate rows for this year
        month_objs = list(
            DimDate.objects.filter(year=year, day_of_month=1).order_by("month")
        )

        # Existing brand-level budgets for this vertical + year
        existing = {}
        for fb in FactBudget.objects.filter(
            brand__vertical=vertical, month__in=month_objs,
        ).select_related("brand", "month"):
            existing[(fb.brand_id, fb.month_id)] = fb

        # Existing vertical-level targets
        vert_targets = {}
        for vb in FactVerticalBudget.objects.filter(
            vertical=vertical, month__in=month_objs,
        ).select_related("month"):
            vert_targets[vb.month_id] = vb

        # Last year's revenue by brand+month (for auto-allocation defaults)
        prior_year = year - 1
        ly_rev = {}
        for row in (
            FactOrdersDaily.objects
            .filter(brand__vertical=vertical, date__year=prior_year)
            .values("brand_id", "date__month")
            .annotate(revenue=Sum("net_revenue"))
        ):
            ly_rev[(row["brand_id"], row["date__month"])] = float(row["revenue"])

        # Last year's cancellation rates
        ly_cancel = {}
        for fb in FactBudget.objects.filter(
            brand__vertical=vertical, month__year=prior_year,
        ):
            ly_cancel[(fb.brand_id, fb.month.month)] = fb.cancellation_rate

        # ── POST: bulk save all 12 months ──
        if request.method == "POST":
            for m_obj in month_objs:
                mn = m_obj.month
                raw_mts = request.POST.get(f"mts_{mn}", "").strip()
                raw_vert_rev = request.POST.get(f"vert_rev_{mn}", "").strip()
                raw_cancel = request.POST.get(f"cancel_{mn}", "").strip()
                try:
                    mts_val = Decimal(raw_mts) / 100 if raw_mts else None
                except InvalidOperation:
                    mts_val = None
                try:
                    vert_rev_val = Decimal(raw_vert_rev) if raw_vert_rev else None
                except InvalidOperation:
                    vert_rev_val = None
                try:
                    cancel_val = Decimal(raw_cancel) / 100 if raw_cancel else Decimal("0")
                except InvalidOperation:
                    cancel_val = Decimal("0")

                # Always save vertical-level targets
                if vert_rev_val is not None and mts_val is not None:
                    FactVerticalBudget.objects.update_or_create(
                        vertical=vertical, month=m_obj,
                        defaults={
                            "revenue_budget": vert_rev_val,
                            "mts_budget": mts_val,
                            "cancellation_rate": cancel_val,
                        },
                    )
                elif vert_rev_val is None:
                    FactVerticalBudget.objects.filter(
                        vertical=vertical, month=m_obj,
                    ).delete()

                # Distribute to brands if any exist
                if vert_rev_val is not None and mts_val is not None and brands:
                    # Collect manually-overridden brand revenues
                    overrides = {}
                    for brand in brands:
                        if request.POST.get(f"override_{mn}_{brand.id}") == "1":
                            raw_rev = request.POST.get(f"rev_{mn}_{brand.id}", "").strip()
                            try:
                                overrides[brand.id] = Decimal(raw_rev) if raw_rev else Decimal("0")
                            except InvalidOperation:
                                overrides[brand.id] = Decimal("0")

                    override_total = sum(overrides.values())
                    remaining = vert_rev_val - override_total
                    auto_brands = [b for b in brands if b.id not in overrides]
                    total_ly_auto = sum(ly_rev.get((b.id, mn), 0) for b in auto_brands)

                    # Save overridden brands
                    for brand in brands:
                        if brand.id in overrides:
                            FactBudget.objects.update_or_create(
                                brand=brand, month=m_obj,
                                defaults={
                                    "revenue_budget": overrides[brand.id],
                                    "mts_budget": mts_val,
                                    "cancellation_rate": cancel_val,
                                    "manually_overridden": True,
                                },
                            )

                    # Distribute remaining to auto-allocated brands
                    allocated = Decimal("0")
                    for i, brand in enumerate(auto_brands):
                        if i == len(auto_brands) - 1:
                            rev_val = remaining - allocated
                        else:
                            if total_ly_auto > 0:
                                share = Decimal(str(ly_rev.get((brand.id, mn), 0))) / Decimal(str(total_ly_auto))
                            else:
                                share = Decimal("1") / Decimal(str(len(auto_brands)))
                            rev_val = (remaining * share).quantize(Decimal("1"))
                            allocated += rev_val

                        FactBudget.objects.update_or_create(
                            brand=brand, month=m_obj,
                            defaults={
                                "revenue_budget": rev_val,
                                "mts_budget": mts_val,
                                "cancellation_rate": cancel_val,
                                "manually_overridden": False,
                            },
                        )
                elif vert_rev_val is None:
                    # Clear brand budgets for this month when vert rev is blank
                    FactBudget.objects.filter(
                        brand__in=brands, month=m_obj,
                    ).delete()

            saved = True
            # Refresh existing after save
            existing = {}
            for fb in FactBudget.objects.filter(
                brand__vertical=vertical, month__in=month_objs,
            ).select_related("brand", "month"):
                existing[(fb.brand_id, fb.month_id)] = fb
            vert_targets = {}
            for vb in FactVerticalBudget.objects.filter(
                vertical=vertical, month__in=month_objs,
            ).select_related("month"):
                vert_targets[vb.month_id] = vb

        # ── Build brand_rows: one entry per brand, each with 12 month cells ──
        brand_rows = []
        for brand in brands:
            cells = []
            for m_obj in month_objs:
                fb = existing.get((brand.id, m_obj.id))
                cells.append({
                    "month_num": m_obj.month,
                    "revenue_budget": int(round(fb.revenue_budget)) if fb and fb.revenue_budget else "",
                    "manually_overridden": fb.manually_overridden if fb else False,
                })
            brand_rows.append({
                "brand": brand,
                "cells": cells,
            })

        # ── Build month_headers from vertical-level targets ──
        month_headers = []
        for m_obj in month_objs:
            mn = m_obj.month
            vb = vert_targets.get(m_obj.id)
            mts_display = ""
            cancel_display = ""
            vert_rev_int = ""
            if vb:
                vert_rev_int = int(round(vb.revenue_budget))
                mts_display = round(vb.mts_budget * 100, 2)
                if vb.cancellation_rate:
                    cancel_display = round(vb.cancellation_rate * 100, 2)

            # Marketing Revenue Budget = Vert Rev / (1 - Cancel Rate)
            mktg_rev = ""
            if vert_rev_int and cancel_display:
                cancel_frac = float(cancel_display) / 100
                if cancel_frac < 1:
                    mktg_rev = f"${int(round(float(vert_rev_int) / (1 - cancel_frac))):,}"

            month_headers.append({
                "month_num": mn,
                "month_label": _MONTH_LABELS[mn],
                "month_obj": m_obj,
                "mts_display": mts_display,
                "cancel_display": cancel_display,
                "vert_rev_budget": vert_rev_int,
                "marketing_rev_budget": mktg_rev,
            })

        # Build ly_rev_json for JS auto-allocation: { "month_num": { "brand_id": revenue }, ... }
        ly_rev_serializable = {}
        for (brand_id, month_num), revenue in ly_rev.items():
            mn_str = str(month_num)
            if mn_str not in ly_rev_serializable:
                ly_rev_serializable[mn_str] = {}
            ly_rev_serializable[mn_str][str(brand_id)] = revenue
        ly_rev_json = json.dumps(ly_rev_serializable)

    ctx = {
        "verticals": verticals,
        "year_choices": year_choices,
        "selected_vertical": int(selected_vertical) if selected_vertical else None,
        "selected_year": int(selected_year) if selected_year else None,
        "vertical": vertical,
        "brands": brands,
        "brand_rows": brand_rows,
        "month_headers": month_headers,
        "saved": saved,
        "ly_rev_json": ly_rev_json,
    }
    return render(request, "dashboard/budgets.html", ctx)


# ───────────────────────────────────────────────────────────────────────────
# Verticals
# ───────────────────────────────────────────────────────────────────────────

def verticals(request):
    error = None

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "add":
            name = request.POST.get("name", "").strip()
            if name:
                slug = slugify(name)
                if DimVertical.objects.filter(slug=slug).exists():
                    error = f'A vertical with the name "{name}" already exists.'
                else:
                    DimVertical.objects.create(name=name, slug=slug)
                    return redirect("dashboard:verticals")

        elif action == "edit":
            vid = request.POST.get("id")
            name = request.POST.get("name", "").strip()
            if vid and name:
                v = get_object_or_404(DimVertical, id=vid)
                slug = slugify(name)
                if DimVertical.objects.filter(slug=slug).exclude(id=vid).exists():
                    error = f'A vertical with the name "{name}" already exists.'
                else:
                    v.name = name
                    v.slug = slug
                    v.save()
                    return redirect("dashboard:verticals")

        elif action == "delete":
            vid = request.POST.get("id")
            if vid:
                v = get_object_or_404(DimVertical, id=vid)
                v.delete()
                return redirect("dashboard:verticals")

    all_verticals = DimVertical.objects.prefetch_related("brands").all()
    rows = []
    for v in all_verticals:
        rows.append({
            "vertical": v,
            "brand_count": v.brands.count(),
        })

    return render(request, "dashboard/verticals.html", {
        "rows": rows,
        "error": error,
    })


# ───────────────────────────────────────────────────────────────────────────
# Brands
# ───────────────────────────────────────────────────────────────────────────

def brands(request):
    error = None
    upload_summary = None
    selected_vertical = request.GET.get("vertical") or request.POST.get("vertical")

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "add":
            name = request.POST.get("name", "").strip()
            vid = request.POST.get("vertical_id", "").strip()
            raw_bid = request.POST.get("brand_id", "").strip()
            brand_id_val = int(raw_bid) if raw_bid else None
            if name:
                vertical = get_object_or_404(DimVertical, id=vid) if vid else None
                dup = DimBrand.objects.filter(name=name)
                if vertical:
                    dup = dup.filter(vertical=vertical)
                if dup.exists():
                    error = f'Brand "{name}" already exists.'
                elif brand_id_val is not None and DimBrand.objects.filter(brand_id=brand_id_val, vertical=vertical).exists():
                    error = f'Brand ID {brand_id_val} is already in use in this vertical.'
                else:
                    DimBrand.objects.create(
                        name=name, slug=slugify(name),
                        vertical=vertical, brand_id=brand_id_val,
                    )
                    return redirect(f"{request.path}?vertical={vid}" if vid else request.path)

        elif action == "edit":
            bid = request.POST.get("id")
            name = request.POST.get("name", "").strip()
            vid = request.POST.get("vertical_id", "").strip()
            raw_bid = request.POST.get("brand_id", "").strip()
            brand_id_val = int(raw_bid) if raw_bid else None
            if bid and name:
                b = get_object_or_404(DimBrand, id=bid)
                vertical = get_object_or_404(DimVertical, id=vid) if vid else None
                if brand_id_val is not None and DimBrand.objects.filter(brand_id=brand_id_val, vertical=vertical).exclude(id=bid).exists():
                    error = f'Brand ID {brand_id_val} is already in use in this vertical.'
                else:
                    b.name = name
                    b.slug = slugify(name)
                    b.vertical = vertical
                    b.brand_id = brand_id_val
                    b.save()
                    return redirect(f"{request.path}?vertical={selected_vertical or vid}" if (selected_vertical or vid) else request.path)

        elif action == "delete":
            bid = request.POST.get("id")
            if bid:
                b = get_object_or_404(DimBrand, id=bid)
                if b.campaigns.exists():
                    error = f'Cannot delete "{b.name}" — it has campaigns linked to it.'
                else:
                    vid = b.vertical_id
                    b.delete()
                    if vid:
                        return redirect(f"{request.path}?vertical={vid}")
                    return redirect(request.path)

        elif action == "upload_brands":
            csv_file = request.FILES.get("brand_csv")
            if csv_file:
                raw = csv_file.read()
                try:
                    text = raw.decode("utf-8-sig")
                except UnicodeDecodeError:
                    text = raw.decode("latin-1")
                reader = csv.reader(io.StringIO(text))
                headers = [h.strip().lower() for h in next(reader)]

                name_idx = next((i for i, h in enumerate(headers) if h in ("brand_name", "brand", "name")), None)
                bid_idx = next((i for i, h in enumerate(headers) if h in ("brand_id", "id")), None)
                vert_idx = next((i for i, h in enumerate(headers) if h in ("vertical", "vertical_name")), None)

                if name_idx is None or bid_idx is None:
                    error = "CSV must have 'brand_name' and 'brand_id' columns."
                else:
                    created_count = 0
                    updated_count = 0
                    for row in reader:
                        if not any(cell.strip() for cell in row):
                            continue
                        bname = row[name_idx].strip()
                        raw_bid = row[bid_idx].strip()
                        if not bname or not raw_bid:
                            continue
                        try:
                            bid_val = int(raw_bid)
                        except ValueError:
                            continue

                        vert = None
                        if vert_idx is not None and vert_idx < len(row) and row[vert_idx].strip():
                            vert, _ = DimVertical.objects.get_or_create(
                                name=row[vert_idx].strip(),
                                defaults={"slug": slugify(row[vert_idx].strip())},
                            )

                        brand, is_new = DimBrand.objects.update_or_create(
                            brand_id=bid_val,
                            vertical=vert,
                            defaults={
                                "name": bname,
                                "slug": slugify(bname),
                            },
                        )
                        if is_new:
                            created_count += 1
                        else:
                            updated_count += 1
                    upload_summary = f"Created {created_count}, updated {updated_count} brands."

    all_verticals = DimVertical.objects.all()
    vertical = None
    brand_rows = []

    if selected_vertical:
        vertical = DimVertical.objects.filter(id=selected_vertical).first()

    if vertical:
        for b in DimBrand.objects.filter(vertical=vertical).select_related("vertical"):
            brand_rows.append({
                "brand": b,
                "campaign_count": b.campaigns.count(),
            })
    else:
        for b in DimBrand.objects.select_related("vertical").all():
            brand_rows.append({
                "brand": b,
                "campaign_count": b.campaigns.count(),
            })

    return render(request, "dashboard/brands.html", {
        "verticals": all_verticals,
        "selected_vertical": int(selected_vertical) if selected_vertical else None,
        "vertical": vertical,
        "rows": brand_rows,
        "error": error,
        "upload_summary": upload_summary,
    })


# ───────────────────────────────────────────────────────────────────────────
# CSV Upload — Campaign Media Data Import
# ───────────────────────────────────────────────────────────────────────────

# Per-source column name mappings.
# Keys are internal field names; values are the exact header text in exports.
COLUMN_MAPS = {
    "google-ads": {
        "campaign":         "Campaign",
        "external_id":      "Campaign ID",
        "campaign_type":    "Campaign type",
        "date":             "Day",
        "impressions":      "Impr.",
        "clicks":           "Clicks",
        "cost":             "Cost",
        "conversions":      "Conversions",
        "conversion_value": "Conv. value",
        "impression_share": "Search impr. share",
        "click_share":      "Click share",
    },
    "bing-ads": {
        "campaign":         "Campaign name",
        "external_id":      "Campaign ID",
        "campaign_type":    "Campaign type",
        "date":             "Time period",
        "impressions":      "Impressions",
        "clicks":           "Clicks",
        "cost":             "Spend",
        "conversions":      "Conversions",
        "conversion_value": "Revenue",
        "impression_share": "Impression share %",
        "click_share":      "Click share %",
    },
    "meta-ads": {
        "campaign":         "Campaign name",
        "external_id":      "Campaign ID",
        "campaign_type":    "Campaign type",
        "date":             "Day",
        "impressions":      "Impressions",
        "clicks":           "Link clicks",
        "cost":             "Amount spent",
        "conversions":      "Purchases",
        "conversion_value": "Purchase conversion value",
    },
    "amazon-marketplace-sponsored": {
        "campaign":         "Campaign name",
        "external_id":      "Campaign ID",
        "campaign_type":    "Campaign type",
        "date":             "Day",
        "impressions":      "Impressions",
        "clicks":           "Clicks",
        "cost":             "Spend",
        "conversions":      "Orders",
        "conversion_value": "Sales",
    },
    "amazon-marketplace-feed": {
        "campaign":         "Campaign name",
        "external_id":      "Campaign ID",
        "campaign_type":    "Campaign type",
        "date":             "Day",
        "clicks":           "Sessions",
        "conversions":      "Orders",
        "conversion_value": "Sales",
    },
}

# Fallback — common column-name variants for auto-detection (case-insensitive).
_GENERIC_NAMES = {
    "campaign":         ["campaign", "campaign name", "campaign_name"],
    "external_id":      ["campaign id", "campaign_id", "id"],
    "campaign_type":    ["campaign type", "campaign_type", "type"],
    "date":             ["date", "day", "time period", "report date"],
    "impressions":      ["impressions", "impr.", "impr"],
    "clicks":           ["clicks", "link clicks", "sessions"],
    "cost":             ["cost", "spend", "amount spent"],
    "conversions":      ["conversions", "purchases", "conv.", "orders"],
    "conversion_value": ["conversion value", "conv. value", "purchase value",
                         "purchase conversion value", "sales",
                         "revenue", "conversion_value"],
    "impression_share": ["impression share", "search impr. share",
                         "impression share %", "impr. share"],
    "click_share":      ["click share", "click share %"],
}


def _parse_csv_date(val):
    """Parse a date string trying ISO, US, then EU formats."""
    if not val or not val.strip():
        return None
    val = val.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return date(*__import__("datetime").datetime.strptime(val, fmt).timetuple()[:3])
        except ValueError:
            continue
    return None


def _build_column_index(headers, source_slug):
    """Return {field_name: col_index} mapping from CSV headers."""
    lower_headers = [h.lower().strip() for h in headers]
    index = {}

    if source_slug in COLUMN_MAPS:
        mapping = COLUMN_MAPS[source_slug]
        for field, col_name in mapping.items():
            try:
                index[field] = lower_headers.index(col_name.lower())
            except ValueError:
                pass
    else:
        for field, variants in _GENERIC_NAMES.items():
            for variant in variants:
                if variant.lower() in lower_headers:
                    index[field] = lower_headers.index(variant.lower())
                    break
    return index


def _parse_decimal(val, default=Decimal("0")):
    """Parse a decimal value, stripping currency/percent symbols."""
    if not val or not isinstance(val, str):
        return default
    val = val.strip()
    if val in ("--", "N/A", "n/a", ""):
        return default
    val = val.replace("$", "").replace(",", "").replace("%", "").strip()
    try:
        return Decimal(val)
    except (InvalidOperation, ValueError):
        return default


def _parse_int(val, default=0):
    """Parse an integer, handling commas and float strings."""
    if not val or not isinstance(val, str):
        return default
    val = val.strip()
    if val in ("--", "N/A", "n/a", ""):
        return default
    val = val.replace(",", "").strip()
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return default


def _parse_share(val):
    """Parse share values: '0.45', '45%', '< 10%' → Decimal or None.

    Rules:
    - "< 10%" → interpreted as 10% (0.10)
    - Any parsed share below 10% is floored to 0.10
    - Any parsed share above 100% is capped at 1.00
    - Blank / "—" / "--" → None
    """
    if not val or not isinstance(val, str):
        return None
    val = val.strip()
    if val in ("--", "—", "N/A", "n/a", ""):
        return None
    val = val.replace("<", "").replace(">", "").strip()
    had_pct = "%" in val
    val = val.replace("%", "").strip()
    try:
        d = Decimal(val)
    except (InvalidOperation, ValueError):
        return None
    if had_pct or d > 1:
        d = d / 100
    # Floor at 10%, cap at 100%
    if d < Decimal("0.10"):
        d = Decimal("0.10")
    if d > Decimal("1.00"):
        d = Decimal("1.00")
    return d


def _detect_header_row(lines, source_slug):
    """Scan the first ~10 lines for the actual header row.

    Google Ads exports include metadata rows before the real header.
    We look for a line containing key column names from the source mapping
    (or generic fallbacks).  Returns (header_row_index, header_fields) or
    (None, None) if not found.
    """
    # Build a set of lowercase marker column names to search for.
    # We require at least a "campaign" column + a "date" column + a "cost" column.
    markers = set()
    if source_slug in COLUMN_MAPS:
        mapping = COLUMN_MAPS[source_slug]
        for field in ("campaign", "date", "cost"):
            if field in mapping:
                markers.add(mapping[field].lower())
    if not markers:
        # Fallback: generic markers
        markers = {"campaign", "day", "cost"}

    for idx, line in enumerate(lines[:10]):
        # Parse this line as CSV to handle quoted fields
        parsed = list(csv.reader([line]))
        if not parsed or not parsed[0]:
            continue
        lower_cells = {c.strip().lower() for c in parsed[0]}
        if markers <= lower_cells:
            return idx, parsed[0]
    return None, None


def upload_csv(request):
    sources = DimSource.objects.all()
    verticals = DimVertical.objects.all()

    # Build column info for JS hints
    column_info = {}
    for slug, mapping in COLUMN_MAPS.items():
        column_info[slug] = list(mapping.values())

    ctx = {
        "sources": sources,
        "verticals": verticals,
        "column_info_json": json.dumps(column_info),
        "error": None,
        "summary": None,
    }

    if request.method != "POST":
        return render(request, "dashboard/upload.html", ctx)

    # ── Validate required fields ──────────────────────────────────────
    vertical_id = request.POST.get("vertical")
    source_id = request.POST.get("source")
    csv_file = request.FILES.get("csv_file")
    amazon_type = request.POST.get("amazon_type", "").strip()

    if not all([vertical_id, source_id, csv_file]):
        ctx["error"] = "All fields are required: vertical, source, and CSV file."
        return render(request, "dashboard/upload.html", ctx)

    vertical = DimVertical.objects.filter(id=vertical_id).first()
    if not vertical:
        ctx["error"] = "Invalid vertical selection."
        return render(request, "dashboard/upload.html", ctx)

    source = DimSource.objects.filter(id=source_id).first()

    if not source:
        ctx["error"] = "Invalid source selection."
        return render(request, "dashboard/upload.html", ctx)

    if source.slug == "amazon-marketplace" and amazon_type not in ("sponsored", "feed"):
        ctx["error"] = "Please select an Amazon Type (Sponsored Ads or Feed-based Listings)."
        return render(request, "dashboard/upload.html", ctx)

    # ── Decode CSV ────────────────────────────────────────────────────
    raw = csv_file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")

    all_lines = text.splitlines()
    if not all_lines:
        ctx["error"] = "The CSV file is empty."
        return render(request, "dashboard/upload.html", ctx)

    # For Amazon, use composite slug to select the right column mapping
    lookup_slug = source.slug
    if source.slug == "amazon-marketplace" and amazon_type:
        lookup_slug = f"amazon-marketplace-{amazon_type}"

    # ── Auto-detect header row (handles Google Ads metadata rows) ─────
    header_row_idx, headers = _detect_header_row(all_lines, lookup_slug)
    metadata_rows_skipped = 0

    if header_row_idx is not None:
        metadata_rows_skipped = header_row_idx
        data_lines = all_lines[header_row_idx + 1:]
    else:
        # Fallback: treat first row as header (original behavior)
        parsed_first = list(csv.reader([all_lines[0]]))
        headers = parsed_first[0] if parsed_first else []
        data_lines = all_lines[1:]

    col_idx = _build_column_index(headers, lookup_slug)

    # Verify required columns — cost is optional for Amazon feed-based listings
    is_amazon_feed = source.slug == "amazon-marketplace" and amazon_type == "feed"
    requires_clicks = source.slug in ("meta-ads", "amazon-marketplace")
    required = ["campaign", "date", "conversions", "conversion_value"]
    if not is_amazon_feed:
        required.append("cost")
    if requires_clicks:
        required.append("clicks")
    missing = [req for req in required if req not in col_idx]
    if missing:
        ctx["error"] = f"Could not find required columns: {', '.join(missing)}. Found headers: {', '.join(headers)}"
        return render(request, "dashboard/upload.html", ctx)

    reader = csv.reader(io.StringIO("\n".join(data_lines)))

    # ── Pre-load campaign lookups (scoped to vertical + source) ──────
    source_campaigns = (
        DimCampaign.objects
        .filter(source=source, brand__vertical=vertical)
        .select_related("brand")
    )
    campaigns_by_ext_id = {}
    campaigns_by_name = {}
    for c in source_campaigns:
        if c.external_id:
            campaigns_by_ext_id[c.external_id] = c
        campaigns_by_name[c.name.lower()] = c

    # Pre-load campaign types by lowercase name for lookup
    types_by_name = {
        ct.name.lower(): ct for ct in DimCampaignType.objects.all()
    }

    # Default brand for auto-created campaigns (scoped to selected vertical)
    unknown_brand, _ = DimBrand.objects.get_or_create(
        slug="unknown", vertical=vertical,
        defaults={"name": "Unknown"},
    )

    # ── Process rows ──────────────────────────────────────────────────
    created = 0
    updated = 0
    campaigns_created = 0
    types_created = 0
    errors = []

    def _cell(row, field):
        idx = col_idx.get(field)
        if idx is None or idx >= len(row):
            return None
        return row[idx]

    # Row numbers reference the original file (1-indexed): metadata + header + data
    data_start_line = metadata_rows_skipped + 2  # +1 for header, +1 for 1-indexing
    for row_num, row in enumerate(reader, start=data_start_line):
        if not any(cell.strip() for cell in row):
            continue  # skip blank rows

        # Campaign matching
        campaign_name = (_cell(row, "campaign") or "").strip()
        ext_id = (_cell(row, "external_id") or "").strip()

        campaign = None
        if ext_id and ext_id in campaigns_by_ext_id:
            campaign = campaigns_by_ext_id[ext_id]
        elif campaign_name and campaign_name.lower() in campaigns_by_name:
            campaign = campaigns_by_name[campaign_name.lower()]

        if not campaign:
            if not campaign_name:
                errors.append(f"Row {row_num}: missing campaign name, skipped.")
                continue

            # Resolve campaign type from CSV column
            type_val = (_cell(row, "campaign_type") or "").strip()
            if type_val and type_val.lower() in types_by_name:
                ctype = types_by_name[type_val.lower()]
            elif type_val:
                ctype = DimCampaignType.objects.create(
                    name=type_val, slug=slugify(type_val),
                )
                types_by_name[type_val.lower()] = ctype
                types_created += 1
            else:
                # Fallback: use or create "Uncategorized"
                fallback = "uncategorized"
                if fallback in types_by_name:
                    ctype = types_by_name[fallback]
                else:
                    ctype = DimCampaignType.objects.create(
                        name="Uncategorized", slug="uncategorized",
                    )
                    types_by_name[fallback] = ctype
                    types_created += 1

            # Auto-assign brand via brand_id prefix (e.g. "123; My Campaign")
            # First check the upload vertical; if not found, look globally
            # and auto-create the brand in the upload vertical.
            brand_for_campaign = unknown_brand
            m = re.match(r'^(\d+)\s*;\s*', campaign_name)
            if m:
                parsed_brand_id = int(m.group(1))
                found = DimBrand.objects.filter(
                    brand_id=parsed_brand_id, vertical=vertical,
                ).first()
                if not found:
                    # Look up globally and clone into the upload vertical
                    global_brand = DimBrand.objects.filter(
                        brand_id=parsed_brand_id,
                    ).first()
                    if global_brand:
                        found = DimBrand.objects.create(
                            name=global_brand.name,
                            slug=slugify(global_brand.name),
                            brand_id=parsed_brand_id,
                            vertical=vertical,
                        )
                if found:
                    brand_for_campaign = found

            # Auto-create campaign
            campaign = DimCampaign.objects.create(
                name=campaign_name,
                external_id=ext_id,
                brand=brand_for_campaign,
                source=source,
                campaign_type=ctype,
                amazon_type=amazon_type if source.slug == "amazon-marketplace" else "",
            )
            if ext_id:
                campaigns_by_ext_id[ext_id] = campaign
            campaigns_by_name[campaign_name.lower()] = campaign
            campaigns_created += 1

        # Parse date
        date_val = _parse_csv_date(_cell(row, "date"))
        if not date_val:
            errors.append(f"Row {row_num}: invalid or missing date, skipped.")
            continue

        dim_date = DimDate.objects.filter(date=date_val).first()
        if not dim_date:
            errors.append(f"Row {row_num}: date {date_val} not found in calendar, skipped.")
            continue

        # Parse metrics
        impressions = _parse_int(_cell(row, "impressions"))
        clicks = _parse_int(_cell(row, "clicks"))
        cost = _parse_decimal(_cell(row, "cost"))
        conversions = _parse_int(_cell(row, "conversions"))
        conversion_value = _parse_decimal(_cell(row, "conversion_value"))
        impression_share = _parse_share(_cell(row, "impression_share"))
        click_share = _parse_share(_cell(row, "click_share"))

        _, is_created = FactMediaDaily.objects.update_or_create(
            campaign=campaign,
            date=dim_date,
            defaults={
                "impressions": impressions,
                "clicks": clicks,
                "cost": cost,
                "conversions": conversions,
                "conversion_value": conversion_value,
                "impression_share": impression_share,
                "click_share": click_share,
            },
        )
        if is_created:
            created += 1
        else:
            updated += 1

    ctx["summary"] = {
        "created": created,
        "updated": updated,
        "campaigns_created": campaigns_created,
        "types_created": types_created,
        "errors": errors,
        "total": created + updated,
        "metadata_rows_skipped": metadata_rows_skipped,
    }
    return render(request, "dashboard/upload.html", ctx)


# ───────────────────────────────────────────────────────────────────────────
# Campaign → Brand matching
# ───────────────────────────────────────────────────────────────────────────

def match_campaigns(request):
    """View to reassign Unknown campaigns to the correct brand, scoped by vertical."""
    verticals = DimVertical.objects.all()
    sources = DimSource.objects.all()

    selected_vertical = request.GET.get("vertical") or request.POST.get("vertical")
    selected_source = request.GET.get("source") or request.POST.get("source")
    saved = False
    error = None

    # Default to first vertical when none selected
    if not selected_vertical and verticals.exists():
        selected_vertical = str(verticals.first().id)

    vertical = None
    if selected_vertical:
        vertical = DimVertical.objects.filter(id=selected_vertical).first()

    if request.method == "POST":
        selected_source = request.POST.get("source")
        selected_vertical = request.POST.get("vertical")
        if selected_vertical:
            vertical = DimVertical.objects.filter(id=selected_vertical).first()
        campaign_ids = request.POST.getlist("campaign_id")
        for cid in campaign_ids:
            brand_id = request.POST.get(f"brand_{cid}")
            if brand_id:
                try:
                    campaign = DimCampaign.objects.get(id=cid)
                    campaign.brand_id = int(brand_id)
                    campaign.save(update_fields=["brand_id"])
                except (DimCampaign.DoesNotExist, ValueError):
                    error = f"Could not update campaign {cid}."
        saved = True

    # Only Unknown campaigns (slug="unknown") in the selected vertical
    campaigns = DimCampaign.objects.select_related(
        "brand", "brand__vertical", "source", "campaign_type",
    ).filter(brand__slug="unknown")
    if vertical:
        campaigns = campaigns.filter(brand__vertical=vertical)
    if selected_source:
        campaigns = campaigns.filter(source_id=selected_source)
    campaigns = campaigns.order_by("source__name", "name")

    # Brand dropdown scoped to selected vertical (exclude Unknown)
    brands = DimBrand.objects.select_related("vertical").exclude(slug="unknown")
    if vertical:
        brands = brands.filter(vertical=vertical)
    brands = brands.order_by("name")

    return render(request, "dashboard/match_campaigns.html", {
        "verticals": verticals,
        "sources": sources,
        "brands": brands,
        "campaigns": campaigns,
        "selected_vertical": int(selected_vertical) if selected_vertical else None,
        "selected_source": int(selected_source) if selected_source else None,
        "saved": saved,
        "error": error,
        "vertical": vertical,
    })


# ───────────────────────────────────────────────────────────────────────────
# Help
# ───────────────────────────────────────────────────────────────────────────

def help_page(request):
    # Canonical alert statuses for the help page status table
    help_alerts = []
    for key in ALERT_DISPLAY_ORDER:
        meta = services.ALERT_META[key]
        help_alerts.append({
            "label": meta["label"],
            "color": meta["color"],
            "tooltip": meta["tooltip"],
        })
    return render(request, "dashboard/help.html", {"help_alerts": help_alerts})


# ───────────────────────────────────────────────────────────────────────────
# Revenue Data Upload
# ───────────────────────────────────────────────────────────────────────────

REVENUE_REQUIRED_COLUMNS = ["vertical", "brand", "date", "orders", "net sales", "newsales"]


def _parse_currency(raw):
    """Parse a currency string like '$1,234.56' or '($309)' into a Decimal."""
    s = raw.strip()
    neg = s.startswith("(") and s.endswith(")")
    if neg:
        s = s[1:-1]
    s = s.replace(",", "").replace("$", "").strip()
    val = Decimal(s)
    return -val if neg else val


def _parse_revenue_date(val):
    """Parse date string accepting YYYY-MM-DD, M/D/YYYY, and M/D/YY formats."""
    if not val:
        return None
    val = val.strip()
    # YYYY-MM-DD
    try:
        return date.fromisoformat(val)
    except (ValueError, TypeError):
        pass
    # M/D/YYYY or M/D/YY
    m = re.match(r'^(\d{1,2})/(\d{1,2})/(\d{2,4})$', val)
    if m:
        try:
            year = int(m.group(3))
            if year < 100:
                year += 2000
            return date(year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            pass
    return None


def upload_revenue(request):
    """Upload backend revenue/orders CSV — source of truth for brand-level performance."""
    error = None
    summary = None

    if request.method == "POST":
        csv_file = request.FILES.get("csv_file")
        if not csv_file:
            error = "No file selected."
        else:
            raw = csv_file.read()
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = raw.decode("latin-1")

            lines = text.splitlines()
            if not lines:
                error = "CSV file is empty."
            else:
                reader = csv.reader(io.StringIO(text))
                raw_headers = next(reader)
                headers = [h.strip().lower() for h in raw_headers]

                # ── Validate required columns ──
                missing = [
                    col for col in REVENUE_REQUIRED_COLUMNS
                    if col not in headers
                ]
                if missing:
                    error = f"Missing required columns: {', '.join(missing)}. Found: {', '.join(raw_headers)}"
                else:
                    col_idx = {col: headers.index(col) for col in REVENUE_REQUIRED_COLUMNS}

                    # ── First pass: collect all vertical names and validate ──
                    rows_data = []
                    row_errors = []
                    vertical_names = set()
                    for line_num, row in enumerate(reader, start=2):
                        if not any(cell.strip() for cell in row):
                            continue

                        vert_name = row[col_idx["vertical"]].strip()
                        brand_name = row[col_idx["brand"]].strip()
                        date_raw = row[col_idx["date"]].strip()
                        orders_raw = row[col_idx["orders"]].strip()
                        net_raw = row[col_idx["net sales"]].strip()
                        new_raw = row[col_idx["newsales"]].strip()

                        if not vert_name:
                            row_errors.append(f"Row {line_num}: missing vertical.")
                            continue
                        if not brand_name:
                            row_errors.append(f"Row {line_num}: missing brand.")
                            continue

                        date_val = _parse_revenue_date(date_raw)
                        if not date_val:
                            row_errors.append(f"Row {line_num}: invalid date '{date_raw}'.")
                            continue

                        try:
                            orders_val = int(orders_raw.replace(",", ""))
                        except (ValueError, TypeError):
                            row_errors.append(f"Row {line_num}: invalid orders '{orders_raw}'.")
                            continue

                        try:
                            net_val = _parse_currency(net_raw)
                        except (InvalidOperation, ValueError, TypeError):
                            row_errors.append(f"Row {line_num}: invalid Net Sales '{net_raw}'.")
                            continue

                        try:
                            new_val = _parse_currency(new_raw)
                        except (InvalidOperation, ValueError, TypeError):
                            row_errors.append(f"Row {line_num}: invalid NewSales '{new_raw}'.")
                            continue

                        vertical_names.add(vert_name)
                        rows_data.append({
                            "line": line_num,
                            "vertical": vert_name,
                            "brand": brand_name,
                            "date": date_val,
                            "orders": orders_val,
                            "net_revenue": net_val,
                            "new_revenue": new_val,
                        })

                    # ── Check all verticals exist ──
                    vert_lookup = {}
                    for v in DimVertical.objects.all():
                        vert_lookup[v.name.lower().strip()] = v

                    unknown_verts = sorted({
                        r["vertical"] for r in rows_data
                        if r["vertical"].lower().strip() not in vert_lookup
                    })
                    if unknown_verts:
                        error = (
                            f"Unknown verticals (create them first): "
                            f"{', '.join(unknown_verts)}"
                        )
                    else:
                        # ── Process rows ──
                        brand_cache = {}
                        created = 0
                        updated = 0
                        skipped = 0
                        brands_created = []

                        for r in rows_data:
                            vertical = vert_lookup[r["vertical"].lower().strip()]
                            brand_key = (vertical.id, r["brand"].lower().strip())

                            if brand_key not in brand_cache:
                                brand = DimBrand.objects.filter(
                                    vertical=vertical,
                                    name__iexact=r["brand"].strip(),
                                ).first()
                                if not brand:
                                    brand = DimBrand.objects.create(
                                        name=r["brand"].strip(),
                                        slug=slugify(r["brand"].strip()),
                                        vertical=vertical,
                                    )
                                    brands_created.append(f"{r['brand']} ({vertical.name})")
                                brand_cache[brand_key] = brand

                            brand = brand_cache[brand_key]

                            dim_date = DimDate.objects.filter(date=r["date"]).first()
                            if not dim_date:
                                row_errors.append(
                                    f"Row {r['line']}: date {r['date']} not in calendar table — skipped."
                                )
                                skipped += 1
                                continue

                            _, is_new = FactOrdersDaily.objects.update_or_create(
                                brand=brand,
                                date=dim_date,
                                defaults={
                                    "orders": r["orders"],
                                    "net_revenue": r["net_revenue"],
                                    "new_revenue": r["new_revenue"],
                                },
                            )
                            if is_new:
                                created += 1
                            else:
                                updated += 1

                        summary = {
                            "total": len(rows_data),
                            "created": created,
                            "updated": updated,
                            "skipped": skipped,
                            "brands_created": brands_created,
                            "errors": row_errors,
                        }

    return render(request, "dashboard/upload_revenue.html", {
        "error": error,
        "summary": summary,
    })
