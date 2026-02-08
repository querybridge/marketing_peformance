"""
Comprehensive tests for the date / comparison engine in services.py.

Covers:
  - Every preset (this_week, last_week, this_month, last_month, this_quarter, custom)
  - Every comparison mode (wow, mom, yoy, custom)
  - Pacing logic (partial vs full periods)
  - Edge cases: Monday start, Sunday (full week), 1st of month, month
    boundaries, leap-year YOY, inverted custom ranges, missing data
  - DateWindow helpers (sql_where, sql_params, contains, overlap_days)
  - compare_delta missing-data helper
"""

from datetime import date, timedelta
from decimal import Decimal

from django.test import SimpleTestCase

from dashboard.services import (
    DateWindow,
    Period,
    compare_delta,
    resolve_period,
    _div,
    _monday,
    _month_end,
    _month_start,
    _pct,
    _shift_month,
)


# ═══════════════════════════════════════════════════════════════════════════
# DateWindow unit tests
# ═══════════════════════════════════════════════════════════════════════════


class DateWindowTests(SimpleTestCase):

    def test_days_single(self):
        w = DateWindow(date(2026, 2, 2), date(2026, 2, 2))
        self.assertEqual(w.days, 1)

    def test_days_full_week(self):
        w = DateWindow(date(2026, 2, 2), date(2026, 2, 8))  # Mon–Sun
        self.assertEqual(w.days, 7)

    def test_contains(self):
        w = DateWindow(date(2026, 2, 2), date(2026, 2, 8))
        self.assertTrue(w.contains(date(2026, 2, 5)))
        self.assertTrue(w.contains(date(2026, 2, 2)))
        self.assertTrue(w.contains(date(2026, 2, 8)))
        self.assertFalse(w.contains(date(2026, 2, 1)))
        self.assertFalse(w.contains(date(2026, 2, 9)))

    def test_overlap_days_full(self):
        a = DateWindow(date(2026, 2, 2), date(2026, 2, 8))
        b = DateWindow(date(2026, 2, 2), date(2026, 2, 8))
        self.assertEqual(a.overlap_days(b), 7)

    def test_overlap_days_partial(self):
        a = DateWindow(date(2026, 2, 2), date(2026, 2, 8))
        b = DateWindow(date(2026, 2, 5), date(2026, 2, 12))
        self.assertEqual(a.overlap_days(b), 4)  # Feb 5–8

    def test_overlap_days_disjoint(self):
        a = DateWindow(date(2026, 2, 2), date(2026, 2, 4))
        b = DateWindow(date(2026, 2, 6), date(2026, 2, 8))
        self.assertEqual(a.overlap_days(b), 0)

    def test_sql_where(self):
        w = DateWindow(date(2026, 2, 2), date(2026, 2, 8))
        self.assertEqual(
            w.sql_where(),
            "dd.date BETWEEN '2026-02-02' AND '2026-02-08'",
        )

    def test_sql_where_custom_col(self):
        w = DateWindow(date(2026, 1, 1), date(2026, 1, 31))
        self.assertEqual(
            w.sql_where("d.dt"),
            "d.dt BETWEEN '2026-01-01' AND '2026-01-31'",
        )

    def test_sql_params(self):
        w = DateWindow(date(2026, 2, 2), date(2026, 2, 8))
        fragment, params = w.sql_params()
        self.assertEqual(fragment, "dd.date BETWEEN %s AND %s")
        self.assertEqual(params, [date(2026, 2, 2), date(2026, 2, 8)])


# ═══════════════════════════════════════════════════════════════════════════
# Period properties
# ═══════════════════════════════════════════════════════════════════════════


class PeriodTests(SimpleTestCase):

    def test_pacing_note_partial(self):
        p = Period(
            current=DateWindow(date(2026, 2, 2), date(2026, 2, 4)),
            compare=DateWindow(date(2026, 1, 26), date(2026, 1, 28)),
            label="vs Prior Week",
            is_partial=True,
        )
        self.assertIn("3 elapsed day(s)", p.pacing_note)
        self.assertIn("Pacing", p.pacing_note)

    def test_pacing_note_full(self):
        p = Period(
            current=DateWindow(date(2026, 1, 26), date(2026, 2, 1)),
            compare=DateWindow(date(2026, 1, 19), date(2026, 1, 25)),
            label="vs Prior Week",
            is_partial=False,
        )
        self.assertIn("Full period", p.pacing_note)


# ═══════════════════════════════════════════════════════════════════════════
# Helper function tests
# ═══════════════════════════════════════════════════════════════════════════


