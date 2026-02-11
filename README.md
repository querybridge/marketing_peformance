# Paid Marketing Performance

A Django application for tracking paid marketing performance across verticals, brands, ad platforms, and campaigns. Aggregates advertising spend, revenue, and budget data into a drill-down dashboard with alerts, trend charts, PDF exports, and CSV imports.

## Key Features

- **Brand performance table** with drill-down: Vertical > Brand > Source > Campaign Type > Campaign (with per-ad-group rows)
- **Brand focus mode**: Click a brand row to scope top-level charts and badges to that single brand; click again to deselect
- **7 automated alerts**: Needs Attention, Pacing Risk, Over/Under Efficient, Doing Well, Missing Revenue, Missing Budget
- **Trend charts**: Daily Revenue, Spend, MTS, Orders, AOV, Net CVR with current vs. comparison period
- **MTS budget bounds**: Upper/lower threshold lines (+-50 bps) on the daily MTS chart
- **Revenue toggle**: Switch between Net Revenue, New Revenue, and Platform Revenue (ad-platform conversion value)
- **CSV import**: Campaign media data from Google Ads, Bing Ads, Meta Ads, and Amazon Marketplace with auto-column detection, cross-source safety guards, and per-ad-group storage when the CSV includes an Ad Group column
- **Revenue upload**: Order/revenue data (CSV) mapped to brands via site IDs
- **Campaign matching**: Reassign auto-created campaigns to the correct brand after import (brand_id prefix auto-assignment)
- **Budget management**: Monthly revenue and MTS targets at vertical level, auto-distributed to brands with manual override support
- **Weekly optimization**: Campaign-level scalability scoring (0–100) with tROAS and seasonality adjustment recommendations, grouped by brand with revenue-at-risk alerts
- **Scoring configuration**: Adjustable component weights, elasticity/efficiency windows, and minimum click thresholds
- **Excel export**: Optimization results with styled headers via openpyxl
- **PDF export**: Landscape report with brand table, exceptions panel, and trend charts
- **HTMX drill-downs**: Click the caret icon on any brand row to expand source/type/campaign breakdowns without full page reload

## Data Model

Star schema with four fact tables at different grains:

| Table | Grain | Source |
|-------|-------|--------|
| FactMediaDaily | campaign x day x ad_group | CSV upload / ad platform export |
| FactOrdersDaily | brand x day | CSV upload from order/revenue system |
| FactVerticalBudget | vertical x month | Manual form entry |
| FactBudget | brand x month | Auto-distributed from vertical budget (with manual override) |

Dimension hierarchy: **Vertical > Brand > Source > Campaign Type > Campaign**

Supporting dimensions: DimDate (calendar spine), DimVertical, DimBrand (with external brand_id), DimSource, DimCampaignType, DimCampaign (with UniqueConstraint on external_id + source, ad_group_name for brand matching), DimSite (site_id-to-vertical mapping for revenue ingest), ScoringConfig (singleton for optimization weights/windows).

Revenue exists only at brand level. Below brand, revenue is allocated proportionally by spend share.

## Metrics

- **Spend, Revenue, Orders** — additive, summed directly
- **Revenue modes**: Net Revenue (backend), New Revenue (backend), Platform Revenue (ad-platform conversion_value)
- **MTS (Cost / Revenue)** — lower is better; displayed as percentage, deltas in basis points
- **AOV (Revenue / Orders)** — ratio, recomputed at each rollup level
- **CTR, CPC, Conversion Rate** — ratios from platform data
- **Impression Share, Click Share** — platform-reported competitive metrics
- **Net CVR (Orders / Clicks)** — cross-fact, brand level only
- **Budget variance** — revenue vs. prorated budget; MTS vs. target

## Quick Start

```bash
pip install -r requirements.txt
python manage.py migrate --run-syncdb
python manage.py seed_data        # populate demo data (5 brands, 460 days)
python manage.py runserver
```

Visit `http://localhost:8000/` for the dashboard.

## URL Routes

| Path | Purpose |
|------|---------|
| `/` | Main dashboard with brand table, alerts, and trend charts |
| `/budgets/` | Monthly budget entry (vertical-level revenue target, MTS target, cancellation rate) |
| `/upload/` | CSV import for campaign media data |
| `/revenue/upload/` | CSV import for order/revenue data (mapped via site IDs) |
| `/sites/upload/` | Upload site-ID-to-vertical mapping |
| `/campaigns/match/` | Reassign campaigns to correct brands after import |
| `/export/pdf/` | Download PDF report for current filters |
| `/verticals/` | Manage verticals |
| `/brands/` | Manage brands |
| `/data-dictionary/` | Searchable metric definitions |
| `/alert-spec/` | Alert rule documentation (Python + SQL) |
| `/optimization/` | Weekly optimization — scalability scores and tROAS recommendations |
| `/optimization/export/` | Download optimization results as Excel |
| `/scoring/` | Scoring configuration — component weights and window sizes |
| `/help/` | User guide and help documentation |

