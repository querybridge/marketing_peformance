"""
Tests for the ReportLab PDF builder (pdf_report.py).

Covers:
  - build_pdf returns valid PDF bytes
  - compute_totals aggregation
  - Edge cases: empty brand_rows, single brand, no trend data
  - Formatting helpers
"""

from django.test import SimpleTestCase

from dashboard.pdf_report import (
    build_pdf,
    compute_totals,
    _currency,
    _pct,
    _mts,
    _bps,
    _intcomma,
    _delta_color,
)
from reportlab.lib import colors


# ── Sample data fixtures ──────────────────────────────────────────────────

SAMPLE_BRAND_ROW = {
    "id": 1,
    "name": "Acme Corp",
    "vertical": "Retail",
    "spend": 2150.50,
    "spend_cmp": 2100.00,
    "spend_delta": 0.0240,
    "revenue": 10230.25,
    "revenue_cmp": 9800.00,
    "revenue_delta": 0.0439,
    "revenue_budget": 15000.00,
    "revenue_vs_budget": -0.318,
    "mts": 0.2100,
    "mts_cmp": 0.2143,
    "mts_delta": -0.0043,
    "mts_budget": 0.2200,
    "orders": 45,
    "aov": 227.34,
    "clicks": 1200,
    "conversions": 60,
    "net_cvr": 0.0375,
    "mkt_cvr": 0.0500,
    "alerts": ["doing_well"],
}

SECOND_BRAND_ROW = {
    "id": 2,
    "name": "Widget Inc",
    "vertical": "Tech",
    "spend": 3000.00,
    "spend_cmp": 3200.00,
    "spend_delta": -0.0625,
    "revenue": 8500.00,
    "revenue_cmp": 9000.00,
    "revenue_delta": -0.0556,
    "revenue_budget": 10000.00,
    "revenue_vs_budget": -0.15,
    "mts": 0.3529,
    "mts_cmp": 0.3556,
    "mts_delta": -0.0027,
    "mts_budget": 0.3000,
    "orders": 30,
    "aov": 283.33,
    "clicks": 800,
    "conversions": 35,
    "net_cvr": 0.0375,
    "mkt_cvr": 0.0438,
    "alerts": ["pacing_risk"],
}

SAMPLE_EXCEPTIONS = {
    "doing_well": {
        "count": 1,
        "label": "Doing Well",
        "color": "green",
        "icon": "check circle",
        "severity": "Low",
        "tooltip": "Revenue is growing 10%+ YOY...",
        "cta": "No immediate action.",
        "drill_target": "Source",
    },
}

SAMPLE_TREND = {
    "current": [
        {"date": "2026-02-02", "day": "Mon", "spend": 300.0, "revenue": 1500.0},
        {"date": "2026-02-03", "day": "Tue", "spend": 320.0, "revenue": 1600.0},
        {"date": "2026-02-04", "day": "Wed", "spend": 310.0, "revenue": 1550.0},
    ],
    "compare": [
        {"date": "2026-01-26", "day": "Mon", "spend": 280.0, "revenue": 1400.0},
        {"date": "2026-01-27", "day": "Tue", "spend": 290.0, "revenue": 1450.0},
        {"date": "2026-01-28", "day": "Wed", "spend": 300.0, "revenue": 1500.0},
    ],
}

SAMPLE_PERIOD_INFO = {
    "title": "Weekly Performance Dashboard",
    "date_range": "2026-02-02 \u2013 2026-02-08",
    "period_label": "This Week vs Last Week",
    "pacing_note": "Pacing: 5 elapsed day(s)",
}


# ── Formatting helper tests ──────────────────────────────────────────────

class CurrencyFormatTests(SimpleTestCase):

    def test_millions(self):
        self.assertEqual(_currency(1_500_000), "$1.5M")

    def test_thousands(self):
        self.assertEqual(_currency(12_345), "$12.3K")

    def test_small_value(self):
        self.assertEqual(_currency(99.50), "$99.50")

    def test_none(self):
        self.assertEqual(_currency(None), "\u2014")

    def test_zero(self):
        self.assertEqual(_currency(0), "$0.00")