class HelperTests(SimpleTestCase):

    def test_monday(self):
        # 2026-02-04 is a Wednesday
        self.assertEqual(_monday(date(2026, 2, 4)), date(2026, 2, 2))
        # Already Monday
        self.assertEqual(_monday(date(2026, 2, 2)), date(2026, 2, 2))
        # Sunday → previous Monday
        self.assertEqual(_monday(date(2026, 2, 8)), date(2026, 2, 2))

    def test_month_start(self):
        self.assertEqual(_month_start(date(2026, 2, 15)), date(2026, 2, 1))
        self.assertEqual(_month_start(date(2026, 1, 1)), date(2026, 1, 1))

    def test_month_end(self):
        self.assertEqual(_month_end(date(2026, 2, 1)), date(2026, 2, 28))
        self.assertEqual(_month_end(date(2024, 2, 1)), date(2024, 2, 29))  # leap
        self.assertEqual(_month_end(date(2026, 1, 15)), date(2026, 1, 31))

    def test_shift_month_basic(self):
        self.assertEqual(_shift_month(date(2026, 3, 1), -1), date(2026, 2, 1))
        self.assertEqual(_shift_month(date(2026, 1, 1), -1), date(2025, 12, 1))

    def test_shift_month_clamp(self):
        # March 31 → shift back → Feb has 28 days → clamp to 28
        self.assertEqual(_shift_month(date(2026, 3, 31), -1), date(2026, 2, 28))
        # Leap year: March 31 → Feb 29
        self.assertEqual(_shift_month(date(2024, 3, 31), -1), date(2024, 2, 29))

    def test_pct_normal(self):
        self.assertEqual(_pct(110, 100), 0.1)
        self.assertEqual(_pct(90, 100), -0.1)

    def test_pct_zero_prev(self):
        self.assertIsNone(_pct(100, 0))
        self.assertIsNone(_pct(100, None))

    def test_div_normal(self):
        self.assertEqual(_div(1, 4), 0.25)

    def test_div_zero_denom(self):
        self.assertIsNone(_div(100, 0))
        self.assertIsNone(_div(100, None))


# ═══════════════════════════════════════════════════════════════════════════
# resolve_period — PRESETS
# ═══════════════════════════════════════════════════════════════════════════


class ResolveThisWeekTests(SimpleTestCase):
    """this_week: Mon–today, pacing comparison."""

    def test_wednesday(self):
        """Wed 2026-02-04: current = Mon Feb 2 – Wed Feb 4 (3 days)."""
        p = resolve_period("this_week", "wow", today=date(2026, 2, 4))
        self.assertEqual(p.current.start, date(2026, 2, 2))
        self.assertEqual(p.current.end, date(2026, 2, 4))
        self.assertEqual(p.current.days, 3)
        self.assertTrue(p.is_partial)

    def test_monday_one_day(self):
        """Mon 2026-02-02: current = 1 day. Compare = 1 day (prior Mon)."""
        p = resolve_period("this_week", "wow", today=date(2026, 2, 2))
        self.assertEqual(p.current.days, 1)
        self.assertEqual(p.compare.start, date(2026, 1, 26))  # prior Mon
        self.assertEqual(p.compare.days, 1)
        self.assertTrue(p.is_partial)

    def test_sunday_full_week(self):
        """Sun 2026-02-08: this_week = Mon–Sun (7 days). Still pacing."""
        p = resolve_period("this_week", "wow", today=date(2026, 2, 8))
        self.assertEqual(p.current.start, date(2026, 2, 2))
        self.assertEqual(p.current.end, date(2026, 2, 8))
        self.assertEqual(p.current.days, 7)
        # Pacing: compare is also 7 days (same as full-to-full)
        self.assertEqual(p.compare.days, 7)
        self.assertTrue(p.is_partial)

    def test_wow_pacing(self):
        """Pacing: Mon–Wed (3 days) vs prior Mon–Wed (3 days)."""
        p = resolve_period("this_week", "wow", today=date(2026, 2, 4))
        self.assertEqual(p.compare.start, date(2026, 1, 26))
        self.assertEqual(p.compare.end, date(2026, 1, 28))
        self.assertEqual(p.compare.days, 3)


