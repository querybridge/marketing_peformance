"""
Weekly optimization scoring engine.

Scores campaigns on scalability and recommends tROAS or Seasonality
Adjustment actions to help each Vertical hit monthly NET revenue goals
within +/-50 bps of MTS.
"""

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import List, Optional, Tuple

from django.db.models import DecimalField, Sum, F
from django.db.models.functions import Coalesce

from .models import (
    DimCampaign,
    FactBudget,
    FactMediaDaily,
    FactVerticalBudget,
    ScoringConfig,
)
from .services import DateWindow, _div, _media_qs, _Z, _DF

# ═══════════════════════════════════════════════════════════════════════════
# PERIOD CONSTRUCTION
# ═══════════════════════════════════════════════════════════════════════════

OPT_PRESET_CHOICES = [
    ("last_week", "Last Week (Mon\u2013Sun)"),
    ("last_7", "Last 7 Days"),
    ("mtd", "Month-to-Date"),
]


@dataclass(frozen=True)
class OptimizationPeriod:
    analysis: DateWindow
    elasticity: DateWindow
    efficiency: DateWindow


def resolve_optimization_period(
    preset: str = "last_week",
    elasticity_days: int = 14,
    efficiency_days: int = 28,
    today: Optional[date] = None,
) -> OptimizationPeriod:
    today = today or date.today()

    if preset == "last_week":
        monday = today - timedelta(days=today.weekday())
        end = monday - timedelta(days=1)        # prior Sunday
        start = end - timedelta(days=6)          # prior Monday
    elif preset == "last_7":
        end = today - timedelta(days=1)
        start = end - timedelta(days=6)
    elif preset == "mtd":
        start = today.replace(day=1)
        end = today - timedelta(days=1)
        if end < start:
            end = start
    else:
        # fallback to last_week
        monday = today - timedelta(days=today.weekday())
        end = monday - timedelta(days=1)
        start = end - timedelta(days=6)

    analysis = DateWindow(start, end)
    elast_start = end - timedelta(days=elasticity_days - 1)
    efficiency_start = end - timedelta(days=efficiency_days - 1)

    return OptimizationPeriod(
        analysis=analysis,
        elasticity=DateWindow(elast_start, end),
        efficiency=DateWindow(efficiency_start, end),
    )


# ═══════════════════════════════════════════════════════════════════════════
# COMPONENT A: SPEND -> CLICK ELASTICITY
# ═══════════════════════════════════════════════════════════════════════════


def compute_elasticity(campaign_id: int, window: DateWindow) -> float:
    """Log-log regression of daily spend vs clicks. Returns 0.0-1.0."""
    rows = (
        FactMediaDaily.objects.filter(
            campaign_id=campaign_id,
            date__date__gte=window.start,
            date__date__lte=window.end,
        )
        .values("date__date")
        .annotate(
            spend=Coalesce(Sum("cost"), _Z, output_field=_DF),
            clicks=Coalesce(Sum("clicks"), 0),
        )
    )

    # Collect valid days (spend > 0 and clicks > 0 for log-log)
    points = []
    for r in rows:
        s = float(r["spend"])
        c = r["clicks"]
        if s > 0 and c > 0:
            points.append((math.log(s), math.log(c)))

    if len(points) < 3:
        return 0.5  # neutral

    n = len(points)
    sum_x = sum(p[0] for p in points)
    sum_y = sum(p[1] for p in points)
    sum_xy = sum(p[0] * p[1] for p in points)
    sum_x2 = sum(p[0] ** 2 for p in points)

    denom = n * sum_x2 - sum_x ** 2
    if denom == 0:
        return 0.5  # no variance in spend

    beta = (n * sum_xy - sum_x * sum_y) / denom

    # Clamp to [0.0, 1.0]
    return max(0.0, min(1.0, beta))


# ═══════════════════════════════════════════════════════════════════════════
# COMPONENT B: BUDGET BINDING PROXY
# ═══════════════════════════════════════════════════════════════════════════


