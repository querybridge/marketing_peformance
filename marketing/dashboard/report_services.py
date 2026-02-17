"""
Query logic for the Weekly Report feature.

Computes executive snapshot metrics, brand performance table, and
WoW/YoY comparisons from FactOrdersDaily and FactMediaDaily.
"""

from datetime import date, timedelta
from decimal import Decimal

from django.db.models import DecimalField, Sum
from django.db.models.functions import Coalesce

from .models import (
    DimBrand,
    FactMediaDaily,
    FactOrdersDaily,
    FactVerticalBudget,
)
from .services import (
    ALERT_META,
    DateWindow,
    classify_brand,
    media_by_brand,
    orders_by_brand,
    budgets_for_period,
    vertical_mts_for_period,
    _div,
)

_Z = Decimal("0")
_DF = DecimalField()


def last_week_window(ref_date=None):
    """Return (week_start, week_end) for last Mon-Sun before ref_date."""
    today = ref_date or date.today()
    last_sunday = today - timedelta(days=today.isoweekday())
    last_monday = last_sunday - timedelta(days=6)
    return last_monday, last_sunday


def week_window_for_date(week_ending):
    """Given a Sunday week_ending date, return (monday, sunday)."""
    return week_ending - timedelta(days=6), week_ending


def _pct_change(cur, prev):
    """Return percentage change or None."""
    if prev and float(prev) != 0:
        return round((float(cur) - float(prev)) / float(prev) * 100, 1)
    return None