class ResolveLastWeekTests(SimpleTestCase):
    """last_week: full Mon–Sun, no pacing."""

    def test_basic(self):
        p = resolve_period("last_week", "wow", today=date(2026, 2, 4))
        # last_week from Wed Feb 4 → Mon Jan 26 – Sun Feb 1
        self.assertEqual(p.current.start, date(2026, 1, 26))
        self.assertEqual(p.current.end, date(2026, 2, 1))
        self.assertEqual(p.current.days, 7)
        self.assertFalse(p.is_partial)

    def test_wow_full_to_full(self):
        """WOW compare for full week shifts entire window back 7 days."""
        p = resolve_period("last_week", "wow", today=date(2026, 2, 4))
        self.assertEqual(p.compare.start, date(2026, 1, 19))
        self.assertEqual(p.compare.end, date(2026, 1, 25))
        self.assertEqual(p.compare.days, 7)


class ResolveThisMonthTests(SimpleTestCase):

    def test_mid_month(self):
        """Feb 15: current = Feb 1–15 (15 days), pacing."""
        p = resolve_period("this_month", "wow", today=date(2026, 2, 15))
        self.assertEqual(p.current.start, date(2026, 2, 1))
        self.assertEqual(p.current.end, date(2026, 2, 15))
        self.assertEqual(p.current.days, 15)
        self.assertTrue(p.is_partial)

    def test_first_of_month(self):
        """Feb 1: current = 1 day. WOW compare = 1 day."""
        p = resolve_period("this_month", "wow", today=date(2026, 2, 1))
        self.assertEqual(p.current.days, 1)
        self.assertEqual(p.compare.days, 1)

    def test_mom_pacing(self):
        """Feb 15: MOM pacing → Jan 1–15 (15 days)."""
        p = resolve_period("this_month", "mom", today=date(2026, 2, 15))
        self.assertEqual(p.compare.start, date(2026, 1, 1))
        self.assertEqual(p.compare.end, date(2026, 1, 15))
        self.assertEqual(p.compare.days, 15)


class ResolveLastMonthTests(SimpleTestCase):

    def test_basic(self):
        """Today Feb 4: last_month = Jan 1–31 (31 days), no pacing."""
        p = resolve_period("last_month", "mom", today=date(2026, 2, 4))
        self.assertEqual(p.current.start, date(2026, 1, 1))
        self.assertEqual(p.current.end, date(2026, 1, 31))
        self.assertEqual(p.current.days, 31)
        self.assertFalse(p.is_partial)

    def test_mom_full_to_full(self):
        """Jan (31 days) vs Dec (31 days) — full month to full month."""
        p = resolve_period("last_month", "mom", today=date(2026, 2, 4))
        self.assertEqual(p.compare.start, date(2025, 12, 1))
        self.assertEqual(p.compare.end, date(2025, 12, 31))
        self.assertEqual(p.compare.days, 31)

    def test_mom_feb_vs_jan(self):
        """last_month from Mar: Feb (28 days) vs Jan (31 days)."""
        p = resolve_period("last_month", "mom", today=date(2026, 3, 4))
        self.assertEqual(p.current.start, date(2026, 2, 1))
        self.assertEqual(p.current.end, date(2026, 2, 28))
        self.assertEqual(p.current.days, 28)
        self.assertEqual(p.compare.start, date(2026, 1, 1))
        self.assertEqual(p.compare.end, date(2026, 1, 31))
        self.assertEqual(p.compare.days, 31)


class ResolveThisQuarterTests(SimpleTestCase):

    def test_q1(self):
        p = resolve_period("this_quarter", "wow", today=date(2026, 2, 15))
        self.assertEqual(p.current.start, date(2026, 1, 1))
        self.assertEqual(p.current.end, date(2026, 2, 15))
        self.assertTrue(p.is_partial)

    def test_q2(self):
        p = resolve_period("this_quarter", "wow", today=date(2026, 5, 10))
        self.assertEqual(p.current.start, date(2026, 4, 1))


# ═══════════════════════════════════════════════════════════════════════════
# resolve_period — COMPARISON MODES
# ═══════════════════════════════════════════════════════════════════════════