def compute_budget_binding(
    campaign_spend: float,
    vert_total_spend: float,
    vert_budget_spend: float,
    ly_campaign_spend: float,
    ly_vert_spend: float,
) -> float:
    """Room-to-grow score based on LY spend share and current utilization.

    Returns 0.0-1.0 where higher = more room to grow.
    """
    if ly_vert_spend <= 0 or vert_budget_spend <= 0:
        return 0.5  # neutral when no LY data

    ly_share = ly_campaign_spend / ly_vert_spend
    implied_budget = ly_share * vert_budget_spend
    if implied_budget <= 0:
        return 0.5

    utilization = campaign_spend / implied_budget
    score = 1.0 - utilization
    return max(0.0, min(1.0, score))


# ═══════════════════════════════════════════════════════════════════════════
# COMPONENT C: EFFICIENCY STABILITY
# ═══════════════════════════════════════════════════════════════════════════


def compute_efficiency_stability(
    campaign_id: int,
    window: DateWindow,
    peer_roas_cv: float,
    peer_cvr_cv: float,
) -> float:
    """Weekly ROAS and CVR coefficient of variation, shrunk toward peer group.

    Lower CV = more stable = higher score. Returns 0.0-1.0.
    """
    # Get weekly aggregates within the window
    rows = (
        FactMediaDaily.objects.filter(
            campaign_id=campaign_id,
            date__date__gte=window.start,
            date__date__lte=window.end,
        )
        .values("date__year_week")
        .annotate(
            spend=Coalesce(Sum("cost"), _Z, output_field=_DF),
            clicks=Coalesce(Sum("clicks"), 0),
            conversions=Coalesce(Sum("conversions"), 0),
            conv_value=Coalesce(Sum("conversion_value"), _Z, output_field=_DF),
        )
    )

    weekly_roas = []
    weekly_cvr = []
    for r in rows:
        spend = float(r["spend"])
        clicks = r["clicks"]
        conversions = r["conversions"]
        conv_value = float(r["conv_value"])

        if spend > 0:
            weekly_roas.append(conv_value / spend)
        if clicks > 0:
            weekly_cvr.append(conversions / clicks)

    n_weeks = len(weekly_roas)
    alpha = min(n_weeks / 4.0, 1.0)  # shrinkage factor

    def _cv(values):
        if len(values) < 2:
            return 0.0
        mean = sum(values) / len(values)
        if mean == 0:
            return 0.0
        var = sum((v - mean) ** 2 for v in values) / len(values)
        return math.sqrt(var) / abs(mean)

    camp_roas_cv = _cv(weekly_roas)
    camp_cvr_cv = _cv(weekly_cvr)

    # Shrink toward peer
    blended_roas_cv = alpha * camp_roas_cv + (1 - alpha) * peer_roas_cv
    blended_cvr_cv = alpha * camp_cvr_cv + (1 - alpha) * peer_cvr_cv

    avg_cv = (blended_roas_cv + blended_cvr_cv) / 2.0

    # Lower CV → higher score. Map CV to score: CV=0 → 1.0, CV>=1 → 0.0
    score = 1.0 - avg_cv
    return max(0.0, min(1.0, score))


# ═══════════════════════════════════════════════════════════════════════════
# PEER GROUP
# ═══════════════════════════════════════════════════════════════════════════