class PctFormatTests(SimpleTestCase):

    def test_positive(self):
        self.assertEqual(_pct(0.123), "+12.3%")

    def test_negative(self):
        self.assertEqual(_pct(-0.045), "-4.5%")

    def test_zero(self):
        self.assertEqual(_pct(0), "0.0%")

    def test_none(self):
        self.assertEqual(_pct(None), "\u2014")


class MtsFormatTests(SimpleTestCase):

    def test_normal(self):
        self.assertEqual(_mts(0.2345), "23.4%")

    def test_none(self):
        self.assertEqual(_mts(None), "\u2014")


class BpsFormatTests(SimpleTestCase):

    def test_positive(self):
        self.assertEqual(_bps(0.005), "+50 bps")

    def test_negative(self):
        self.assertEqual(_bps(-0.003), "-30 bps")

    def test_none(self):
        self.assertEqual(_bps(None), "\u2014")


class IntcommaFormatTests(SimpleTestCase):

    def test_thousands(self):
        self.assertEqual(_intcomma(1234), "1,234")

    def test_none(self):
        self.assertEqual(_intcomma(None), "\u2014")


class DeltaColorTests(SimpleTestCase):

    def test_positive_is_green(self):
        self.assertEqual(_delta_color(0.05).hexval(), colors.HexColor("#21BA45").hexval())

    def test_negative_is_red(self):
        self.assertEqual(_delta_color(-0.05).hexval(), colors.HexColor("#DB2828").hexval())

    def test_flat_is_black(self):
        self.assertEqual(_delta_color(0.0), colors.black)

    def test_inverted_negative_is_green(self):
        """For MTS, lower is better — negative delta should be green."""
        self.assertEqual(_delta_color(-0.05, invert=True).hexval(), colors.HexColor("#21BA45").hexval())

    def test_none(self):
        self.assertEqual(_delta_color(None), colors.black)


# ── compute_totals tests ─────────────────────────────────────────────────

class ComputeTotalsTests(SimpleTestCase):

    def test_empty(self):
        self.assertEqual(compute_totals([]), {})

    def test_single_brand(self):
        totals = compute_totals([SAMPLE_BRAND_ROW])
        self.assertAlmostEqual(totals["spend"], 2150.50)
        self.assertAlmostEqual(totals["revenue"], 10230.25)
        self.assertEqual(totals["orders"], 45)

    def test_multiple_brands(self):
        totals = compute_totals([SAMPLE_BRAND_ROW, SECOND_BRAND_ROW])
        self.assertAlmostEqual(totals["spend"], 5150.50)
        self.assertAlmostEqual(totals["revenue"], 18730.25)
        self.assertEqual(totals["orders"], 75)

    def test_spend_delta(self):
        totals = compute_totals([SAMPLE_BRAND_ROW, SECOND_BRAND_ROW])
        expected = (5150.50 - 5300.00) / 5300.00
        self.assertAlmostEqual(totals["spend_delta"], expected, places=4)

    def test_revenue_delta(self):
        totals = compute_totals([SAMPLE_BRAND_ROW, SECOND_BRAND_ROW])
        expected = (18730.25 - 18800.00) / 18800.00
        self.assertAlmostEqual(totals["revenue_delta"], expected, places=4)

    def test_mts(self):
        totals = compute_totals([SAMPLE_BRAND_ROW, SECOND_BRAND_ROW])
        expected = 5150.50 / 18730.25
        self.assertAlmostEqual(totals["mts"], expected, places=4)

    def test_aov(self):
        totals = compute_totals([SAMPLE_BRAND_ROW, SECOND_BRAND_ROW])
        expected = 18730.25 / 75
        self.assertAlmostEqual(totals["aov"], expected, places=2)

    def test_zero_orders(self):
        row = {**SAMPLE_BRAND_ROW, "orders": 0, "revenue": 0, "revenue_cmp": 0}
        totals = compute_totals([row])
        self.assertIsNone(totals["aov"])

    def test_zero_revenue(self):
        row = {**SAMPLE_BRAND_ROW, "revenue": 0, "spend": 100, "revenue_cmp": 0}
        totals = compute_totals([row])
        self.assertIsNone(totals["mts"])

    def test_net_cvr(self):
        totals = compute_totals([SAMPLE_BRAND_ROW, SECOND_BRAND_ROW])
        # total orders=75, total clicks=2000
        expected = 75 / 2000
        self.assertAlmostEqual(totals["net_cvr"], expected, places=4)

    def test_mkt_cvr(self):
        totals = compute_totals([SAMPLE_BRAND_ROW, SECOND_BRAND_ROW])
        # total conversions=95, total clicks=2000
        expected = 95 / 2000
        self.assertAlmostEqual(totals["mkt_cvr"], expected, places=4)

    def test_zero_clicks(self):
        row = {**SAMPLE_BRAND_ROW, "clicks": 0, "conversions": 0}
        totals = compute_totals([row])
        self.assertIsNone(totals["net_cvr"])
        self.assertIsNone(totals["mkt_cvr"])


