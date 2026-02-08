# Marketing Performance Dashboard

A Django application for tracking weekly marketing performance across brands, ad platforms, and campaigns. Aggregates advertising spend, revenue, and budget data into a drill-down dashboard with alerts, trend charts, PDF exports, and CSV imports.

## Key Features

- **Brand performance table** with drill-down: Vertical > Brand > Source > Campaign Type > Campaign
- **7 automated alerts**: Needs Attention, Pacing Risk, Over/Under Efficient, Doing Well, Missing Revenue, Missing Budget
- **Trend charts**: Daily Revenue, Spend, MTS, Orders, AOV, Net CVR with current vs. comparison period
- **MTS budget bounds**: Upper/lower threshold lines (+-50 bps) on the daily MTS chart
- **CSV import**: Campaign media data from Google Ads, Bing Ads, Meta Ads with auto-column detection
- **Campaign matching**: Reassign auto-created campaigns to the correct brand after import
- **Budget management**: Monthly revenue and MTS targets per vertical, prorated to any date window
- **PDF export**: Landscape report with brand table, exceptions panel, and trend charts
- **HTMX drill-downs**: Click any brand row to expand source/type/campaign breakdowns without full page reload

## Data Model

Star schema with three fact tables at different grains:

| Table | Grain | Source |
|-------|-------|--------|
| FactMediaDaily | campaign x day | CSV upload / ad platform export |
| FactOrdersDaily | brand x day | CSV upload from order system |
| FactBudget | brand x month | Manual form entry |

Dimension hierarchy: **Vertical > Brand > Source > Campaign Type > Campaign**

Revenue exists only at brand level. Below brand, revenue is allocated proportionally by spend share.

## Metrics

- **Spend, Revenue, Orders** — additive, summed directly
- **MTS (Cost / Revenue)** — lower is better; displayed as percentage, deltas in basis points
- **AOV (Revenue / Orders)** — ratio, recomputed at each rollup level
- **CTR, CPC, Conversion Rate** — ratios from platform data
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
| `/budgets/` | Monthly budget entry (revenue target, MTS target, cancellation rate) |
| `/upload/` | CSV import for campaign media data |
| `/campaigns/match/` | Reassign campaigns to correct brands after import |
| `/export/pdf/` | Download PDF report for current filters |
| `/verticals/` | Manage verticals |
| `/brands/` | Manage brands |
| `/data-dictionary/` | Searchable metric definitions |
| `/alert-spec/` | Alert rule documentation (Python + SQL) |

## CSV Import

The upload view accepts CSV exports from:

- **Google Ads** — columns: Campaign, Campaign ID, Campaign type, Day, Impr., Clicks, Cost, etc.
- **Bing Ads** — columns: Campaign name, Campaign ID, Campaign type, Time period, Impressions, Clicks, Spend, etc.
- **Meta Ads** — columns: Campaign name, Campaign ID, Campaign type, Day, Impressions, Link clicks, Amount spent, etc.

Unknown sources use generic column-name detection. Unknown campaigns are auto-created under the "Unknown" brand — use the Match Campaigns view to assign them to the correct brand.

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

151 tests covering date engine logic, alert classification, and PDF generation.

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