def compute_peer_group_stats(
    vertical_id: int,
    source_id: int,
    campaign_type_id: int,
    window: DateWindow,
) -> dict:
    """Peer group = same source x campaign_type within vertical.

    Returns {peer_cvr, peer_roas_cv, peer_cvr_cv}.
    """
    qs = FactMediaDaily.objects.filter(
        campaign__brand__vertical_id=vertical_id,
        campaign__source_id=source_id,
        campaign__campaign_type_id=campaign_type_id,
        date__date__gte=window.start,
        date__date__lte=window.end,
    )

    # Overall peer CVR
    totals = qs.aggregate(
        total_clicks=Coalesce(Sum("clicks"), 0),
        total_conversions=Coalesce(Sum("conversions"), 0),
    )
    peer_clicks = totals["total_clicks"]
    peer_conversions = totals["total_conversions"]
    peer_cvr = peer_conversions / peer_clicks if peer_clicks > 0 else 0.0

    # Weekly peer-level CV for ROAS and CVR
    weekly = (
        qs.values("date__year_week")
        .annotate(
            spend=Coalesce(Sum("cost"), _Z, output_field=_DF),
            clicks=Coalesce(Sum("clicks"), 0),
            conversions=Coalesce(Sum("conversions"), 0),
            conv_value=Coalesce(Sum("conversion_value"), _Z, output_field=_DF),
        )
    )

    weekly_roas = []
    weekly_cvr = []
    for r in weekly:
        spend = float(r["spend"])
        clicks = r["clicks"]
        conversions = r["conversions"]
        conv_value = float(r["conv_value"])
        if spend > 0:
            weekly_roas.append(conv_value / spend)
        if clicks > 0:
            weekly_cvr.append(conversions / clicks)

    def _cv(values):
        if len(values) < 2:
            return 0.0
        mean = sum(values) / len(values)
        if mean == 0:
            return 0.0
        var = sum((v - mean) ** 2 for v in values) / len(values)
        return math.sqrt(var) / abs(mean)

    return {
        "peer_cvr": peer_cvr,
        "peer_roas_cv": _cv(weekly_roas),
        "peer_cvr_cv": _cv(weekly_cvr),
    }


# ═══════════════════════════════════════════════════════════════════════════
# INCLUSION FILTER
# ═══════════════════════════════════════════════════════════════════════════


def passes_inclusion(
    clicks_period: int,
    cvr_baseline: float,
) -> Tuple[bool, float]:
    """ExpectedConversions = clicks x CVR_baseline. Must be >= 1."""
    expected = clicks_period * cvr_baseline
    return expected >= 1.0, expected


# ═══════════════════════════════════════════════════════════════════════════
# RECOMMENDATION ENGINE
# ═══════════════════════════════════════════════════════════════════════════


def recommend_action(
    scalability_score: int,
    roas: Optional[float],
    source_cvr: Optional[float],
    efficiency_conversions: float,
    vertical_revenue_pace: Optional[float],
    vertical_mts_pace: Optional[float],
    comp_a: float,
    comp_b: float,
    comp_c: float,
) -> Tuple[str, str, float, List[str]]:
    """Produce an action recommendation.

    Returns (action, detail, magnitude, reason_codes).
    Actions: 'adjust_troas', 'seasonality_adjustment', 'hold'
    """
    reason_codes = []

    # Gather reason codes
    if comp_a >= 0.7:
        reason_codes.append("high_elasticity")
    elif comp_a <= 0.3:
        reason_codes.append("low_elasticity")

    if comp_b >= 0.7:
        reason_codes.append("budget_headroom")
    elif comp_b <= 0.3:
        reason_codes.append("budget_constrained")

    if comp_c >= 0.7:
        reason_codes.append("stable_efficiency")
    elif comp_c <= 0.3:
        reason_codes.append("volatile_efficiency")

    # Check for seasonality adjustment first
    if (
        scalability_score >= 60
        and vertical_revenue_pace is not None
        and vertical_revenue_pace < 0.90
        and vertical_mts_pace is not None
        and vertical_mts_pace <= 1.10
    ):
        # Temporary bid boost
        gap = 0.90 - vertical_revenue_pace
        magnitude = min(round(gap * 100, 1), 15.0)
        magnitude = max(magnitude, 1.0)
        reason_codes.append("revenue_pacing_behind")
        detail = f"+{magnitude}% bid boost (temporary)"
        return "seasonality_adjustment", detail, magnitude, reason_codes

    # tROAS adjustment
    if scalability_score >= 70:
        # High score: lower tROAS to bid more aggressively
        raw_pct = (scalability_score - 70) / 30.0 * 15.0  # 0-15%
        magnitude = min(round(max(raw_pct, 1.0), 1), 15.0)
        reason_codes.append("scale_opportunity")
        detail = f"Decrease tROAS by {magnitude}%"
        return "adjust_troas", detail, -magnitude, reason_codes

    if scalability_score < 40:
        # Low score: increase tROAS to pull back
        raw_pct = (40 - scalability_score) / 40.0 * 15.0  # 0-15%
        magnitude = min(round(max(raw_pct, 1.0), 1), 15.0)
        reason_codes.append("efficiency_concern")
        detail = f"Increase tROAS by {magnitude}%"
        return "adjust_troas", detail, magnitude, reason_codes

    # Score 40-69: hold
    return "hold", "No action recommended", 0.0, reason_codes