# ── build_pdf tests ──────────────────────────────────────────────────────

class BuildPdfTests(SimpleTestCase):

    def test_returns_bytes(self):
        totals = compute_totals([SAMPLE_BRAND_ROW])
        result = build_pdf(
            [SAMPLE_BRAND_ROW], SAMPLE_EXCEPTIONS,
            SAMPLE_TREND, totals, SAMPLE_PERIOD_INFO,
        )
        self.assertIsInstance(result, bytes)

    def test_starts_with_pdf_header(self):
        totals = compute_totals([SAMPLE_BRAND_ROW])
        result = build_pdf(
            [SAMPLE_BRAND_ROW], SAMPLE_EXCEPTIONS,
            SAMPLE_TREND, totals, SAMPLE_PERIOD_INFO,
        )
        self.assertTrue(result.startswith(b"%PDF"))

    def test_nonzero_length(self):
        totals = compute_totals([SAMPLE_BRAND_ROW])
        result = build_pdf(
            [SAMPLE_BRAND_ROW], SAMPLE_EXCEPTIONS,
            SAMPLE_TREND, totals, SAMPLE_PERIOD_INFO,
        )
        self.assertGreater(len(result), 1000)

    def test_multiple_brands(self):
        rows = [SAMPLE_BRAND_ROW, SECOND_BRAND_ROW]
        totals = compute_totals(rows)
        result = build_pdf(
            rows, SAMPLE_EXCEPTIONS,
            SAMPLE_TREND, totals, SAMPLE_PERIOD_INFO,
        )
        self.assertTrue(result.startswith(b"%PDF"))

    def test_empty_brand_rows(self):
        """PDF should still generate with zero brands."""
        result = build_pdf([], {}, SAMPLE_TREND, {}, SAMPLE_PERIOD_INFO)
        self.assertTrue(result.startswith(b"%PDF"))

    def test_no_exceptions(self):
        totals = compute_totals([SAMPLE_BRAND_ROW])
        result = build_pdf(
            [SAMPLE_BRAND_ROW], {},
            SAMPLE_TREND, totals, SAMPLE_PERIOD_INFO,
        )
        self.assertTrue(result.startswith(b"%PDF"))

    def test_no_trend_data(self):
        """PDF should generate without trend charts."""
        totals = compute_totals([SAMPLE_BRAND_ROW])
        result = build_pdf(
            [SAMPLE_BRAND_ROW], SAMPLE_EXCEPTIONS,
            {}, totals, SAMPLE_PERIOD_INFO,
        )
        self.assertTrue(result.startswith(b"%PDF"))

    def test_none_trend_data(self):
        """PDF should handle None trend gracefully."""
        totals = compute_totals([SAMPLE_BRAND_ROW])
        result = build_pdf(
            [SAMPLE_BRAND_ROW], SAMPLE_EXCEPTIONS,
            None, totals, SAMPLE_PERIOD_INFO,
        )
        self.assertTrue(result.startswith(b"%PDF"))

    def test_empty_trend_current(self):
        """PDF should handle trend with empty current list."""
        totals = compute_totals([SAMPLE_BRAND_ROW])
        result = build_pdf(
            [SAMPLE_BRAND_ROW], SAMPLE_EXCEPTIONS,
            {"current": [], "compare": []}, totals, SAMPLE_PERIOD_INFO,
        )
        self.assertTrue(result.startswith(b"%PDF"))

    def test_single_day_trend(self):
        """PDF should handle trend with a single data point."""
        trend = {
            "current": [
                {"date": "2026-02-02", "day": "Mon", "spend": 300, "revenue": 1500},
            ],
            "compare": [],
        }
        totals = compute_totals([SAMPLE_BRAND_ROW])
        result = build_pdf(
            [SAMPLE_BRAND_ROW], SAMPLE_EXCEPTIONS,
            trend, totals, SAMPLE_PERIOD_INFO,
        )
        self.assertTrue(result.startswith(b"%PDF"))

    def test_brand_with_no_alerts(self):
        row = {**SAMPLE_BRAND_ROW, "alerts": []}
        totals = compute_totals([row])
        result = build_pdf(
            [row], {}, SAMPLE_TREND, totals, SAMPLE_PERIOD_INFO,
        )
        self.assertTrue(result.startswith(b"%PDF"))

    def test_brand_with_none_values(self):
        """Brands with None deltas should not crash the builder."""
        row = {
            **SAMPLE_BRAND_ROW,
            "spend_delta": None,
            "revenue_delta": None,
            "revenue_vs_budget": None,
            "mts": None,
            "mts_delta": None,
            "mts_budget": None,
            "aov": None,
        }
        totals = compute_totals([row])
        result = build_pdf(
            [row], SAMPLE_EXCEPTIONS,
            SAMPLE_TREND, totals, SAMPLE_PERIOD_INFO,
        )
        self.assertTrue(result.startswith(b"%PDF"))

    def test_no_totals(self):
        """PDF should generate with empty totals dict."""
        result = build_pdf(
            [SAMPLE_BRAND_ROW], SAMPLE_EXCEPTIONS,
            SAMPLE_TREND, {}, SAMPLE_PERIOD_INFO,
        )
        self.assertTrue(result.startswith(b"%PDF"))

    def test_minimal_period_info(self):
        """PDF should handle minimal period_info."""
        totals = compute_totals([SAMPLE_BRAND_ROW])
        result = build_pdf(
            [SAMPLE_BRAND_ROW], SAMPLE_EXCEPTIONS,
            SAMPLE_TREND, totals, {},
        )
        self.assertTrue(result.startswith(b"%PDF"))

    def test_zero_revenue_in_trend(self):
        """MTS chart should handle zero revenue (division by zero)."""
        trend = {
            "current": [
                {"date": "2026-02-02", "day": "Mon", "spend": 300, "revenue": 0},
                {"date": "2026-02-03", "day": "Tue", "spend": 320, "revenue": 0},
            ],
            "compare": [],
        }
        totals = compute_totals([SAMPLE_BRAND_ROW])
        result = build_pdf(
            [SAMPLE_BRAND_ROW], SAMPLE_EXCEPTIONS,
            trend, totals, SAMPLE_PERIOD_INFO,
        )
        self.assertTrue(result.startswith(b"%PDF"))

    def test_multiple_alert_types(self):
        exceptions = {
            "doing_well": {**SAMPLE_EXCEPTIONS["doing_well"]},
            "pacing_risk": {
                "count": 2, "label": "Pacing Risk", "color": "orange",
                "icon": "warning sign", "severity": "Med",
                "tooltip": "Revenue dropped.", "cta": "Drill in.",
                "drill_target": "Campaign",
            },
            "needs_attention": {
                "count": 1, "label": "Needs Attn", "color": "red",
                "icon": "exclamation triangle", "severity": "High",
                "tooltip": "Revenue collapsed.", "cta": "Check tracking.",
                "drill_target": "Source",
            },
        }
        totals = compute_totals([SAMPLE_BRAND_ROW, SECOND_BRAND_ROW])
        result = build_pdf(
            [SAMPLE_BRAND_ROW, SECOND_BRAND_ROW], exceptions,
            SAMPLE_TREND, totals, SAMPLE_PERIOD_INFO,
        )
        self.assertTrue(result.startswith(b"%PDF"))
        self.assertGreater(len(result), 1000)