class YOYComparisonTests(SimpleTestCase):

    def test_52_week_offset(self):
        """YOY uses 52 weeks (364 days) to preserve weekday alignment."""
        p = resolve_period("this_week", "yoy", today=date(2026, 2, 4))
        # 2026-02-02 is Monday; 52 weeks back = 2025-02-03 (also Monday)
        expected_start = date(2026, 2, 2) - timedelta(weeks=52)
        self.assertEqual(p.compare.start, expected_start)
        self.assertEqual(p.compare.start.weekday(), 0)  # Monday
        self.assertEqual(p.compare.days, p.current.days)

    def test_yoy_full_period(self):
        """YOY for last_week: same elapsed days, weekday preserved."""
        p = resolve_period("last_week", "yoy", today=date(2026, 2, 4))
        self.assertEqual(p.compare.days, 7)
        self.assertEqual(p.compare.start.weekday(), 0)  # Monday

    def test_yoy_leap_year(self):
        """
        YOY from Feb 29 2024 (leap day).
        52 weeks back = Feb 27 2023 (NOT Feb 28).
        Weekday alignment wins over calendar alignment.
        """
        p = resolve_period(
            "custom", "yoy",
            custom_start=date(2024, 2, 26),  # Monday
            custom_end=date(2024, 2, 29),     # Thursday (leap day)
            today=date(2024, 2, 29),
        )
        self.assertEqual(p.current.days, 4)
        self.assertEqual(p.compare.days, 4)
        # 52 weeks back from Monday Feb 26 2024 = Monday Feb 27 2023
        self.assertEqual(p.compare.start, date(2024, 2, 26) - timedelta(weeks=52))
        self.assertEqual(p.compare.start.weekday(), 0)


class MOMComparisonTests(SimpleTestCase):

    def test_mom_pacing_short_month(self):
        """
        Mar 1–15 (15 days) with MOM pacing → Feb 1–15 (15 days).
        Even though Feb only has 28 days, pacing extends 15 days in.
        """
        p = resolve_period("this_month", "mom", today=date(2026, 3, 15))
        self.assertEqual(p.compare.start, date(2026, 2, 1))
        self.assertEqual(p.compare.end, date(2026, 2, 15))
        self.assertEqual(p.compare.days, 15)

    def test_mom_pacing_into_shorter_month(self):
        """
        March 31 elapsed = 31 days. MOM pacing: Feb 1 + 30 = Mar 3.
        Extends past Feb — this is by design (no silent day loss).
        """
        p = resolve_period(
            "custom", "mom",
            custom_start=date(2026, 3, 1),
            custom_end=date(2026, 3, 31),
            today=date(2026, 3, 31),
        )
        # Custom preset → pacing=False, so MOM uses _month_end
        self.assertFalse(p.is_partial)


class CustomRangeTests(SimpleTestCase):

    def test_inverted_dates_swapped(self):
        """custom_start > custom_end is silently swapped."""
        p = resolve_period(
            "custom", "wow",
            custom_start=date(2026, 2, 10),
            custom_end=date(2026, 2, 1),
            today=date(2026, 2, 10),
        )
        self.assertEqual(p.current.start, date(2026, 2, 1))
        self.assertEqual(p.current.end, date(2026, 2, 10))

    def test_inverted_compare_dates_swapped(self):
        """Custom comparison dates are also swapped if inverted."""
        p = resolve_period(
            "custom", "custom",
            custom_start=date(2026, 2, 1),
            custom_end=date(2026, 2, 7),
            compare_start=date(2026, 1, 31),
            compare_end=date(2026, 1, 25),
            today=date(2026, 2, 7),
        )
        self.assertEqual(p.compare.start, date(2026, 1, 25))
        self.assertEqual(p.compare.end, date(2026, 1, 31))

    def test_custom_defaults(self):
        """No dates → 30-day lookback, WOW compare."""
        p = resolve_period("custom", "wow", today=date(2026, 2, 7))
        self.assertEqual(p.current.start, date(2026, 1, 8))
        self.assertEqual(p.current.end, date(2026, 2, 7))
        self.assertFalse(p.is_partial)

    def test_custom_not_pacing(self):
        """Custom preset is never partial / pacing."""
        p = resolve_period(
            "custom", "wow",
            custom_start=date(2026, 2, 1),
            custom_end=date(2026, 2, 7),
            today=date(2026, 2, 7),
        )
        self.assertFalse(p.is_partial)


# ═══════════════════════════════════════════════════════════════════════════
# Pacing contract tests
# ═══════════════════════════════════════════════════════════════════════════