# ═══════════════════════════════════════════════════════════════════════════
# VERTICAL ALERTING
# ═══════════════════════════════════════════════════════════════════════════


def check_vertical_alert(
    vertical_id: int,
    analysis_window: DateWindow,
) -> Optional[dict]:
    """Fires when projected net revenue < goal AND projected MTS > MTS goal."""
    from .services import orders_by_brand, media_by_brand

    month_start = analysis_window.start.replace(day=1)
    # Get vertical budget for the current month
    vb = FactVerticalBudget.objects.filter(
        vertical_id=vertical_id,
        month__date=month_start,
    ).first()
    if not vb:
        return None

    # Get MTD actuals
    mtd_window = DateWindow(month_start, analysis_window.end)
    media = media_by_brand(mtd_window, vertical_id)
    orders = orders_by_brand(mtd_window, vertical_id, rev_type="net")

    total_spend = sum(float(r.get("spend", 0)) for r in media.values())
    total_revenue = sum(float(r.get("revenue", 0)) for r in orders.values())

    # Project to month end
    elapsed = (analysis_window.end - month_start).days + 1
    import calendar
    days_in_month = calendar.monthrange(month_start.year, month_start.month)[1]
    if elapsed <= 0:
        return None
    projection_factor = days_in_month / elapsed

    projected_revenue = total_revenue * projection_factor
    projected_spend = total_spend * projection_factor
    revenue_goal = float(vb.revenue_budget)
    mts_goal = float(vb.mts_budget)

    projected_mts = projected_spend / projected_revenue if projected_revenue > 0 else None

    if (
        projected_revenue < revenue_goal
        and projected_mts is not None
        and projected_mts > mts_goal
    ):
        rev_gap = revenue_goal - projected_revenue
        mts_over = (projected_mts - mts_goal) * 10000  # bps
        projected_mts_pct = projected_mts * 100
        return {
            "message": (
                f"Revenue at risk: projected ${projected_revenue:,.0f} vs "
                f"${revenue_goal:,.0f} goal (gap: ${rev_gap:,.0f}). "
                f"Projected MTS: {projected_mts_pct:.1f}% "
                f"({mts_over:+.0f} bps vs target)."
            ),
            "projected_revenue": projected_revenue,
            "revenue_goal": revenue_goal,
            "projected_mts": projected_mts,
            "projected_mts_pct": projected_mts_pct,
            "mts_goal": mts_goal,
        }

    return None


# ═══════════════════════════════════════════════════════════════════════════
# CAMPAIGN SCORE DATACLASS
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class CampaignScore:
    campaign_id: int
    campaign_name: str
    ad_group_name: str
    source_name: str
    campaign_type_name: str
    brand_name: str
    brand_id: int
    spend: float
    clicks: int
    conversions: int
    conv_value: float
    roas: Optional[float]
    mts: Optional[float]
    source_cvr: Optional[float]
    scalability_score: int
    comp_a: float
    comp_b: float
    comp_c: float
    expected_conversions: float
    cvr_baseline: float
    used_peer_cvr: bool
    action: str
    detail: str
    magnitude: float
    reason_codes: List[str] = field(default_factory=list)


# Reason code classification for badge coloring (good / bad / neutral).
REASON_SENTIMENT = {
    "high_elasticity":       "good",
    "low_elasticity":        "bad",
    "budget_headroom":       "good",
    "budget_constrained":    "bad",
    "stable_efficiency":     "good",
    "volatile_efficiency":   "bad",
    "revenue_pacing_behind": "bad",
    "scale_opportunity":     "good",
    "efficiency_concern":    "bad",
}


@dataclass
class BrandGroup:
    """A brand with its campaigns and aggregate metrics for the template."""
    brand_name: str
    brand_id: int
    total_spend: float
    campaign_count: int
    campaigns: List[CampaignScore]
    revenue_at_risk: Optional[dict] = None


