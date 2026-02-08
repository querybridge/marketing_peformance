"""Tests for CSV ingest hardening: share parsing, header detection, column mapping."""

from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from dashboard.views import _parse_share, _detect_header_row, _build_column_index


# ═══════════════════════════════════════════════════════════════════════════
# _parse_share — floor at 10%, cap at 100%, handle Google Ads quirks
# ═══════════════════════════════════════════════════════════════════════════


class ParseShareTests(TestCase):
    """Share normalization: floor 10%, cap 100%, Google Ads formats."""

    # ── NULL / blank cases ──────────────────────────────────────────────
    def test_none_returns_none(self):
        self.assertIsNone(_parse_share(None))

    def test_empty_string_returns_none(self):
        self.assertIsNone(_parse_share(""))

    def test_double_dash_returns_none(self):
        self.assertIsNone(_parse_share("--"))

    def test_em_dash_returns_none(self):
        self.assertIsNone(_parse_share("\u2014"))

    def test_na_returns_none(self):
        self.assertIsNone(_parse_share("N/A"))
        self.assertIsNone(_parse_share("n/a"))

    def test_whitespace_only_returns_none(self):
        self.assertIsNone(_parse_share("   "))

    # ── Google Ads "< 10%" → floored to 0.10 ───────────────────────────
    def test_less_than_10_pct(self):
        self.assertEqual(_parse_share("< 10%"), Decimal("0.10"))

    def test_less_than_10_pct_no_space(self):
        self.assertEqual(_parse_share("<10%"), Decimal("0.10"))

    # ── Floor at 10% ────────────────────────────────────────────────────
    def test_five_pct_floored(self):
        self.assertEqual(_parse_share("5%"), Decimal("0.10"))

    def test_decimal_0_03_floored(self):
        self.assertEqual(_parse_share("0.03"), Decimal("0.10"))

    def test_eight_point_three_pct_floored(self):
        self.assertEqual(_parse_share("8.3%"), Decimal("0.10"))

    def test_exactly_10_pct(self):
        self.assertEqual(_parse_share("10%"), Decimal("0.10"))

    def test_decimal_0_10_exact(self):
        self.assertEqual(_parse_share("0.10"), Decimal("0.10"))

    # ── Normal values (no floor/cap) ────────────────────────────────────
    def test_45_pct(self):
        self.assertEqual(_parse_share("45%"), Decimal("0.45"))

    def test_decimal_0_45(self):
        self.assertEqual(_parse_share("0.45"), Decimal("0.45"))

    def test_100_pct(self):
        self.assertEqual(_parse_share("100%"), Decimal("1.00"))

    def test_decimal_1_00(self):
        self.assertEqual(_parse_share("1.00"), Decimal("1.00"))

    # ── Cap at 100% ─────────────────────────────────────────────────────
    def test_120_pct_capped(self):
        self.assertEqual(_parse_share("120%"), Decimal("1.00"))

    def test_150_pct_capped(self):
        self.assertEqual(_parse_share("150%"), Decimal("1.00"))

    def test_raw_decimal_gt_1_treated_as_pct_and_floored(self):
        """1.50 without % → treated as 1.5% → 0.015 → floored to 0.10."""
        self.assertEqual(_parse_share("1.50"), Decimal("0.10"))

    # ── "> X%" Google Ads format ────────────────────────────────────────
    def test_greater_than_90_pct(self):
        self.assertEqual(_parse_share("> 90%"), Decimal("0.90"))

    # ── Non-numeric → None ──────────────────────────────────────────────
    def test_garbage_returns_none(self):
        self.assertIsNone(_parse_share("abc"))

    def test_non_string_returns_none(self):
        self.assertIsNone(_parse_share(42))


# ═══════════════════════════════════════════════════════════════════════════
# _detect_header_row — skip Google Ads metadata lines
# ═══════════════════════════════════════════════════════════════════════════