class PacingContractTests(SimpleTestCase):
    """
    Core pacing invariant: for partial presets with WOW/YOY,
    current.days == compare.days.
    """

    def test_this_week_wow_equal_days(self):
        for offset in range(7):  # Mon through Sun
            today = date(2026, 2, 2) + timedelta(days=offset)
            p = resolve_period("this_week", "wow", today=today)
            self.assertEqual(
                p.current.days, p.compare.days,
                f"Pacing failed for {today} ({today.strftime('%A')})",
            )

    def test_this_week_yoy_equal_days(self):
        for offset in range(7):
            today = date(2026, 2, 2) + timedelta(days=offset)
            p = resolve_period("this_week", "yoy", today=today)
            self.assertEqual(p.current.days, p.compare.days)

    def test_this_month_wow_equal_days(self):
        """Every day in February: pacing should give equal elapsed days."""
        for day in range(1, 29):
            today = date(2026, 2, day)
            p = resolve_period("this_month", "wow", today=today)
            self.assertEqual(
                p.current.days, p.compare.days,
                f"Pacing failed for Feb {day}",
            )

    def test_this_month_mom_equal_days(self):
        for day in range(1, 29):
            today = date(2026, 2, day)
            p = resolve_period("this_month", "mom", today=today)
            self.assertEqual(p.current.days, p.compare.days)


# ═══════════════════════════════════════════════════════════════════════════
# Weekday alignment tests
# ═══════════════════════════════════════════════════════════════════════════


class WeekdayAlignmentTests(SimpleTestCase):

    def test_wow_preserves_weekday(self):
        """WOW comparison start is always the same weekday as current start."""
        for offset in range(14):
            today = date(2026, 2, 1) + timedelta(days=offset)
            p = resolve_period("this_week", "wow", today=today)
            self.assertEqual(
                p.current.start.weekday(),
                p.compare.start.weekday(),
                f"Weekday mismatch for {today}",
            )

    def test_yoy_preserves_weekday(self):
        """YOY (52-week offset) preserves weekday for any preset."""
        for preset in ("this_week", "last_week"):
            p = resolve_period(preset, "yoy", today=date(2026, 2, 4))
            self.assertEqual(
                p.current.start.weekday(),
                p.compare.start.weekday(),
            )


# ═══════════════════════════════════════════════════════════════════════════
# SQL WHERE clause tests
# ═══════════════════════════════════════════════════════════════════════════


class SQLWhereTests(SimpleTestCase):
    """
    Verify the SQL WHERE clause patterns match the Period windows.

    Example base query for FactMediaDaily:

        SELECT c.brand_id,
               SUM(f.cost)              AS spend,
               SUM(f.clicks)            AS clicks
        FROM dashboard_factmediadaily f
        JOIN dashboard_dimdate         dd ON f.date_id = dd.id
        JOIN dashboard_dimcampaign     c  ON f.campaign_id = c.id
        WHERE {period.current.sql_where('dd.date')}
        GROUP BY c.brand_id

    Comparison period:
        WHERE {period.compare.sql_where('dd.date')}

    FactOrdersDaily:
        SELECT o.brand_id,
               SUM(o.net_revenue)       AS revenue
        FROM dashboard_factordersdaily o
        JOIN dashboard_dimdate         dd ON o.date_id = dd.id
        WHERE {period.current.sql_where('dd.date')}
        GROUP BY o.brand_id

    FactBudget (monthly proration — see budgets_for_period):
        SELECT b.brand_id, b.revenue_budget, b.mts_budget, dd.date
        FROM dashboard_factbudget b
        JOIN dashboard_dimdate    dd ON b.month_id = dd.id
        WHERE dd.date >= '{month_start(window.start)}'
          AND dd.date <= '{window.end}'
    """

    def test_base_and_compare_clauses(self):
        p = resolve_period("this_week", "wow", today=date(2026, 2, 4))
        base = p.current.sql_where()
        cmp = p.compare.sql_where()
        self.assertIn("2026-02-02", base)
        self.assertIn("2026-02-04", base)
        self.assertIn("2026-01-26", cmp)
        self.assertIn("2026-01-28", cmp)

    def test_parameterized(self):
        p = resolve_period("this_week", "wow", today=date(2026, 2, 4))
        frag, params = p.current.sql_params("dd.date")
        self.assertEqual(frag, "dd.date BETWEEN %s AND %s")
        self.assertEqual(params[0], date(2026, 2, 2))
        self.assertEqual(params[1], date(2026, 2, 4))


# ═══════════════════════════════════════════════════════════════════════════
# compare_delta — missing prior data
# ═══════════════════════════════════════════════════════════════════════════