def build_report_data(vertical_id, week_start, week_end):
    """Build all auto-filled data for the weekly report.

    Returns a dict with:
    - snapshot: executive metrics with WoW/YoY comparisons
    - brand_rows: top 5 + flagged brands with metrics
    - mts_goal: vertical MTS target for the month
    """
    cur = DateWindow(week_start, week_end)

    # WoW: prior week
    wow_start = week_start - timedelta(days=7)
    wow_end = week_end - timedelta(days=7)
    wow = DateWindow(wow_start, wow_end)

    # YoY: same week last year (52-week offset for weekday alignment)
    yoy_start = week_start - timedelta(weeks=52)
    yoy_end = week_end - timedelta(weeks=52)
    yoy = DateWindow(yoy_start, yoy_end)

    # ── Aggregate revenue (backend net) ──────────────────────────
    rev_cur = orders_by_brand(cur, vertical_id, rev_type="net")
    rev_wow = orders_by_brand(wow, vertical_id, rev_type="net")
    rev_yoy = orders_by_brand(yoy, vertical_id, rev_type="net")

    # ── Aggregate spend (all platform sources) ───────────────────
    media_cur = media_by_brand(cur, vertical_id)
    media_wow = media_by_brand(wow, vertical_id)
    media_yoy = media_by_brand(yoy, vertical_id)

    # ── Totals ───────────────────────────────────────────────────
    total_rev = sum(float(r.get("revenue", 0)) for r in rev_cur.values())
    total_spend = sum(float(r.get("spend", 0)) for r in media_cur.values())
    total_mts = total_spend / total_rev if total_rev > 0 else None

    total_rev_wow = sum(float(r.get("revenue", 0)) for r in rev_wow.values())
    total_spend_wow = sum(float(r.get("spend", 0)) for r in media_wow.values())

    total_rev_yoy = sum(float(r.get("revenue", 0)) for r in rev_yoy.values())
    total_spend_yoy = sum(float(r.get("spend", 0)) for r in media_yoy.values())

    # ── MTS goal from vertical budget ────────────────────────────
    vert_mts = vertical_mts_for_period(cur)
    mts_goal = vert_mts.get(vertical_id)

    # ── Brand-level metrics ──────────────────────────────────────
    brands = DimBrand.objects.filter(
        vertical_id=vertical_id,
    ).exclude(slug="unknown").select_related("vertical")

    budgets = budgets_for_period(cur, rev_type="net")

    brand_data = []
    for b in brands:
        b_rev = float(rev_cur.get(b.id, {}).get("revenue", 0))
        b_spend = float(media_cur.get(b.id, {}).get("spend", 0))
        b_rev_wow = float(rev_wow.get(b.id, {}).get("revenue", 0))
        b_rev_yoy = float(rev_yoy.get(b.id, {}).get("revenue", 0))
        b_spend_yoy = float(media_yoy.get(b.id, {}).get("spend", 0))
        b_spend_wow = float(media_wow.get(b.id, {}).get("spend", 0))
        b_mts = b_spend / b_rev if b_rev > 0 else None

        bgt = budgets.get(b.id, {})
        mts_budget = float(bgt.get("mts_budget", 0)) if bgt.get("mts_budget") else 0

        # Classify for alert badges
        alerts = classify_brand(
            cur_rev=b_rev,
            yoy_rev=b_rev_yoy,
            cur_mts=b_mts,
            mts_budget=mts_budget,
            cur_spend=b_spend,
            cmp_spend=b_spend_wow,
            cmp_rev=b_rev_wow,
            has_budget=1 if mts_budget else 0,
            has_revenue=1 if b_rev > 0 else 0,
        )

        brand_data.append({
            "id": b.id,
            "name": b.name,
            "revenue": b_rev,
            "spend": b_spend,
            "rev_wow_pct": _pct_change(b_rev, b_rev_wow),
            "rev_yoy_pct": _pct_change(b_rev, b_rev_yoy),
            "spend_yoy_pct": _pct_change(b_spend, b_spend_yoy),
            "mts": b_mts,
            "mts_pct": round(b_mts * 100, 1) if b_mts else None,
            "alerts": alerts,
            "alert_badges": [
                {
                    "key": a,
                    "label": ALERT_META[a]["label"],
                    "color": ALERT_META[a]["color"],
                    "icon": ALERT_META[a]["icon"],
                }
                for a in alerts
                if a in ALERT_META
            ],
            "yoy_rev_delta": b_rev - b_rev_yoy,
        })

    # Sort by revenue descending
    brand_data.sort(key=lambda r: r["revenue"], reverse=True)

    # Top 5 by revenue + any flagged as needs_attention or pacing_risk
    top_5_ids = {r["id"] for r in brand_data[:5]}
    flagged_ids = {
        r["id"] for r in brand_data
        if any(a in ("needs_attention", "pacing_risk") for a in r["alerts"])
    }
    include_ids = top_5_ids | flagged_ids
    brand_rows = [r for r in brand_data if r["id"] in include_ids]

    # Top revenue driver
    top_driver = brand_data[0]["name"] if brand_data else "—"
    top_driver_revenue = brand_data[0]["revenue"] if brand_data else 0

    # Largest YoY shift (absolute $ delta)
    yoy_brands = [r for r in brand_data if r["revenue"] > 0 or r["yoy_rev_delta"] != 0]
    if yoy_brands:
        largest_shift = max(yoy_brands, key=lambda r: abs(r["yoy_rev_delta"]))
        shift_name = largest_shift["name"]
        shift_val = largest_shift["yoy_rev_delta"]
        shift_desc = f"{shift_name} ({'+' if shift_val >= 0 else ''}${shift_val:,.0f})"
    else:
        shift_name = "—"
        shift_val = 0
        shift_desc = "—"

    # ── Bullet-graph dimensions for MTS vs goal ──────────────────
    mts_pct_val = round(total_mts * 100, 1) if total_mts else None
    mts_goal_pct_val = round(mts_goal * 100, 1) if mts_goal else None
    bullet_max = max(
        float(mts_pct_val) if mts_pct_val else 0,
        float(mts_goal_pct_val) if mts_goal_pct_val else 0,
        1,
    ) * 1.3  # 30% headroom
    mts_bullet_pct = min(round(float(mts_pct_val) / bullet_max * 100), 100) if mts_pct_val else 0
    mts_goal_bullet_pct = min(round(float(mts_goal_pct_val) / bullet_max * 100), 100) if mts_goal_pct_val else None
    # MTS status: ±50 bps = on_target (green), ±75 bps = warning (yellow), beyond = off_target (red)
    if mts_pct_val is not None and mts_goal_pct_val is not None:
        mts_bps_diff = abs(mts_pct_val - mts_goal_pct_val) * 100
        if mts_bps_diff <= 50:
            mts_status = "on_target"
        elif mts_bps_diff <= 75:
            mts_status = "warning"
        else:
            mts_status = "off_target"
    else:
        mts_status = None

    snapshot = {
        "revenue": total_rev,
        "spend": total_spend,
        "mts": total_mts,
        "mts_pct": mts_pct_val,
        "mts_goal": mts_goal,
        "mts_goal_pct": mts_goal_pct_val,
        "rev_wow_pct": _pct_change(total_rev, total_rev_wow),
        "rev_yoy_pct": _pct_change(total_rev, total_rev_yoy),
        "spend_wow_pct": _pct_change(total_spend, total_spend_wow),
        "spend_yoy_pct": _pct_change(total_spend, total_spend_yoy),
        "top_driver": top_driver,
        "top_driver_revenue": top_driver_revenue,
        "largest_yoy_shift": shift_desc,
        "largest_yoy_shift_name": shift_name,
        "largest_yoy_shift_delta": shift_val,
        "mts_bullet_pct": mts_bullet_pct,
        "mts_goal_bullet_pct": mts_goal_bullet_pct,
        "mts_status": mts_status,
    }

    return {
        "snapshot": snapshot,
        "brand_rows": brand_rows,
        "mts_goal": mts_goal,
    }
