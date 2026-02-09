# Paid Marketing Performance

A Django application for tracking paid marketing performance across verticals, brands, ad platforms, and campaigns. Aggregates advertising spend, revenue, and budget data into a drill-down dashboard with alerts, trend charts, PDF exports, and CSV imports.

## Key Features

- **Brand performance table** with drill-down: Vertical > Brand > Source > Campaign Type > Campaign
- **Brand focus mode**: Click a brand row to scope top-level charts and badges to that single brand; click again to deselect
- **7 automated alerts**: Needs Attention, Pacing Risk, Over/Under Efficient, Doing Well, Missing Revenue, Missing Budget
- **Trend charts**: Daily Revenue, Spend, MTS, Orders, AOV, Net CVR with current vs. comparison period
- **MTS budget bounds**: Upper/lower threshold lines (+-50 bps) on the daily MTS chart
- **Revenue toggle**: Switch between Net Revenue, New Revenue, and Platform Revenue (ad-platform conversion value)
- **CSV import**: Campaign media data from Google Ads, Bing Ads, Meta Ads, and Amazon Marketplace with auto-column detection and cross-source safety guards
- **Revenue upload**: Order/revenue data (CSV) mapped to brands via site IDs
- **Campaign matching**: Reassign auto-created campaigns to the correct brand after import (brand_id prefix auto-assignment)
- **Budget management**: Monthly revenue and MTS targets at vertical level, auto-distributed to brands with manual override support
- **PDF export**: Landscape report with brand table, exceptions panel, and trend charts
- **HTMX drill-downs**: Click the caret icon on any brand row to expand source/type/campaign breakdowns without full page reload

## Data Model

Star schema with four fact tables at different grains:

| Table | Grain | Source |
|-------|-------|--------|
| FactMediaDaily | campaign x day | CSV upload / ad platform export |
| FactOrdersDaily | brand x day | CSV upload from order/revenue system |
| FactVerticalBudget | vertical x month | Manual form entry |
| FactBudget | brand x month | Auto-distributed from vertical budget (with manual override) |

Dimension hierarchy: **Vertical > Brand > Source > Campaign Type > Campaign**

Supporting dimensions: DimDate (calendar spine), DimVertical, DimBrand (with external brand_id), DimSource, DimCampaignType, DimCampaign (with UniqueConstraint on external_id + source), DimSite (site_id-to-vertical mapping for revenue ingest).

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
| `/help/` | User guide and help documentation |

## CSV Import

The upload view accepts CSV exports from:

- **Google Ads** — columns: Campaign, Campaign ID, Campaign type, Day, Impr., Clicks, Cost, etc.
- **Bing Ads** — columns: Campaign name, Campaign ID, Ad distribution, Time period, Impressions, Clicks, Spend, etc. (supports column name variants; scans up to 20 rows for header detection)
- **Meta Ads** — columns: Campaign name, Campaign ID, Campaign type, Day, Impressions, Link clicks, Amount spent, etc.
- **Amazon Marketplace** — supports Sponsored Ads and Feed-based Listings sub-types

Cross-source safety: campaigns are scoped by source during matching, with a DB-level unique constraint on (external_id, source) and a runtime source-verification guard to prevent data contamination across ad platforms.

Campaign names with a brand_id prefix (e.g. `123; My Campaign`) are auto-assigned to the matching brand. Unknown sources use generic column-name detection. Remaining unknown campaigns are created under the "Unknown" brand — use the Match Campaigns view to assign them.

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

## Testing

```bash
python manage.py test dashboard
```

185 tests covering date engine logic, alert classification, PDF generation, and CSV import.

## Deployment

CI/CD via GitLab (`.gitlab-ci.yml`):
- **Test stage**: migrations, system check, full test suite
- **Deploy stage**: PythonAnywhere via API (git pull, pip install, migrate, reload)

## Tech Stack

- Django 5.0+
- SQLite
- ReportLab (PDF generation)
- HTMX (drill-down interactions)
- Google Charts (trend visualizations)
- Semantic UI (CSS framework)
