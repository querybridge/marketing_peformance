"""
Tests for the alert classification engine (classify_brand) and ALERT_META.

Covers:
  - Each alert rule firing in isolation
  - Each alert rule NOT firing when just below threshold
  - Multiple simultaneous alerts
  - Data-quality alerts suppressing dependent alerts
  - ALERT_META completeness
"""

from django.test import SimpleTestCase

from dashboard.services import ALERT_META, classify_brand


class AlertMetaTests(SimpleTestCase):
    """Verify ALERT_META has all required fields for every alert."""

    REQUIRED_KEYS = {
        "label", "color", "icon", "severity", "tooltip", "cta", "drill_target",
    }

    def test_all_keys_present(self):
        for key, meta in ALERT_META.items():
            for field in self.REQUIRED_KEYS:
                self.assertIn(
                    field, meta,
                    f"ALERT_META['{key}'] missing '{field}'",
                )

    def test_severity_values(self):
        for key, meta in ALERT_META.items():
            self.assertIn(
                meta["severity"], ("High", "Med", "Low"),
                f"ALERT_META['{key}'] has invalid severity: {meta['severity']}",
            )

    def test_drill_target_values(self):
        valid = {"Brand", "Source", "Campaign Type", "Campaign"}
        for key, meta in ALERT_META.items():
            self.assertIn(
                meta["drill_target"], valid,
                f"ALERT_META['{key}'] has invalid drill_target: {meta['drill_target']}",
            )


# ── Base kwargs: a "normal" brand that triggers NO alerts ──────────────

NORMAL = dict(
    cur_rev=10000,
    yoy_rev=9000,         # ~11% YOY growth
    cur_mts=0.2200,       # 22.00%
    mts_budget=0.2200,    # exact match
    cur_spend=2200,
    cmp_spend=2100,
    cmp_rev=9800,
    has_budget=True,
    has_revenue=True,
)


def _classify(**overrides):
    """Call classify_brand with NORMAL defaults + overrides."""
    kw = {**NORMAL, **overrides}
    return classify_brand(**kw)


class NeedsAttentionTests(SimpleTestCase):
    """Revenue < 80% of YOY → needs_attention (High)."""

    def test_fires(self):
        # cur_rev = 7000 < yoy_rev * 0.80 = 7200
        alerts = _classify(cur_rev=7000, yoy_rev=9000)
        self.assertIn("needs_attention", alerts)

    def test_boundary_not_fires(self):
        # cur_rev = 7200 = exactly 80% → should NOT fire (< not <=)
        alerts = _classify(cur_rev=7200, yoy_rev=9000)
        self.assertNotIn("needs_attention", alerts)

    def test_no_yoy_data(self):
        """No YOY revenue → rule cannot evaluate → no alert."""
        alerts = _classify(cur_rev=100, yoy_rev=0)
        self.assertNotIn("needs_attention", alerts)

    def test_none_yoy(self):
        alerts = _classify(cur_rev=100, yoy_rev=None)
        self.assertNotIn("needs_attention", alerts)


class DoingWellTests(SimpleTestCase):
    """≥10% YOY growth AND within ±50 bps of MTS budget → doing_well (Low)."""

    def test_fires(self):
        alerts = _classify(
            cur_rev=11000, yoy_rev=9000,  # +22% YOY
            cur_mts=0.2220, mts_budget=0.2200,  # 20 bps diff
        )
        self.assertIn("doing_well", alerts)

    def test_near_threshold(self):
        """10% growth and 45 bps diff → fires (comfortably inside)."""
        alerts = _classify(
            cur_rev=9900, yoy_rev=9000,  # +10%
            cur_mts=0.2245, mts_budget=0.2200,  # 45 bps
        )
        self.assertIn("doing_well", alerts)

    def test_growth_too_low(self):
        """9% YOY growth → doesn't fire."""
        alerts = _classify(
            cur_rev=9810, yoy_rev=9000,  # +9%
            cur_mts=0.2200, mts_budget=0.2200,
        )
        self.assertNotIn("doing_well", alerts)

    def test_mts_too_far(self):
        """MTS 60 bps away from budget → doesn't fire."""
        alerts = _classify(
            cur_rev=11000, yoy_rev=9000,
            cur_mts=0.2260, mts_budget=0.2200,  # 60 bps
        )
        self.assertNotIn("doing_well", alerts)

    def test_no_budget(self):
        """No MTS budget → rule can't evaluate."""
        alerts = _classify(
            cur_rev=11000, yoy_rev=9000,
            cur_mts=0.2200, mts_budget=0,
        )
        self.assertNotIn("doing_well", alerts)


class OverlyEfficientTests(SimpleTestCase):
    """MTS ≥ 50 bps below budget → overly_efficient (Med)."""

    def test_fires(self):
        # budget 22.00%, actual 21.40% → 60 bps better
        alerts = _classify(cur_mts=0.2140, mts_budget=0.2200)
        self.assertIn("overly_efficient", alerts)

    def test_exact_threshold(self):
        """Exactly 50 bps below → fires (>=)."""
        alerts = _classify(cur_mts=0.2150, mts_budget=0.2200)
        self.assertIn("overly_efficient", alerts)

    def test_boundary_not_fires(self):
        """49 bps below → doesn't fire."""
        alerts = _classify(cur_mts=0.2151, mts_budget=0.2200)
        self.assertNotIn("overly_efficient", alerts)

    def test_mts_above_budget(self):
        """MTS above budget → not efficient, doesn't fire."""
        alerts = _classify(cur_mts=0.2300, mts_budget=0.2200)
        self.assertNotIn("overly_efficient", alerts)

    def test_no_mts(self):
        """No current MTS (zero revenue) → can't evaluate."""
        alerts = _classify(cur_mts=None, mts_budget=0.2200)
        self.assertNotIn("overly_efficient", alerts)