# ═══════════════════════════════════════════════════════════════════════════
# MAIN ORCHESTRATOR
# ═══════════════════════════════════════════════════════════════════════════


def build_optimization_table(
    vertical_id: int,
    period: OptimizationPeriod,
    config: ScoringConfig,
    source_id: Optional[int] = None,
) -> List[CampaignScore]:
    """Build the scored campaign table for a vertical."""
    from .services import media_by_brand, orders_by_brand

    # 1. Load active campaigns
    camp_qs = DimCampaign.objects.filter(
        brand__vertical_id=vertical_id,
        status="active",
    ).select_related("brand", "source", "campaign_type")
    if source_id:
        camp_qs = camp_qs.filter(source_id=source_id)

    campaigns = list(camp_qs)
    if not campaigns:
        return []

    campaign_ids = [c.id for c in campaigns]

    # 2. Bulk query — analysis period aggregates per campaign
    analysis_agg = {}
    qs = (
        FactMediaDaily.objects.filter(
            campaign_id__in=campaign_ids,
            date__date__gte=period.analysis.start,
            date__date__lte=period.analysis.end,
        )
        .values("campaign_id")
        .annotate(
            spend=Coalesce(Sum("cost"), _Z, output_field=_DF),
            clicks=Coalesce(Sum("clicks"), 0),
            conversions=Coalesce(Sum("conversions"), 0),
            conv_value=Coalesce(Sum("conversion_value"), _Z, output_field=_DF),
        )
    )
    for r in qs:
        analysis_agg[r["campaign_id"]] = r

    # Vertical-level spend totals for budget binding
    vert_spend_qs = FactMediaDaily.objects.filter(
        campaign__brand__vertical_id=vertical_id,
        date__date__gte=period.analysis.start,
        date__date__lte=period.analysis.end,
    ).aggregate(
        total_spend=Coalesce(Sum("cost"), _Z, output_field=_DF),
    )
    vert_total_spend = float(vert_spend_qs["total_spend"])

    # Vertical budget for current month
    month_start = period.analysis.start.replace(day=1)
    vert_budget = FactVerticalBudget.objects.filter(
        vertical_id=vertical_id,
        month__date=month_start,
    ).first()
    vert_budget_spend = 0.0
    if vert_budget:
        rev = float(vert_budget.revenue_budget)
        mts = float(vert_budget.mts_budget)
        vert_budget_spend = rev * mts

    # LY aggregates (52-week offset)
    ly_start = period.analysis.start - timedelta(weeks=52)
    ly_end = period.analysis.end - timedelta(weeks=52)
    ly_agg = {}
    ly_qs = (
        FactMediaDaily.objects.filter(
            campaign_id__in=campaign_ids,
            date__date__gte=ly_start,
            date__date__lte=ly_end,
        )
        .values("campaign_id")
        .annotate(
            spend=Coalesce(Sum("cost"), _Z, output_field=_DF),
        )
    )
    for r in ly_qs:
        ly_agg[r["campaign_id"]] = float(r["spend"])

    ly_vert_qs = FactMediaDaily.objects.filter(
        campaign__brand__vertical_id=vertical_id,
        date__date__gte=ly_start,
        date__date__lte=ly_end,
    ).aggregate(
        total_spend=Coalesce(Sum("cost"), _Z, output_field=_DF),
    )
    ly_vert_spend = float(ly_vert_qs["total_spend"])

    # Vertical revenue pacing for recommendations
    vertical_revenue_pace = None
    vertical_mts_pace = None
    if vert_budget:
        mtd_window = DateWindow(month_start, period.analysis.end)
        mtd_orders = orders_by_brand(mtd_window, vertical_id, rev_type="net")
        mtd_media = media_by_brand(mtd_window, vertical_id)
        import calendar
        days_in_month = calendar.monthrange(month_start.year, month_start.month)[1]
        elapsed = (period.analysis.end - month_start).days + 1
        if elapsed > 0:
            expected_elapsed_rev = float(vert_budget.revenue_budget) * (elapsed / days_in_month)
            actual_rev = sum(float(r.get("revenue", 0)) for r in mtd_orders.values())
            actual_spend = sum(float(r.get("spend", 0)) for r in mtd_media.values())
            if expected_elapsed_rev > 0:
                vertical_revenue_pace = actual_rev / expected_elapsed_rev
            if actual_rev > 0:
                actual_mts = actual_spend / actual_rev
                vertical_mts_pace = actual_mts / float(vert_budget.mts_budget) if float(vert_budget.mts_budget) > 0 else None

    # 3. Compute peer group stats (cached by source x campaign_type)
    peer_cache = {}

    def _get_peer(source_id_p, ctype_id):
        key = (source_id_p, ctype_id)
        if key not in peer_cache:
            peer_cache[key] = compute_peer_group_stats(
                vertical_id, source_id_p, ctype_id, period.efficiency,
            )
        return peer_cache[key]

    # 4. Per-campaign scoring
    results = []
    for camp in campaigns:
        agg = analysis_agg.get(camp.id, {})
        camp_spend = float(agg.get("spend", 0))
        camp_clicks = agg.get("clicks", 0)
        camp_conversions = agg.get("conversions", 0)
        camp_conv_value = float(agg.get("conv_value", 0))

        camp_roas = camp_conv_value / camp_spend if camp_spend > 0 else None
        camp_mts = camp_spend / camp_conv_value if camp_conv_value > 0 else None
        camp_cvr = camp_conversions / camp_clicks if camp_clicks > 0 else None

        peer = _get_peer(camp.source_id, camp.campaign_type_id)

        # CVR baseline: use campaign CVR if sufficient clicks, otherwise peer
        used_peer_cvr = False
        if camp_clicks < config.min_click_threshold:
            cvr_baseline = peer["peer_cvr"]
            used_peer_cvr = True
        else:
            cvr_baseline = camp_cvr or 0.0

        # Inclusion filter
        passes, expected_conv = passes_inclusion(camp_clicks, cvr_baseline)
        if not passes:
            continue

        # Component A: Elasticity
        comp_a = compute_elasticity(camp.id, period.elasticity)

        # Component B: Budget Binding
        ly_camp_spend = ly_agg.get(camp.id, 0.0)
        comp_b = compute_budget_binding(
            camp_spend, vert_total_spend, vert_budget_spend,
            ly_camp_spend, ly_vert_spend,
        )

        # Component C: Efficiency Stability
        comp_c = compute_efficiency_stability(
            camp.id, period.efficiency,
            peer["peer_roas_cv"], peer["peer_cvr_cv"],
        )

        # Weighted score
        raw_score = (
            comp_a * config.weight_a
            + comp_b * config.weight_b
            + comp_c * config.weight_c
        ) / 100.0
        scalability_score = max(0, min(100, round(raw_score * 100)))

        # Recommendation
        action, detail, magnitude, reason_codes = recommend_action(
            scalability_score=scalability_score,
            roas=camp_roas,
            source_cvr=camp_cvr,
            efficiency_conversions=expected_conv,
            vertical_revenue_pace=vertical_revenue_pace,
            vertical_mts_pace=vertical_mts_pace,
            comp_a=comp_a,
            comp_b=comp_b,
            comp_c=comp_c,
        )

        results.append(CampaignScore(
            campaign_id=camp.id,
            campaign_name=camp.name,
            ad_group_name=camp.ad_group_name,
            source_name=camp.source.name,
            campaign_type_name=camp.campaign_type.name,
            brand_name=camp.brand.name,
            brand_id=camp.brand.id,
            spend=camp_spend,
            clicks=camp_clicks,
            conversions=camp_conversions,
            conv_value=camp_conv_value,
            roas=camp_roas,
            mts=round(camp_mts, 4) if camp_mts is not None else None,
            source_cvr=camp_cvr,
            scalability_score=scalability_score,
            comp_a=round(comp_a, 3),
            comp_b=round(comp_b, 3),
            comp_c=round(comp_c, 3),
            expected_conversions=round(expected_conv, 2),
            cvr_baseline=round(cvr_baseline, 4),
            used_peer_cvr=used_peer_cvr,
            action=action,
            detail=detail,
            magnitude=magnitude,
            reason_codes=reason_codes,
        ))

    # 5. Exclude campaigns with no action recommended
    results = [r for r in results if r.action != "hold"]

    # 6. Sort by spend descending
    results.sort(key=lambda r: r.spend, reverse=True)
    return results


