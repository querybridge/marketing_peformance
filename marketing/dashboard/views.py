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
from .models import (
    DimBrand, DimCampaign, DimCampaignType, DimDate, DimSource, DimVertical,
    FactBudget, FactMediaDaily,
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
        "definition": "The active revenue figure used in MTS, AOV, and all delta calculations. "
                       "Equals New Revenue or Net Revenue depending on the Revenue toggle. "
                       "Below brand level (Source, Campaign Type, Campaign), revenue is "
                       "allocated proportionally by spend share because order data exists "
                       "only at brand × day grain.",
        "source": "CSV upload + toggle",
        "grain": "brand × day (native); spend-allocated below brand",
        "additive": "Yes at brand+; allocated below brand",
        "rollup": "SUM(selected_revenue_field) at brand level.  "
                  "Below brand: brand_revenue × (entity_spend ÷ brand_spend).",
        "nulls": "0 when no CSV data uploaded. MTS and AOV become NULL.",
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

    title = "Weekly Performance Dashboard"
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

_MONTH_NAMES = [
    (1, "Jan"), (2, "Feb"), (3, "Mar"), (4, "Apr"),
    (5, "May"), (6, "Jun"), (7, "Jul"), (8, "Aug"),
    (9, "Sep"), (10, "Oct"), (11, "Nov"), (12, "Dec"),
]


def budgets(request):
    verticals = DimVertical.objects.all()

    this_year = date.today().year
    year_choices = [this_year, this_year + 1, this_year + 2]

    selected_vertical = request.GET.get("vertical") or request.POST.get("vertical")
    selected_year = request.GET.get("year") or request.POST.get("year")
    selected_month = request.GET.get("month") or request.POST.get("month")

    vertical = None
    month_obj = None
    brands = []
    rows = []
    saved = False
    vertical_cancel_rate = None

    if selected_vertical:
        vertical = DimVertical.objects.filter(id=selected_vertical).first()

    if selected_year and selected_month:
        try:
            target = date(int(selected_year), int(selected_month), 1)
        except (ValueError, TypeError):
            target = None
        if target:
            month_obj = DimDate.objects.filter(date=target).first()

    mts_display = ""

    if vertical and month_obj:
        brands = DimBrand.objects.filter(vertical=vertical).order_by("name")
        existing = {
            fb.brand_id: fb
            for fb in FactBudget.objects.filter(
                brand__vertical=vertical, month=month_obj,
            )
        }

        # Derive current MTS from any existing budget in this vertical/month
        if existing:
            ratio = next(iter(existing.values())).mts_budget
            mts_display = round(ratio * 100, 2)

        if request.method == "POST":
            raw_pct = request.POST.get("mts_pct", "").strip()
            try:
                mts_val = Decimal(raw_pct) / 100 if raw_pct else None
            except InvalidOperation:
                mts_val = None

            for brand in brands:
                raw_rev = request.POST.get(f"rev_{brand.id}", "").strip()
                raw_cancel = request.POST.get(f"cancel_{brand.id}", "").strip()
                try:
                    rev_val = Decimal(raw_rev) if raw_rev else None
                except InvalidOperation:
                    continue
                try:
                    cancel_val = Decimal(raw_cancel) / 100 if raw_cancel else Decimal("0")
                except InvalidOperation:
                    cancel_val = Decimal("0")

                if rev_val is not None and mts_val is not None:
                    FactBudget.objects.update_or_create(
                        brand=brand,
                        month=month_obj,
                        defaults={
                            "revenue_budget": rev_val,
                            "mts_budget": mts_val,
                            "cancellation_rate": cancel_val,
                        },
                    )
                elif brand.id in existing and rev_val is None:
                    existing[brand.id].delete()

            saved = True
            mts_display = Decimal(raw_pct) if raw_pct else ""
            # Refresh after save
            existing = {
                fb.brand_id: fb
                for fb in FactBudget.objects.filter(
                    brand__vertical=vertical, month=month_obj,
                )
            }

        for brand in brands:
            fb = existing.get(brand.id)
            cancel_display = round(fb.cancellation_rate * 100, 2) if fb and fb.cancellation_rate else ""
            rows.append({
                "brand": brand,
                "revenue_budget": fb.revenue_budget if fb else "",
                "cancellation_rate": cancel_display,
            })

        # Vertical-level aggregate cancellation rate (revenue-weighted average)
        total_rev = sum(
            float(fb.revenue_budget) for fb in existing.values()
            if fb.revenue_budget
        )
        if total_rev > 0:
            weighted = sum(
                float(fb.cancellation_rate or 0) * float(fb.revenue_budget)
                for fb in existing.values()
                if fb.revenue_budget
            )
            vertical_cancel_rate = round(weighted / total_rev * 100, 2)
        else:
            vertical_cancel_rate = None

    ctx = {
        "verticals": verticals,
        "year_choices": year_choices,
        "month_choices": _MONTH_NAMES,
        "selected_vertical": int(selected_vertical) if selected_vertical else None,
        "selected_year": int(selected_year) if selected_year else None,
        "selected_month": int(selected_month) if selected_month else None,
        "vertical": vertical,
        "month_obj": month_obj,
        "rows": rows,
        "mts_display": mts_display,
        "vertical_cancel_rate": vertical_cancel_rate,
        "saved": saved,
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
    selected_vertical = request.GET.get("vertical") or request.POST.get("vertical")

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "add":
            name = request.POST.get("name", "").strip()
            vid = request.POST.get("vertical_id", "").strip()
            if name and vid:
                vertical = get_object_or_404(DimVertical, id=vid)
                slug = slugify(name)
                if DimBrand.objects.filter(name=name, vertical=vertical).exists():
                    error = f'Brand "{name}" already exists in {vertical.name}.'
                else:
                    DimBrand.objects.create(name=name, slug=slug, vertical=vertical)
                    return redirect(f"{request.path}?vertical={vid}")

        elif action == "edit":
            bid = request.POST.get("id")
            name = request.POST.get("name", "").strip()
            vid = request.POST.get("vertical_id", "").strip()
            if bid and name and vid:
                b = get_object_or_404(DimBrand, id=bid)
                vertical = get_object_or_404(DimVertical, id=vid)
                if DimBrand.objects.filter(name=name, vertical=vertical).exclude(id=bid).exists():
                    error = f'Brand "{name}" already exists in {vertical.name}.'
                else:
                    b.name = name
                    b.slug = slugify(name)
                    b.vertical = vertical
                    b.save()
                    return redirect(f"{request.path}?vertical={selected_vertical or vid}")

        elif action == "delete":
            bid = request.POST.get("id")
            if bid:
                b = get_object_or_404(DimBrand, id=bid)
                if b.campaigns.exists():
                    error = f'Cannot delete "{b.name}" — it has campaigns linked to it.'
                else:
                    vid = b.vertical_id
                    b.delete()
                    return redirect(f"{request.path}?vertical={vid}")

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
        "conversion_value": "Purchase value",
    },
}

