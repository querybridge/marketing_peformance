"""
Query logic for the Promotional Report feature.

Computes two snapshots (period-to-yesterday + yesterday-only) with
YoY comparison alignment, brand performance table, and executive metrics.
"""

from datetime import date, timedelta

from .models import DimBrand
from .report_services import _pct_change
from .services import (
    ALERT_META,
    DateWindow,
    _div,
    budgets_for_period,
    classify_brand,
    media_by_brand,
    orders_by_brand,
    vertical_mts_for_period,
)


def _get_brand_rev(brand_id, orders, media, use_platform):
    """Extract revenue for a brand from the appropriate source."""
    if use_platform:
        return float(media.get(brand_id, {}).get("conv_value", 0))
    return float(orders.get(brand_id, {}).get("revenue", 0))


def _build_snapshot(cur_window, cmp_window, vertical_id, rev_type="net"):
    """Build a single executive snapshot dict for a current vs comparison window."""
    use_platform = rev_type == "platform"

    rev_cur = orders_by_brand(cur_window, vertical_id, rev_type=rev_type)
    rev_cmp = orders_by_brand(cmp_window, vertical_id, rev_type=rev_type)

    media_cur = media_by_brand(cur_window, vertical_id)
    media_cmp = media_by_brand(cmp_window, vertical_id)

    if use_platform:
        total_rev = sum(float(r.get("conv_value", 0)) for r in media_cur.values())
        total_rev_cmp = sum(float(r.get("conv_value", 0)) for r in media_cmp.values())
    else:
        total_rev = sum(float(r.get("revenue", 0)) for r in rev_cur.values())
        total_rev_cmp = sum(float(r.get("revenue", 0)) for r in rev_cmp.values())

    total_spend = sum(float(r.get("spend", 0)) for r in media_cur.values())
    total_mts = total_spend / total_rev if total_rev > 0 else None

    total_spend_cmp = sum(float(r.get("spend", 0)) for r in media_cmp.values())

    # MTS goal from vertical budget
    vert_mts = vertical_mts_for_period(cur_window)
    mts_goal = vert_mts.get(vertical_id)

    # Brand-level for top driver / largest shift
    brands = DimBrand.objects.filter(
        vertical_id=vertical_id,
    ).exclude(slug="unknown").select_related("vertical")

    brand_data = []
    for b in brands:
        b_rev = _get_brand_rev(b.id, rev_cur, media_cur, use_platform)
        b_rev_cmp = _get_brand_rev(b.id, rev_cmp, media_cmp, use_platform)
        brand_data.append({
            "name": b.name,
            "revenue": b_rev,
            "yoy_rev_delta": b_rev - b_rev_cmp,
        })

    brand_data.sort(key=lambda r: r["revenue"], reverse=True)

    # Top revenue driver
    top_driver = brand_data[0]["name"] if brand_data else "—"
    top_driver_revenue = brand_data[0]["revenue"] if brand_data else 0

    # Largest YoY shift
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

    # Bullet-graph dimensions
    mts_pct_val = round(total_mts * 100, 1) if total_mts else None
    mts_goal_pct_val = round(mts_goal * 100, 1) if mts_goal else None
    bullet_max = max(
        float(mts_pct_val) if mts_pct_val else 0,
        float(mts_goal_pct_val) if mts_goal_pct_val else 0,
        1,
    ) * 1.3
    mts_bullet_pct = min(round(float(mts_pct_val) / bullet_max * 100), 100) if mts_pct_val else 0
    mts_goal_bullet_pct = min(round(float(mts_goal_pct_val) / bullet_max * 100), 100) if mts_goal_pct_val else None

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

    return {
        "revenue": total_rev,
        "spend": total_spend,
        "mts": total_mts,
        "mts_pct": mts_pct_val,
        "mts_goal": mts_goal,
        "mts_goal_pct": mts_goal_pct_val,
        "rev_yoy_pct": _pct_change(total_rev, total_rev_cmp),
        "spend_yoy_pct": _pct_change(total_spend, total_spend_cmp),
        "top_driver": top_driver,
        "top_driver_revenue": top_driver_revenue,
        "largest_yoy_shift": shift_desc,
        "largest_yoy_shift_name": shift_name,
        "largest_yoy_shift_delta": shift_val,
        "mts_bullet_pct": mts_bullet_pct,
        "mts_goal_bullet_pct": mts_goal_bullet_pct,
        "mts_status": mts_status,
    }