def check_brand_revenue_at_risk(
    brand_id: int,
    vertical_id: int,
    analysis_window: DateWindow,
) -> Optional[dict]:
    """Per-brand revenue-at-risk check using brand budget."""
    from .services import orders_by_brand, media_by_brand

    month_start = analysis_window.start.replace(day=1)

    # Brand budget for current month
    bb = FactBudget.objects.filter(
        brand_id=brand_id,
        month__date=month_start,
    ).first()
    if not bb or float(bb.revenue_budget) <= 0:
        return None

    # MTD actuals for this brand
    mtd_window = DateWindow(month_start, analysis_window.end)

    media_qs = FactMediaDaily.objects.filter(
        campaign__brand_id=brand_id,
        date__date__gte=mtd_window.start,
        date__date__lte=mtd_window.end,
    ).aggregate(
        total_spend=Coalesce(Sum("cost"), _Z, output_field=_DF),
    )
    total_spend = float(media_qs["total_spend"])

    from .models import FactOrdersDaily
    orders_qs = FactOrdersDaily.objects.filter(
        brand_id=brand_id,
        date__date__gte=mtd_window.start,
        date__date__lte=mtd_window.end,
    ).aggregate(
        total_revenue=Coalesce(Sum("net_revenue"), _Z, output_field=_DF),
    )
    total_revenue = float(orders_qs["total_revenue"])

    import calendar
    elapsed = (analysis_window.end - month_start).days + 1
    days_in_month = calendar.monthrange(month_start.year, month_start.month)[1]
    if elapsed <= 0:
        return None

    projection_factor = days_in_month / elapsed
    projected_revenue = total_revenue * projection_factor
    projected_spend = total_spend * projection_factor
    revenue_goal = float(bb.revenue_budget)
    projected_mts = projected_spend / projected_revenue if projected_revenue > 0 else None
    projected_mts_pct = projected_mts * 100 if projected_mts is not None else None

    if projected_revenue < revenue_goal:
        gap = revenue_goal - projected_revenue
        pct_behind = gap / revenue_goal
        mts_str = f" | Projected MTS: {projected_mts_pct:.1f}%" if projected_mts_pct is not None else ""
        return {
            "message": (
                f"Projected ${projected_revenue:,.0f} vs "
                f"${revenue_goal:,.0f} goal (${gap:,.0f} gap)"
                f"{mts_str}"
            ),
            "projected_revenue": projected_revenue,
            "revenue_goal": revenue_goal,
            "projected_mts": projected_mts,
            "projected_mts_pct": projected_mts_pct,
            "pct_behind": pct_behind,
        }
    return None


def group_by_brand(
    rows: List[CampaignScore],
    vertical_id: int,
    analysis_window: Optional[DateWindow] = None,
) -> List[BrandGroup]:
    """Group campaign scores by brand, sorted by brand total spend desc.

    Campaigns within each brand are also sorted by spend desc (already are).
    """
    brand_map: dict = {}  # brand_id → list of CampaignScore
    for r in rows:
        brand_map.setdefault(r.brand_id, []).append(r)

    groups = []
    for brand_id, campaigns in brand_map.items():
        total_spend = sum(c.spend for c in campaigns)
        rev_risk = None
        if analysis_window:
            rev_risk = check_brand_revenue_at_risk(
                brand_id, vertical_id, analysis_window,
            )
        groups.append(BrandGroup(
            brand_name=campaigns[0].brand_name,
            brand_id=brand_id,
            total_spend=total_spend,
            campaign_count=len(campaigns),
            campaigns=campaigns,
            revenue_at_risk=rev_risk,
        ))

    groups.sort(key=lambda g: g.total_spend, reverse=True)
    return groups
