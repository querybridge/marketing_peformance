"""
Core business logic: date engine, metric aggregation, alert classification.

Key concepts
────────────
- Period: a current DateWindow + comparison DateWindow of equal elapsed days
- Revenue source: FactOrdersDaily (brand × day); allocated below brand by spend share
- MTS = Cost ÷ Revenue  (lower is better; "overly efficient" = lower than target)
- Alerts: classified per brand based on YOY, MTS budget, and pacing
"""

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from django.db.models import DecimalField, Sum
from django.db.models.functions import Coalesce

from .models import (
    DimBrand,
    DimCampaign,
    DimCampaignType,
    DimSource,
    FactBudget,
    FactMediaDaily,
    FactOrdersDaily,
)

_Z = Decimal("0")
_DF = DecimalField()


# ═══════════════════════════════════════════════════════════════════════════
# DATE ENGINE
# Week = Monday–Sunday.  Pacing = equal elapsed days.
# YOY uses 52-week offset to preserve day-of-week alignment.
# ═══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class DateWindow:
    start: date
    end: date

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def contains(self, d: date) -> bool:
        return self.start <= d <= self.end

    def overlap_days(self, other: "DateWindow") -> int:
        """Days of overlap between two windows (0 if disjoint)."""
        o_start = max(self.start, other.start)
        o_end = min(self.end, other.end)
        return max((o_end - o_start).days + 1, 0)

    # ── SQL helpers (for raw queries, debugging, external tools) ────

    def sql_where(self, col: str = "dd.date") -> str:
        """
        Inline SQL WHERE fragment.

        Fact tables join to DimDate via FK, so the natural pattern is:

            SELECT ...
            FROM dashboard_factmediadaily f
            JOIN dashboard_dimdate dd ON f.date_id = dd.id
            WHERE {window.sql_where()}

        For FactOrdersDaily the same pattern applies:

            SELECT ...
            FROM dashboard_factordersdaily o
            JOIN dashboard_dimdate dd ON o.date_id = dd.id
            WHERE {window.sql_where('dd.date')}
        """
        return f"{col} BETWEEN '{self.start}' AND '{self.end}'"

    def sql_params(self, col: str = "dd.date") -> tuple:
        """Parameterized version → (fragment, [start, end])."""
        return (f"{col} BETWEEN %s AND %s", [self.start, self.end])


@dataclass(frozen=True)
class Period:
    current: DateWindow
    compare: DateWindow
    label: str
    is_partial: bool = False

    @property
    def pacing_note(self) -> str:
        if self.is_partial:
            return (
                f"Pacing: {self.current.days} elapsed day(s) — "
                f"comparison trimmed to same window length"
            )
        return (
            f"Full period: {self.current.days} day(s) "
            f"vs {self.compare.days} day(s)"
        )


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _month_end(d: date) -> date:
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def _shift_month(d: date, months: int) -> date:
    """Shift by N months, clamping day to destination month-end."""
    m = d.month + months
    y = d.year + (m - 1) // 12
    m = (m - 1) % 12 + 1
    max_day = calendar.monthrange(y, m)[1]
    return date(y, m, min(d.day, max_day))


PRESET_CHOICES = [
    ("this_week", "This Week"),
    ("last_week", "Last Week"),
    ("this_month", "This Month"),
    ("last_month", "Last Month"),
    ("this_quarter", "This Quarter"),
    ("custom", "Custom"),
]

CMP_CHOICES = [
    ("wow", "WOW"),
    ("mom", "MOM"),
    ("yoy", "YOY"),
    ("custom", "Custom"),
]

CMP_LABELS = {
    "wow": "vs Prior Week",
    "yoy": "vs Prior Year",
    "mom": "vs Prior Month",
    "custom": "vs Custom",
}