def build_promo_report_data(vertical_id, promo_start, promo_end, cmp_start, cmp_end,
                            rev_type="net"):
    """Build all auto-filled data for the promotional report.

    rev_type: "net" (default), "new", or "platform"

    Returns a dict with:
    - snapshot_ptd: period-to-yesterday executive snapshot
    - snapshot_yesterday: yesterday-only executive snapshot
    - brand_rows: top 5 + flagged brands with metrics (using PTD range)
    - promo_ended: True if promo ended before yesterday
    - promo_not_started: True if promo starts in the future
    - cmp_warning: warning message if comparison period is too short
    """
    use_platform = rev_type == "platform"
    yesterday = date.today() - timedelta(days=1)

    promo_ended = promo_end < yesterday
    promo_not_started = promo_start > yesterday

    snapshot_ptd = None
    snapshot_yesterday = None
    cmp_warning = None

    # ── Period-to-Yesterday snapshot ──────────────────────────────
    if not promo_not_started:
        ptd_end = min(promo_end, yesterday)
        ptd_start = promo_start
        day_count = (ptd_end - ptd_start).days + 1
        cmp_end_ptd = cmp_start + timedelta(days=day_count - 1)

        if cmp_end_ptd > cmp_end:
            cmp_warning = "Comparison period too short for full alignment"

        cur_ptd = DateWindow(ptd_start, ptd_end)
        cmp_ptd = DateWindow(cmp_start, min(cmp_end_ptd, cmp_end))
        snapshot_ptd = _build_snapshot(cur_ptd, cmp_ptd, vertical_id, rev_type)

    # ── Yesterday-Only snapshot ───────────────────────────────────
    if not promo_not_started and not promo_ended:
        day_offset = (yesterday - promo_start).days
        cmp_day = cmp_start + timedelta(days=day_offset)

        if cmp_day > cmp_end:
            cmp_warning = cmp_warning or "Comparison period too short for full alignment"

        cur_yest = DateWindow(yesterday, yesterday)
        cmp_yest = DateWindow(cmp_day, cmp_day)
        snapshot_yesterday = _build_snapshot(cur_yest, cmp_yest, vertical_id, rev_type)

    # ── Brand rows (using PTD range for the table) ────────────────
    brand_rows = []
    if not promo_not_started:
        ptd_end = min(promo_end, yesterday)
        cur = DateWindow(promo_start, ptd_end)
        day_count = (ptd_end - promo_start).days + 1
        cmp_end_ptd = cmp_start + timedelta(days=day_count - 1)
        cmp = DateWindow(cmp_start, min(cmp_end_ptd, cmp_end))

        rev_cur = orders_by_brand(cur, vertical_id, rev_type=rev_type)
        rev_cmp = orders_by_brand(cmp, vertical_id, rev_type=rev_type)
        media_cur = media_by_brand(cur, vertical_id)
        media_cmp = media_by_brand(cmp, vertical_id)

        budgets = budgets_for_period(cur, rev_type=rev_type)

        brands = DimBrand.objects.filter(
            vertical_id=vertical_id,
        ).exclude(slug="unknown").select_related("vertical")

        all_brand_data = []
        for b in brands:
            b_rev = _get_brand_rev(b.id, rev_cur, media_cur, use_platform)
            b_spend = float(media_cur.get(b.id, {}).get("spend", 0))
            b_rev_cmp = _get_brand_rev(b.id, rev_cmp, media_cmp, use_platform)
            b_spend_cmp = float(media_cmp.get(b.id, {}).get("spend", 0))
            b_mts = b_spend / b_rev if b_rev > 0 else None

            bgt = budgets.get(b.id, {})
            mts_budget = float(bgt.get("mts_budget", 0)) if bgt.get("mts_budget") else 0

            alerts = classify_brand(
                cur_rev=b_rev,
                yoy_rev=b_rev_cmp,
                cur_mts=b_mts,
                mts_budget=mts_budget,
                cur_spend=b_spend,
                cmp_spend=b_spend_cmp,
                cmp_rev=b_rev_cmp,
                has_budget=b.id in budgets,
                has_revenue=b.id in media_cur if use_platform else b.id in rev_cur,
            )

            all_brand_data.append({
                "id": b.id,
                "name": b.name,
                "revenue": b_rev,
                "revenue_cmp": b_rev_cmp,
                "spend": b_spend,
                "spend_cmp": b_spend_cmp,
                "rev_yoy_pct": _pct_change(b_rev, b_rev_cmp),
                "spend_yoy_pct": _pct_change(b_spend, b_spend_cmp),
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
                "yoy_rev_delta": b_rev - b_rev_cmp,
            })

        all_brand_data.sort(key=lambda r: r["revenue"], reverse=True)

        top_5_ids = {r["id"] for r in all_brand_data[:5]}
        flagged_ids = {
            r["id"] for r in all_brand_data
            if any(a in ("needs_attention", "pacing_risk") for a in r["alerts"])
        }
        include_ids = top_5_ids | flagged_ids
        brand_rows = [r for r in all_brand_data if r["id"] in include_ids]

    return {
        "snapshot_ptd": snapshot_ptd,
        "snapshot_yesterday": snapshot_yesterday,
        "brand_rows": brand_rows,
        "promo_ended": promo_ended,
        "promo_not_started": promo_not_started,
        "cmp_warning": cmp_warning,
    }