## CSV Import

The upload view accepts CSV exports from:

- **Google Ads** — columns: Campaign, Campaign ID, Campaign type, Day, Impr., Clicks, Cost, etc.
- **Bing Ads** — columns: Campaign name, Campaign ID, Ad distribution, Time period, Impressions, Clicks, Spend, etc. (supports column name variants; scans up to 20 rows for header detection)
- **Meta Ads** — columns: Campaign name, Campaign ID, Campaign type, Day, Impressions, Link clicks, Amount spent, etc.
- **Amazon Marketplace** — supports Sponsored Ads and Feed-based Listings sub-types

Cross-source safety: campaigns are scoped by source during matching, with a DB-level unique constraint on (external_id, source) and a runtime source-verification guard to prevent data contamination across ad platforms.

**Ad-group-level granularity:** When the CSV includes an "Ad group" column, FactMediaDaily stores one row per campaign × day × ad_group. The brand performance drill-down shows individual ad group rows with correct per-ad-group metrics. CSVs without an "Ad group" column still work — rows are stored at the campaign × day level (ad_group_name defaults to blank). Stale campaign-level rows are automatically cleaned up when ad-group-level rows are uploaded for the same campaign and date. The upload summary confirms whether the Ad Group column was detected.

Campaign names with a brand_id prefix (e.g. `123; My Campaign`) are auto-assigned to the matching brand. Ad group names are also used for brand resolution (ad group match takes precedence over campaign name match). Unknown sources use generic column-name detection. Remaining unknown campaigns are created under the "Unknown" brand — use the Match Campaigns view to assign them.

## Date Engine

Presets: This Week, Last Week, This Month, Last Month, This Quarter, Custom

Comparisons: Week-over-Week, Month-over-Month, Year-over-Year (52 weeks back to preserve day-of-week), Custom

Partial presets (This Week/Month/Quarter) trim the comparison window to the same elapsed days for fair pacing comparisons.

## Alert Rules

| Alert | Condition |
|-------|-----------|
| Needs Attention | Revenue < 80% of YOY same period |
| Doing Well | YOY growth >= 10% and MTS within +-50 bps of budget |
| Overly Efficient | MTS >= 50 bps below budget |
| Under Efficient | MTS >= 50 bps above budget |
| Pacing Risk | Revenue down 10%+ WOW while spend flat or up |
| Missing Budget | No budget entered for period |
| Missing Revenue | No order/revenue data uploaded |

## Weekly Optimization

The optimization engine scores each active campaign on **scalability** (0–100) using three weighted components:

| Component | What It Measures | Default Weight |
|-----------|-----------------|----------------|
| A — Elasticity | Spend→click responsiveness (log-log regression) | 40% |
| B — Budget Binding | Room to grow vs implied budget from LY spend share | 30% |
| C — Efficiency Stability | Weekly ROAS/CVR consistency, shrunk toward peer group | 30% |

**Recommendations** (only actionable campaigns shown — holds are excluded):

| Action | Trigger | Cap |
|--------|---------|-----|
| Decrease tROAS | Score ≥ 70 (scale opportunity) | ±15%/week |
| Increase tROAS | Score < 40 (efficiency concern) | ±15%/week |
| Seasonality Adjustment | Score ≥ 60, revenue pacing < 90%, MTS pacing ≤ 110% | +15% |

Campaigns are grouped by brand with revenue-at-risk badges showing projected MTS. Period presets: Last Week, Last 7 Days, Month-to-Date.

## Testing

```bash
python manage.py test dashboard
```

247 tests covering date engine logic, alert classification, PDF generation, CSV import, and optimization scoring.

## Deployment

CI/CD via GitLab (`.gitlab-ci.yml`):
- **Test stage**: migrations, system check, full test suite
- **Deploy stage**: PythonAnywhere via API (git pull, pip install, migrate, reload)

## Tech Stack

- Django 5.0+
- SQLite
- ReportLab (PDF generation)
- openpyxl (Excel export)
- HTMX (drill-down interactions)
- Google Charts (trend visualizations)
- Semantic UI (CSS framework)