class DetectHeaderRowTests(TestCase):
    """Header auto-detection for Google Ads CSVs with metadata preamble."""

    def test_google_ads_with_two_metadata_rows(self):
        lines = [
            '"Campaign performance"',
            '"January 1, 2025 - February 7, 2026"',
            'Campaign,Campaign ID,Campaign type,Day,Impr.,Clicks,Currency code,Cost,Conversions,Conv. value,Search impr. share,Click share',
            'Brand Search,12345,Search,2026-02-01,100,10,USD,5.00,2,50.00,45%,30%',
        ]
        idx, headers = _detect_header_row(lines, "google-ads")
        self.assertEqual(idx, 2)
        self.assertEqual(headers[0], "Campaign")
        self.assertEqual(headers[3], "Day")

    def test_google_ads_header_on_first_line(self):
        """Standard CSV without metadata — header is row 0."""
        lines = [
            'Campaign,Campaign ID,Campaign type,Day,Impr.,Clicks,Currency code,Cost,Conversions,Conv. value,Search impr. share,Click share',
            'Brand Search,12345,Search,2026-02-01,100,10,USD,5.00,2,50.00,45%,30%',
        ]
        idx, headers = _detect_header_row(lines, "google-ads")
        self.assertEqual(idx, 0)

    def test_google_ads_single_metadata_row(self):
        lines = [
            '"Campaign performance report"',
            'Campaign,Campaign ID,Campaign type,Day,Impr.,Clicks,Currency code,Cost,Conversions,Conv. value,Search impr. share,Click share',
            'Brand Search,12345,Search,2026-02-01,100,10,USD,5.00,2,50.00,45%,30%',
        ]
        idx, headers = _detect_header_row(lines, "google-ads")
        self.assertEqual(idx, 1)

    def test_bing_ads_no_metadata(self):
        lines = [
            'Campaign name,Campaign ID,Campaign type,Time period,Impressions,Clicks,Spend,Conversions,Revenue,Impression share %,Click share %',
            'Brand,999,Search,2026-02-01,200,20,10.00,5,100.00,60%,40%',
        ]
        idx, headers = _detect_header_row(lines, "bing-ads")
        self.assertEqual(idx, 0)

    def test_no_header_found_returns_none(self):
        lines = [
            '"Some garbage"',
            '"More garbage"',
            '"Still garbage"',
        ]
        idx, headers = _detect_header_row(lines, "google-ads")
        self.assertIsNone(idx)
        self.assertIsNone(headers)

    def test_empty_lines_returns_none(self):
        idx, headers = _detect_header_row([], "google-ads")
        self.assertIsNone(idx)

    def test_amazon_feed_no_cost_column(self):
        """Amazon feed mapping lacks 'cost'; detection uses campaign + date."""
        lines = [
            'Campaign name,Campaign ID,Campaign type,Day,Sessions,Orders,Sales',
            'Feed Campaign,111,Feed,2026-02-01,50,5,200.00',
        ]
        idx, headers = _detect_header_row(lines, "amazon-marketplace-feed")
        self.assertEqual(idx, 0)


# ═══════════════════════════════════════════════════════════════════════════
# _build_column_index — mapping verification
# ═══════════════════════════════════════════════════════════════════════════


class BuildColumnIndexTests(TestCase):
    """Column mapping from CSV headers to internal field names."""

    def test_google_ads_full_mapping(self):
        headers = [
            "Campaign", "Campaign ID", "Campaign type", "Day",
            "Impr.", "Clicks", "Currency code", "Cost",
            "Conversions", "Conv. value", "Search impr. share", "Click share",
        ]
        idx = _build_column_index(headers, "google-ads")
        self.assertEqual(idx["campaign"], 0)
        self.assertEqual(idx["external_id"], 1)
        self.assertEqual(idx["campaign_type"], 2)
        self.assertEqual(idx["date"], 3)
        self.assertEqual(idx["impressions"], 4)
        self.assertEqual(idx["clicks"], 5)
        self.assertEqual(idx["cost"], 7)
        self.assertEqual(idx["conversions"], 8)
        self.assertEqual(idx["conversion_value"], 9)
        self.assertEqual(idx["impression_share"], 10)
        self.assertEqual(idx["click_share"], 11)

    def test_google_ads_case_insensitive(self):
        headers = [
            "campaign", "campaign id", "campaign type", "day",
            "impr.", "clicks", "currency code", "cost",
            "conversions", "conv. value", "search impr. share", "click share",
        ]
        idx = _build_column_index(headers, "google-ads")
        self.assertIn("campaign", idx)
        self.assertIn("conversion_value", idx)

    def test_google_ads_missing_optional_columns(self):
        """Shares + campaign type missing — required fields still found."""
        headers = ["Campaign", "Campaign ID", "Day", "Impr.", "Clicks", "Cost", "Conversions", "Conv. value"]
        idx = _build_column_index(headers, "google-ads")
        self.assertIn("campaign", idx)
        self.assertIn("cost", idx)
        self.assertNotIn("impression_share", idx)
        self.assertNotIn("click_share", idx)

    def test_generic_fallback_for_unknown_source(self):
        headers = ["Campaign", "Date", "Cost", "Conversions", "Conversion Value"]
        idx = _build_column_index(headers, "unknown-source")
        self.assertIn("campaign", idx)
        self.assertIn("date", idx)
        self.assertIn("cost", idx)