def resolve_period(
    preset: str = "this_week",
    comparison: str = "wow",
    custom_start: Optional[date] = None,
    custom_end: Optional[date] = None,
    compare_start: Optional[date] = None,
    compare_end: Optional[date] = None,
    today: Optional[date] = None,
) -> Period:
    """
    Build a Period (current window + comparison window) from user inputs.

    Rules
    ─────
    1. Week = Monday–Sunday (ISO).  Default: This Week + WOW.
    2. Partial-period presets (this_week, this_month, this_quarter)
       use **pacing**: the comparison window is trimmed to the same
       number of elapsed days so we compare Mon–Wed vs Mon–Wed, not
       Mon–Wed vs Mon–Sun.
    3. Full-period presets (last_week, last_month) compare the entire
       prior period.  WOW shifts the whole window back 7 days; MOM
       compares full month-to-month; YOY uses 52-week offset (364 days)
       to preserve day-of-week alignment.
    4. Custom ranges: inverted start/end is silently swapped.
       Custom comparison defaults to the same duration shifted back
       one week if no explicit dates are supplied.

    Edge cases handled
    ──────────────────
    - Monday (this_week = 1 day): pacing trims compare to 1 day.
    - 1st of month (this_month = 1 day): same.
    - Sunday (this_week = 7 days): pacing still applied — result is
      identical to full week, which is correct.
    - YOY: 52-week (364-day) offset preserves weekday alignment at
      the cost of ~1 day calendar drift per year.  Leap years shift
      the calendar date but keep the weekday.
    - MOM full period: uses calendar month-end of the destination
      month, so Feb (28 days) vs Jan (31 days) is intentional —
      full-to-full respects natural month boundaries.
    - MOM partial + pacing: e.g. Mar 1-15 compares to Feb 1-15.
      If the destination month is shorter (say 28 days) and elapsed
      exceeds it, the window extends past month-end.  This is
      preferred over clamping, which would silently lose days.
    """
    today = today or date.today()

    # ── current window ────────────────────────────────────────
    if preset == "this_week":
        c0 = _monday(today)
        c1 = today
    elif preset == "last_week":
        c0 = _monday(today) - timedelta(weeks=1)
        c1 = c0 + timedelta(days=6)
    elif preset == "this_month":
        c0 = _month_start(today)
        c1 = today
    elif preset == "last_month":
        c1 = _month_start(today) - timedelta(days=1)
        c0 = _month_start(c1)
    elif preset == "this_quarter":
        q_month = ((today.month - 1) // 3) * 3 + 1
        c0 = date(today.year, q_month, 1)
        c1 = today
    elif preset == "custom":
        c0 = custom_start or (today - timedelta(days=30))
        c1 = custom_end or today
        if c0 > c1:                       # ← edge case: inverted range
            c0, c1 = c1, c0
    else:
        c0 = _monday(today)
        c1 = today

    current = DateWindow(c0, c1)
    elapsed = current.days

    # Partial-period presets use pacing (equal elapsed days).
    # Full-period presets compare full-to-full.
    pacing = preset in ("this_week", "this_month", "this_quarter")

    # ── comparison window ─────────────────────────────────────
    #
    # SQL analogy for every case (join DimDate dd ON fk = dd.id):
    #
    # Base period:
    #   WHERE dd.date BETWEEN '{c0}' AND '{c1}'
    #
    # Comparison period:
    #   WHERE dd.date BETWEEN '{p0}' AND '{p1}'
    #
    if comparison == "wow":
        p0 = c0 - timedelta(weeks=1)
        p1 = (p0 + timedelta(days=elapsed - 1)) if pacing else (c1 - timedelta(weeks=1))

    elif comparison == "yoy":
        # 52-week offset (364 days) — preserves day-of-week alignment.
        # Always uses elapsed days (even for full periods) because a
        # calendar-year offset would shift the weekday.
        p0 = c0 - timedelta(weeks=52)
        p1 = p0 + timedelta(days=elapsed - 1)

    elif comparison == "mom":
        p0 = _shift_month(c0, -1)
        if pacing:
            p1 = p0 + timedelta(days=elapsed - 1)
        else:
            p1 = _month_end(p0)

    elif comparison == "custom":
        p0 = compare_start or (c0 - timedelta(weeks=1))
        p1 = compare_end or (p0 + timedelta(days=elapsed - 1))
        if p0 > p1:                       # ← edge case: inverted range
            p0, p1 = p1, p0

    else:
        # Unrecognised comparison type → fall back to WOW with pacing
        p0 = c0 - timedelta(weeks=1)
        p1 = p0 + timedelta(days=elapsed - 1)

    compare = DateWindow(p0, p1)
    label = CMP_LABELS.get(comparison, comparison)
    return Period(current, compare, label, is_partial=pacing)


# ═══════════════════════════════════════════════════════════════════════════
# QUERY HELPERS
# ═══════════════════════════════════════════════════════════════════════════


def _media_qs(window, **campaign_filters):
    qs = FactMediaDaily.objects.filter(
        date__date__gte=window.start, date__date__lte=window.end,
    )
    if campaign_filters:
        qs = qs.filter(**campaign_filters)
    return qs


def _media_agg(qs, group_field):
    return {
        r[group_field]: r
        for r in qs.values(group_field).annotate(
            spend=Coalesce(Sum("cost"), _Z, output_field=_DF),
            impressions=Coalesce(Sum("impressions"), 0),
            clicks=Coalesce(Sum("clicks"), 0),
            conversions=Coalesce(Sum("conversions"), 0),
            conv_value=Coalesce(Sum("conversion_value"), _Z, output_field=_DF),
        )
    }


def media_by_brand(window, vertical_id=None):
    kw = {}
    if vertical_id:
        kw["campaign__brand__vertical_id"] = vertical_id
    return _media_agg(
        _media_qs(window, **kw), "campaign__brand_id"
    )


def media_by_source(window, brand_id):
    return _media_agg(
        _media_qs(window, campaign__brand_id=brand_id),
        "campaign__source_id",
    )


def media_by_campaign_type(window, brand_id, source_id):
    return _media_agg(
        _media_qs(window, campaign__brand_id=brand_id,
                  campaign__source_id=source_id),
        "campaign__campaign_type_id",
    )


def media_by_campaign(window, brand_id, source_id, type_id):
    return _media_agg(
        _media_qs(window, campaign__brand_id=brand_id,
                  campaign__source_id=source_id,
                  campaign__campaign_type_id=type_id),
        "campaign_id",
    )


def orders_by_brand(window, vertical_id=None, rev_type="net"):
    qs = FactOrdersDaily.objects.filter(
        date__date__gte=window.start, date__date__lte=window.end,
    )
    if vertical_id:
        qs = qs.filter(brand__vertical_id=vertical_id)
    rev_field = "net_revenue" if rev_type == "net" else "new_revenue"
    return {
        r["brand_id"]: r
        for r in qs.values("brand_id").annotate(
            orders=Coalesce(Sum("orders"), 0),
            revenue=Coalesce(Sum(rev_field), _Z, output_field=_DF),
        )
    }


def budgets_for_period(window):
    """Monthly budgets prorated to the overlap with *window*.

    Revenue budget is adjusted upward by the brand's cancellation rate:
      adjusted = revenue_budget / (1 − cancellation_rate)
    so the goal accounts for expected cancellations.
    """
    qs = FactBudget.objects.filter(
        month__date__gte=_month_start(window.start),
        month__date__lte=window.end,
    ).select_related("month")

    result = {}
    for b in qs:
        bid = b.brand_id
        m_days = calendar.monthrange(b.month.date.year, b.month.date.month)[1]
        o_start = max(b.month.date, window.start)
        o_end = min(_month_end(b.month.date), window.end)
        overlap = max((o_end - o_start).days + 1, 0)
        prorate = Decimal(str(overlap)) / Decimal(str(m_days))

        cancel = b.cancellation_rate or _Z
        divisor = Decimal("1") - cancel
        if divisor > _Z:
            adjusted = b.revenue_budget / divisor
        else:
            adjusted = b.revenue_budget

        if bid not in result:
            result[bid] = {"revenue_budget": _Z, "mts_budget": float(b.mts_budget)}
        result[bid]["revenue_budget"] += adjusted * prorate
    return result


# ═══════════════════════════════════════════════════════════════════════════
# METRIC HELPERS
# ═══════════════════════════════════════════════════════════════════════════


def _pct(cur, prev):
    """% change or None."""
    if prev and float(prev) != 0:
        return round((float(cur) - float(prev)) / abs(float(prev)), 4)
    return None


def _div(num, denom):
    """Safe division → float or None."""
    if denom and float(denom) != 0:
        return round(float(num) / float(denom), 4)
    return None


def compare_delta(cur, cmp):
    """
    Compute absolute delta and % change with explicit missing-data rules.

    Returns (abs_delta, pct_delta, status) where status is one of:
      "ok"           — both values present and non-zero comparison
      "no_compare"   — comparison value is missing or zero (delta = None)
      "no_current"   — current value is missing or zero (delta = None)
      "no_data"      — both missing or zero

    Usage:
        abs_d, pct_d, status = compare_delta(cur_spend, cmp_spend)
        if status == "ok":
            # render delta badge
        elif status == "no_compare":
            # show "—" or "New" indicator
        ...

    Edge cases:
    - Prior period has no data (new brand/campaign):
        compare_delta(1500, 0) → (None, None, "no_compare")
    - Current period has no data (paused campaign):
        compare_delta(0, 1500) → (-1500, -1.0, "ok")
        Note: zero current is still valid — it means the metric dropped to 0.
        But if both current and comparison are zero:
        compare_delta(0, 0)    → (None, None, "no_data")
    - Comparison value is None (no DB rows):
        compare_delta(1500, None) → (None, None, "no_compare")
    """
    cur_f = float(cur) if cur is not None else 0.0
    cmp_f = float(cmp) if cmp is not None else 0.0
    cur_missing = cur is None or cur_f == 0.0
    cmp_missing = cmp is None or cmp_f == 0.0

    if cur_missing and cmp_missing:
        return None, None, "no_data"
    if cmp_missing:
        return None, None, "no_compare"
    if cur_missing:
        # Current is zero but compare has data → real decline
        return round(cur_f - cmp_f, 4), round(-1.0, 4), "no_current"

    abs_delta = round(cur_f - cmp_f, 4)
    pct_delta = round(abs_delta / abs(cmp_f), 4)
    return abs_delta, pct_delta, "ok"


# ═══════════════════════════════════════════════════════════════════════════
# ALERT CLASSIFICATION
# ═══════════════════════════════════════════════════════════════════════════

ALERT_META = {
    "needs_attention": {
        "label": "Needs Attn",
        "color": "red",
        "icon": "exclamation triangle",
        "severity": "High",
        "tooltip": (
            "Revenue is below 80% of last year's YTD "
            "for the same period."
        ),
        "cta": (
            "Drill into Source to find which platform lost volume; "
            "check for paused campaigns or broken tracking."
        ),
        "drill_target": "Source",
    },
    "doing_well": {
        "label": "Doing Well",
        "color": "green",
        "icon": "check circle",
        "severity": "Low",
        "tooltip": (
            "Revenue is growing 10%+ YOY and MTS is within "
            "50 bps of the budget target."
        ),
        "cta": (
            "No immediate action — consider scaling spend if "
            "impression share is below 80%."
        ),
        "drill_target": "Source",
    },
    "overly_efficient": {
        "label": "Over Eff.",
        "color": "blue",
        "icon": "arrow up",
        "severity": "Med",
        "tooltip": (
            "MTS is 50+ basis points below budget — "
            "underspending relative to revenue opportunity."
        ),
        "cta": (
            "Drill into Campaign Type to identify where to "
            "increase bids or daily budgets."
        ),
        "drill_target": "Campaign Type",
    },
    "under_efficient": {
        "label": "Under Eff.",
        "color": "red",
        "icon": "arrow down",
        "severity": "Med",
        "tooltip": (
            "MTS is 50+ basis points above budget — "
            "overspending relative to revenue."
        ),
        "cta": (
            "Drill into Campaign Type to identify where to "
            "reduce bids or pause underperforming campaigns."
        ),
        "drill_target": "Campaign Type",
    },
    "pacing_risk": {
        "label": "Pacing Risk",
        "color": "orange",
        "icon": "warning sign",
        "severity": "Med",
        "tooltip": (
            "Revenue dropped 10%+ WOW while spend held "
            "steady or increased."
        ),
        "cta": (
            "Drill into Campaign to check for conversion-rate "
            "drops, landing-page issues, or competitor shifts."
        ),
        "drill_target": "Campaign",
    },
    "missing_budget": {
        "label": "No Budget",
        "color": "grey",
        "icon": "question circle",
        "severity": "Low",
        "tooltip": (
            "No budget target entered for this brand/period."
        ),
        "cta": (
            "Enter a monthly revenue and MTS budget so "
            "efficiency alerts can fire."
        ),
        "drill_target": "Brand",
    },
    "missing_revenue": {
        "label": "No Revenue",
        "color": "grey",
        "icon": "question circle",
        "severity": "Med",
        "tooltip": (
            "No revenue/order data uploaded for this brand "
            "in the current period."
        ),
        "cta": (
            "Upload the daily orders CSV — MTS, AOV, and "
            "revenue alerts are blocked until data arrives."
        ),
        "drill_target": "Brand",
    },
}


def classify_brand(
    cur_rev, yoy_rev, cur_mts, mts_budget,
    cur_spend, cmp_spend, cmp_rev,
    has_budget, has_revenue,
):
    """
    Classify a brand into zero or more alert buckets.

    Returns a list of alert keys (strings) matching ALERT_META keys.
    A brand can carry multiple simultaneous alerts (e.g. overly_efficient
    + missing_revenue).

    SQL CASE equivalents (for documentation / raw-query auditing)
    ─────────────────────────────────────────────────────────────
    All expressions assume a pre-aggregated CTE with columns:
      cur_rev, yoy_rev, cur_mts, mts_budget,
      cur_spend, cmp_spend, cmp_rev,
      has_budget (0/1), has_revenue (0/1)

    needs_attention:
      CASE WHEN yoy_rev > 0
            AND cur_rev < yoy_rev * 0.80
           THEN 1 ELSE 0 END

    doing_well:
      CASE WHEN yoy_rev > 0
            AND mts_budget > 0
            AND cur_mts IS NOT NULL
            AND (cur_rev - yoy_rev) / yoy_rev >= 0.10
            AND ABS(cur_mts - mts_budget) <= 0.0050
           THEN 1 ELSE 0 END

    overly_efficient:
      CASE WHEN mts_budget > 0
            AND cur_mts IS NOT NULL
            AND mts_budget - cur_mts >= 0.0050
           THEN 1 ELSE 0 END

    under_efficient:
      CASE WHEN mts_budget > 0
            AND cur_mts IS NOT NULL
            AND cur_mts - mts_budget >= 0.0050
           THEN 1 ELSE 0 END

    pacing_risk:
      CASE WHEN cmp_rev > 0
            AND cmp_spend > 0
            AND (cur_rev - cmp_rev) / cmp_rev < -0.10
            AND (cur_spend - cmp_spend) / cmp_spend > -0.10
           THEN 1 ELSE 0 END

    missing_budget:
      CASE WHEN has_budget = 0 THEN 1 ELSE 0 END

    missing_revenue:
      CASE WHEN has_revenue = 0 THEN 1 ELSE 0 END
    """
    alerts = []

    # ── Needs Attention: revenue < 80 % of same-period last year ──
    if yoy_rev and float(yoy_rev) > 0 and float(cur_rev) < float(yoy_rev) * 0.80:
        alerts.append("needs_attention")

    # ── Doing Well: ≥ 10 % YOY growth AND within ±50 bps of MTS budget ──
    if (
        yoy_rev
        and float(yoy_rev) > 0
        and mts_budget
        and cur_mts is not None
    ):
        yoy_g = (float(cur_rev) - float(yoy_rev)) / float(yoy_rev)
        if yoy_g >= 0.10 and abs(cur_mts - mts_budget) <= 0.0050:
            alerts.append("doing_well")

    # ── Overly Efficient: MTS ≥ 50 bps lower (better) than budget ──
    if mts_budget and cur_mts is not None:
        if mts_budget - cur_mts >= 0.0050:
            alerts.append("overly_efficient")

    # ── Under Efficient: MTS ≥ 50 bps higher (worse) than budget ──
    if mts_budget and cur_mts is not None:
        if cur_mts - mts_budget >= 0.0050:
            alerts.append("under_efficient")

    # ── Pacing Risk: WOW revenue down while spend flat / up ──
    if cmp_rev and cmp_spend and float(cmp_rev) > 0 and float(cmp_spend) > 0:
        rev_chg = (float(cur_rev) - float(cmp_rev)) / float(cmp_rev)
        spd_chg = (float(cur_spend) - float(cmp_spend)) / float(cmp_spend)
        if rev_chg < -0.10 and spd_chg > -0.10:
            alerts.append("pacing_risk")

    # ── Data quality ──
    if not has_budget:
        alerts.append("missing_budget")
    if not has_revenue:
        alerts.append("missing_revenue")

    return alerts


def exceptions_summary(brand_rows):
    """Aggregate alert counts for the exceptions panel."""
    counts = {}
    for row in brand_rows:
        for a in row["alerts"]:
            counts[a] = counts.get(a, 0) + 1
    return {
        k: {"count": v, **ALERT_META[k]}
        for k, v in counts.items()
        if v > 0
    }


# ═══════════════════════════════════════════════════════════════════════════
# BRAND TABLE  (top-level aggregation)
# ═══════════════════════════════════════════════════════════════════════════


def brand_table(period, vertical_id=None, rev_type="net"):
    """
    One row per brand.  Spend from FactMediaDaily, revenue from FactOrdersDaily.
    Deltas vs comparison period.  Alerts vs YOY + budget.
    """
    mc = media_by_brand(period.current, vertical_id)
    oc = orders_by_brand(period.current, vertical_id, rev_type)
    mp = media_by_brand(period.compare, vertical_id)
    op = orders_by_brand(period.compare, vertical_id, rev_type)

    # YOY — always needed for alerts regardless of selected comparison
    yoy_win = DateWindow(
        period.current.start - timedelta(weeks=52),
        period.current.end - timedelta(weeks=52),
    )
    oy = orders_by_brand(yoy_win, vertical_id, rev_type)

    budgets = budgets_for_period(period.current)

    brands = DimBrand.objects.select_related("vertical")
    if vertical_id:
        brands = brands.filter(vertical_id=vertical_id)

    rows = []
    for b in brands:
        bid = b.id
        cur_spend = float(mc.get(bid, {}).get("spend", 0))
        cur_rev = float(oc.get(bid, {}).get("revenue", 0))
        cur_orders = oc.get(bid, {}).get("orders", 0)
        cur_clicks = mc.get(bid, {}).get("clicks", 0)
        cur_conversions = mc.get(bid, {}).get("conversions", 0)

        cmp_spend = float(mp.get(bid, {}).get("spend", 0))
        cmp_rev = float(op.get(bid, {}).get("revenue", 0))

        yoy_rev = float(oy.get(bid, {}).get("revenue", 0))

        bgt = budgets.get(bid, {})
        rev_budget = float(bgt.get("revenue_budget", 0))
        mts_budget = bgt.get("mts_budget", 0)

        cur_mts = _div(cur_spend, cur_rev)
        cmp_mts = _div(cmp_spend, cmp_rev)

        alerts = classify_brand(
            cur_rev=cur_rev, yoy_rev=yoy_rev,
            cur_mts=cur_mts, mts_budget=mts_budget,
            cur_spend=cur_spend, cmp_spend=cmp_spend,
            cmp_rev=cmp_rev,
            has_budget=bid in budgets,
            has_revenue=bid in oc,
        )

        rows.append({
            "id": bid,
            "name": b.name,
            "vertical": b.vertical.name,
            "spend": cur_spend,
            "spend_cmp": cmp_spend,
            "spend_delta": _pct(cur_spend, cmp_spend),
            "revenue": cur_rev,
            "revenue_cmp": cmp_rev,
            "revenue_delta": _pct(cur_rev, cmp_rev),
            "revenue_budget": rev_budget,
            "revenue_vs_budget": _pct(cur_rev, rev_budget) if rev_budget else None,
            "mts": cur_mts,
            "mts_cmp": cmp_mts,
            "mts_delta": (
                round(cur_mts - cmp_mts, 4)
                if cur_mts is not None and cmp_mts is not None
                else None
            ),
            "mts_budget": mts_budget,
            "mts_vs_budget": (
                round(cur_mts - mts_budget, 4)
                if cur_mts is not None and mts_budget
                else None
            ),
            "orders": cur_orders,
            "aov": _div(cur_rev, cur_orders),
            "clicks": cur_clicks,
            "conversions": cur_conversions,
            "net_cvr": _div(cur_orders, cur_clicks),
            "mkt_cvr": _div(cur_conversions, cur_clicks),
            "alerts": alerts,
        })

    return sorted(rows, key=lambda r: r.get("spend") or 0, reverse=True)


# ═══════════════════════════════════════════════════════════════════════════
# DRILL TABLE  (sub-brand aggregation)
# Revenue is allocated to sub-levels by spend share.
# ═══════════════════════════════════════════════════════════════════════════


def drill_table(period, group_by, rev_type="net", **filters):
    brand_id = filters["brand_id"]

    # Brand-level revenue for allocation
    oc = orders_by_brand(period.current, rev_type=rev_type)
    op = orders_by_brand(period.compare, rev_type=rev_type)
    brand_rev_cur = float(oc.get(brand_id, {}).get("revenue", 0))
    brand_rev_cmp = float(op.get(brand_id, {}).get("revenue", 0))

    # Media at requested grain
    if group_by == "source":
        mc = media_by_source(period.current, brand_id)
        mp = media_by_source(period.compare, brand_id)
        objs = {s.id: s.name for s in DimSource.objects.all()}
    elif group_by == "campaign_type":
        mc = media_by_campaign_type(period.current, brand_id, filters["source_id"])
        mp = media_by_campaign_type(period.compare, brand_id, filters["source_id"])
        objs = {t.id: t.name for t in DimCampaignType.objects.all()}
    elif group_by == "campaign":
        mc = media_by_campaign(
            period.current, brand_id, filters["source_id"], filters["type_id"]
        )
        mp = media_by_campaign(
            period.compare, brand_id, filters["source_id"], filters["type_id"]
        )
        objs = {
            c.id: c.name
            for c in DimCampaign.objects.filter(
                brand_id=brand_id,
                source_id=filters["source_id"],
                campaign_type_id=filters["type_id"],
            )
        }
    else:
        return []

    total_spend_cur = sum(float(v.get("spend", 0)) for v in mc.values())
    total_spend_cmp = sum(float(v.get("spend", 0)) for v in mp.values())

    rows = []
    for obj_id, name in objs.items():
        cur = mc.get(obj_id, {})
        cmp = mp.get(obj_id, {})

        cur_spend = float(cur.get("spend", 0))
        cmp_spend = float(cmp.get("spend", 0))
        if cur_spend == 0 and cmp_spend == 0:
            continue

        # Spend-weighted revenue allocation
        cur_rev = (
            brand_rev_cur * (cur_spend / total_spend_cur)
            if total_spend_cur
            else 0
        )
        cmp_rev = (
            brand_rev_cmp * (cmp_spend / total_spend_cmp)
            if total_spend_cmp
            else 0
        )

        cur_mts = _div(cur_spend, cur_rev)
        cmp_mts = _div(cmp_spend, cmp_rev)

        rows.append({
            "id": obj_id,
            "name": name,
            "spend": cur_spend,
            "spend_delta": _pct(cur_spend, cmp_spend),
            "revenue": round(cur_rev, 2),
            "revenue_delta": _pct(cur_rev, cmp_rev),
            "mts": cur_mts,
            "mts_delta": (
                round(cur_mts - cmp_mts, 4)
                if cur_mts is not None and cmp_mts is not None
                else None
            ),
            "clicks": cur.get("clicks", 0),
            "conversions": cur.get("conversions", 0),
        })

    return sorted(rows, key=lambda r: r["spend"], reverse=True)


# ═══════════════════════════════════════════════════════════════════════════
# TREND DATA  (for Google Charts)
# ═══════════════════════════════════════════════════════════════════════════


def daily_trend(period, vertical_id=None, rev_type="net", preset="this_week"):
    """Spend + revenue trend for current and comparison windows.

    Label precision follows the period filter:
      - week presets  → daily, labelled "Mon 2/3"
      - month presets → daily, labelled "3" (day of month)
      - quarter       → aggregated by month, labelled "Jan"
      - custom        → daily, labelled "2/3"
    """
    monthly = preset == "this_quarter"

    def _build(window):
        kw = {}
        if vertical_id:
            kw["campaign__brand__vertical_id"] = vertical_id

        media_qs = (
            _media_qs(window, **kw)
            .values("date__date")
            .annotate(
                s=Coalesce(Sum("cost"), _Z, output_field=_DF),
                c=Coalesce(Sum("clicks"), 0),
            )
            .order_by("date__date")
        )
        media_days = {r["date__date"]: r for r in media_qs}

        okw = {}
        if vertical_id:
            okw["brand__vertical_id"] = vertical_id
        rev_field = "net_revenue" if rev_type == "net" else "new_revenue"
        order_qs = (
            FactOrdersDaily.objects.filter(
                date__date__gte=window.start, date__date__lte=window.end,
                **okw,
            )
            .values("date__date")
            .annotate(
                r=Coalesce(Sum(rev_field), _Z, output_field=_DF),
                o=Coalesce(Sum("orders"), 0),
            )
            .order_by("date__date")
        )
        order_days = {r["date__date"]: r for r in order_qs}

        all_dates = sorted(set(media_days) | set(order_days))

        if monthly:
            buckets = {}
            for d in all_dates:
                key = (d.year, d.month)
                if key not in buckets:
                    buckets[key] = {
                        "spend": 0, "revenue": 0, "orders": 0, "clicks": 0,
                        "date": d,
                    }
                m = media_days.get(d, {})
                o = order_days.get(d, {})
                buckets[key]["spend"] += float(m.get("s", 0))
                buckets[key]["revenue"] += float(o.get("r", 0))
                buckets[key]["orders"] += o.get("o", 0)
                buckets[key]["clicks"] += m.get("c", 0)
            return [
                {
                    "date": v["date"].isoformat(),
                    "day": date(k[0], k[1], 1).strftime("%b"),
                    "spend": v["spend"],
                    "revenue": v["revenue"],
                    "orders": v["orders"],
                    "clicks": v["clicks"],
                }
                for k, v in sorted(buckets.items())
            ]

        def _label(d):
            if preset in ("this_week", "last_week"):
                return d.strftime("%a %-m/%-d")
            if preset in ("this_month", "last_month"):
                return str(d.day)
            return d.strftime("%-m/%-d")

        return [
            {
                "date": d.isoformat(),
                "day": _label(d),
                "spend": float(media_days.get(d, {}).get("s", 0)),
                "revenue": float(order_days.get(d, {}).get("r", 0)),
                "orders": order_days.get(d, {}).get("o", 0),
                "clicks": media_days.get(d, {}).get("c", 0),
            }
            for d in all_dates
        ]

    return {"current": _build(period.current), "compare": _build(period.compare)}