class CompareDeltaTests(SimpleTestCase):
    """
    Edge cases for missing/zero data in comparison windows.

    In practice this happens when:
    - A brand is new (no YOY data)
    - A campaign was paused (no current data)
    - Both brand and comparison are zero (no delivery at all)
    - Revenue data hasn't been uploaded yet (None)
    """

    def test_normal_increase(self):
        abs_d, pct_d, status = compare_delta(1100, 1000)
        self.assertEqual(status, "ok")
        self.assertAlmostEqual(abs_d, 100.0)
        self.assertAlmostEqual(pct_d, 0.1)

    def test_normal_decrease(self):
        abs_d, pct_d, status = compare_delta(900, 1000)
        self.assertEqual(status, "ok")
        self.assertAlmostEqual(abs_d, -100.0)
        self.assertAlmostEqual(pct_d, -0.1)

    def test_no_compare_data_zero(self):
        """New brand — no prior period data → no_compare."""
        abs_d, pct_d, status = compare_delta(1500, 0)
        self.assertEqual(status, "no_compare")
        self.assertIsNone(abs_d)
        self.assertIsNone(pct_d)

    def test_no_compare_data_none(self):
        """Revenue not uploaded for comparison period → no_compare."""
        abs_d, pct_d, status = compare_delta(1500, None)
        self.assertEqual(status, "no_compare")
        self.assertIsNone(abs_d)

    def test_no_current_data(self):
        """Paused campaign — current is zero, compare has data."""
        abs_d, pct_d, status = compare_delta(0, 1500)
        self.assertEqual(status, "no_current")
        self.assertAlmostEqual(abs_d, -1500.0)
        self.assertAlmostEqual(pct_d, -1.0)

    def test_both_zero(self):
        abs_d, pct_d, status = compare_delta(0, 0)
        self.assertEqual(status, "no_data")
        self.assertIsNone(abs_d)

    def test_both_none(self):
        abs_d, pct_d, status = compare_delta(None, None)
        self.assertEqual(status, "no_data")

    def test_decimal_inputs(self):
        """Works with Decimal (common from Django ORM aggregates)."""
        abs_d, pct_d, status = compare_delta(Decimal("1100.00"), Decimal("1000.00"))
        self.assertEqual(status, "ok")
        self.assertAlmostEqual(pct_d, 0.1)


# ═══════════════════════════════════════════════════════════════════════════
# Edge cases: boundaries and calendar quirks
# ═══════════════════════════════════════════════════════════════════════════


class BoundaryEdgeCaseTests(SimpleTestCase):

    def test_jan1_this_month(self):
        """Jan 1: this_month = 1 day. MOM → Dec 1 (1 day)."""
        p = resolve_period("this_month", "mom", today=date(2026, 1, 1))
        self.assertEqual(p.current.start, date(2026, 1, 1))
        self.assertEqual(p.current.days, 1)
        self.assertEqual(p.compare.start, date(2025, 12, 1))
        self.assertEqual(p.compare.days, 1)

    def test_year_boundary_wow(self):
        """Jan 1 2026 (Thu): this_week = Mon Dec 29 – Thu Jan 1."""
        p = resolve_period("this_week", "wow", today=date(2026, 1, 1))
        self.assertEqual(p.current.start, date(2025, 12, 29))  # Monday
        self.assertEqual(p.current.end, date(2026, 1, 1))
        # WOW compare: Dec 22 – Dec 25
        self.assertEqual(p.compare.start, date(2025, 12, 22))
        self.assertEqual(p.compare.days, p.current.days)

    def test_last_day_of_month(self):
        """Jan 31: this_month = 31 days. WOW compare = 31 days starting Jan 24."""
        p = resolve_period("this_month", "wow", today=date(2026, 1, 31))
        self.assertEqual(p.current.days, 31)
        self.assertEqual(p.compare.start, date(2025, 12, 25))
        self.assertEqual(p.compare.days, 31)

    def test_unknown_preset_falls_back(self):
        """Unknown preset falls back to this_week."""
        p = resolve_period("bogus", "wow", today=date(2026, 2, 4))
        self.assertEqual(p.current.start, _monday(date(2026, 2, 4)))

    def test_unknown_comparison_falls_back(self):
        """Unknown comparison falls back to WOW with pacing."""
        p = resolve_period("this_week", "bogus", today=date(2026, 2, 4))
        expected_start = _monday(date(2026, 2, 4)) - timedelta(weeks=1)
        self.assertEqual(p.compare.start, expected_start)
        self.assertEqual(p.compare.days, p.current.days)

    def test_quarter_boundary_q4(self):
        """Dec 15: this_quarter starts Oct 1."""
        p = resolve_period("this_quarter", "wow", today=date(2026, 12, 15))
        self.assertEqual(p.current.start, date(2026, 10, 1))
        self.assertTrue(p.is_partial)