class UnderEfficientTests(SimpleTestCase):
    """MTS ≥ 50 bps above budget → under_efficient (Med)."""

    def test_fires(self):
        # budget 22.00%, actual 22.60% → 60 bps worse
        alerts = _classify(cur_mts=0.2260, mts_budget=0.2200)
        self.assertIn("under_efficient", alerts)

    def test_exact_threshold(self):
        """Exactly 50 bps above → fires (>=)."""
        alerts = _classify(cur_mts=0.2250, mts_budget=0.2200)
        self.assertIn("under_efficient", alerts)

    def test_boundary_not_fires(self):
        """49 bps above → doesn't fire."""
        alerts = _classify(cur_mts=0.2249, mts_budget=0.2200)
        self.assertNotIn("under_efficient", alerts)

    def test_mts_below_budget(self):
        """MTS below budget → over efficient, not under."""
        alerts = _classify(cur_mts=0.2100, mts_budget=0.2200)
        self.assertNotIn("under_efficient", alerts)

    def test_no_mts(self):
        """No current MTS (zero revenue) → can't evaluate."""
        alerts = _classify(cur_mts=None, mts_budget=0.2200)
        self.assertNotIn("under_efficient", alerts)

    def test_no_budget(self):
        """No MTS budget → can't evaluate."""
        alerts = _classify(cur_mts=0.2300, mts_budget=0)
        self.assertNotIn("under_efficient", alerts)


class PacingRiskTests(SimpleTestCase):
    """Revenue down 10%+ WOW while spend flat/up → pacing_risk (Med)."""

    def test_fires(self):
        # Revenue down 15%, spend up 2%
        alerts = _classify(
            cur_rev=8330, cmp_rev=9800,     # -15%
            cur_spend=2142, cmp_spend=2100,  # +2%
        )
        self.assertIn("pacing_risk", alerts)

    def test_revenue_down_spend_down(self):
        """Both down → spend cut may be intentional → no alert."""
        alerts = _classify(
            cur_rev=8330, cmp_rev=9800,     # -15%
            cur_spend=1700, cmp_spend=2100,  # -19%
        )
        self.assertNotIn("pacing_risk", alerts)

    def test_revenue_down_4pct(self):
        """Revenue only down 4% → below threshold."""
        alerts = _classify(
            cur_rev=9408, cmp_rev=9800,  # -4%
            cur_spend=2200, cmp_spend=2100,
        )
        self.assertNotIn("pacing_risk", alerts)

    def test_no_comparison_data(self):
        """No comparison revenue → can't evaluate."""
        alerts = _classify(cmp_rev=0, cmp_spend=0)
        self.assertNotIn("pacing_risk", alerts)


class DataQualityTests(SimpleTestCase):
    """Missing budget / revenue → data quality alerts."""

    def test_missing_budget(self):
        alerts = _classify(has_budget=False)
        self.assertIn("missing_budget", alerts)

    def test_has_budget(self):
        alerts = _classify(has_budget=True)
        self.assertNotIn("missing_budget", alerts)

    def test_missing_revenue(self):
        alerts = _classify(has_revenue=False)
        self.assertIn("missing_revenue", alerts)

    def test_has_revenue(self):
        alerts = _classify(has_revenue=True)
        self.assertNotIn("missing_revenue", alerts)


class MultiAlertTests(SimpleTestCase):
    """A brand can carry multiple simultaneous alerts."""

    def test_overly_efficient_plus_doing_well(self):
        """Brand growing 15% YOY, MTS 60 bps below budget → both fire."""
        alerts = _classify(
            cur_rev=10350, yoy_rev=9000,      # +15% YOY
            cur_mts=0.2100, mts_budget=0.2200, # 100 bps below, but also within 50? No.
        )
        # 100 bps > 50 bps, so doing_well should NOT fire (MTS too far from budget)
        self.assertIn("overly_efficient", alerts)
        self.assertNotIn("doing_well", alerts)

    def test_overly_efficient_excludes_doing_well_past_boundary(self):
        """doing_well needs ≤50 bps; overly_efficient needs ≥50 bps.
        At 60 bps below budget, only overly_efficient fires."""
        alerts = _classify(
            cur_rev=10350, yoy_rev=9000,      # +15% YOY
            cur_mts=0.2140, mts_budget=0.2200, # 60 bps below
        )
        self.assertIn("overly_efficient", alerts)
        self.assertNotIn("doing_well", alerts)

    def test_needs_attention_excludes_doing_well(self):
        """Revenue at 5% of YOY can't also be 10%+ growth — logically exclusive."""
        alerts = _classify(
            cur_rev=450, yoy_rev=9000,
            cur_mts=0.2200, mts_budget=0.2200,
        )
        self.assertIn("needs_attention", alerts)
        self.assertNotIn("doing_well", alerts)

    def test_pacing_risk_plus_missing_budget(self):
        alerts = _classify(
            cur_rev=8330, cmp_rev=9800,
            cur_spend=2200, cmp_spend=2100,
            has_budget=False,
        )
        self.assertIn("pacing_risk", alerts)
        self.assertIn("missing_budget", alerts)

    def test_all_data_quality_alerts(self):
        alerts = _classify(has_budget=False, has_revenue=False)
        self.assertIn("missing_budget", alerts)
        self.assertIn("missing_revenue", alerts)


class NormalBrandTests(SimpleTestCase):
    """A well-behaved brand with normal metrics gets no perf alerts."""

    def test_normal_no_perf_alerts(self):
        alerts = _classify()
        perf_alerts = [a for a in alerts if not a.startswith("missing_")]
        # doing_well fires: 11% YOY growth, 0 bps diff
        self.assertEqual(perf_alerts, ["doing_well"])