# Fallback — common column-name variants for auto-detection (case-insensitive).
_GENERIC_NAMES = {
    "campaign":         ["campaign", "campaign name", "campaign_name"],
    "external_id":      ["campaign id", "campaign_id", "id"],
    "campaign_type":    ["campaign type", "campaign_type", "type"],
    "date":             ["date", "day", "time period", "report date"],
    "impressions":      ["impressions", "impr.", "impr"],
    "clicks":           ["clicks", "link clicks"],
    "cost":             ["cost", "spend", "amount spent"],
    "conversions":      ["conversions", "purchases", "conv."],
    "conversion_value": ["conversion value", "conv. value", "purchase value",
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
    """Parse share values: '0.45', '45%', '< 10%' → Decimal or None."""
    if not val or not isinstance(val, str):
        return None
    val = val.strip()
    if val in ("--", "N/A", "n/a", ""):
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
    return d


def upload_csv(request):
    sources = DimSource.objects.all()

    # Build column info for JS hints
    column_info = {}
    for slug, mapping in COLUMN_MAPS.items():
        column_info[slug] = list(mapping.values())

    ctx = {
        "sources": sources,
        "column_info_json": json.dumps(column_info),
        "error": None,
        "summary": None,
    }

    if request.method != "POST":
        return render(request, "dashboard/upload.html", ctx)

    # ── Validate required fields ──────────────────────────────────────
    source_id = request.POST.get("source")
    csv_file = request.FILES.get("csv_file")

    if not all([source_id, csv_file]):
        ctx["error"] = "All fields are required: source and CSV file."
        return render(request, "dashboard/upload.html", ctx)

    source = DimSource.objects.filter(id=source_id).first()

    if not source:
        ctx["error"] = "Invalid source selection."
        return render(request, "dashboard/upload.html", ctx)

    # ── Decode CSV ────────────────────────────────────────────────────
    raw = csv_file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")

    reader = csv.reader(io.StringIO(text))
    try:
        headers = next(reader)
    except StopIteration:
        ctx["error"] = "The CSV file is empty."
        return render(request, "dashboard/upload.html", ctx)

    col_idx = _build_column_index(headers, source.slug)

    # Verify required columns
    missing = []
    for req in ("campaign", "date", "cost"):
        if req not in col_idx:
            missing.append(req)
    if missing:
        ctx["error"] = f"Could not find required columns: {', '.join(missing)}. Found headers: {', '.join(headers)}"
        return render(request, "dashboard/upload.html", ctx)

    # ── Pre-load campaign lookups ─────────────────────────────────────
    source_campaigns = DimCampaign.objects.filter(source=source).select_related("brand")
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

    # Default brand for auto-created campaigns
    unknown_vertical, _ = DimVertical.objects.get_or_create(
        slug="unknown", defaults={"name": "Unknown"},
    )
    unknown_brand, _ = DimBrand.objects.get_or_create(
        slug="unknown", vertical=unknown_vertical,
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

    for row_num, row in enumerate(reader, start=2):
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

            # Auto-create campaign
            campaign = DimCampaign.objects.create(
                name=campaign_name,
                external_id=ext_id,
                brand=unknown_brand,
                source=source,
                campaign_type=ctype,
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
    }
    return render(request, "dashboard/upload.html", ctx)


# ───────────────────────────────────────────────────────────────────────────
# Campaign → Brand matching
# ───────────────────────────────────────────────────────────────────────────

def match_campaigns(request):
    """View to reassign campaigns to the correct brand."""
    sources = DimSource.objects.all()
    brands = DimBrand.objects.select_related("vertical").order_by("vertical__name", "name")
    selected_source = request.GET.get("source") or request.POST.get("source")
    saved = False
    error = None

    if request.method == "POST":
        selected_source = request.POST.get("source")
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

    campaigns = DimCampaign.objects.select_related(
        "brand", "brand__vertical", "source", "campaign_type",
    )
    if selected_source:
        campaigns = campaigns.filter(source_id=selected_source)
    campaigns = campaigns.order_by("source__name", "name")

    return render(request, "dashboard/match_campaigns.html", {
        "sources": sources,
        "brands": brands,
        "campaigns": campaigns,
        "selected_source": int(selected_source) if selected_source else None,
        "saved": saved,
        "error": error,
    })
