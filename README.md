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
- **Weekly report**: Structured weekly performance summary with auto-filled executive snapshot (Stephen Few-style scorecards), brand performance table with alert badges, 9 commentary sections, save-draft persistence, and Word doc export
- **Login & access control**: Middleware-based authentication with 6 user groups, per-view permission matrix (full/read-only/no-access), email domain whitelist, and HTMX-aware session handling
- **User management**: Admin page to create users, assign groups, and manage access
- **Excel export**: Optimization results with styled headers via openpyxl
- **Word export**: Weekly report as formatted .docx with executive snapshot table, brand metrics, and commentary sections
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

Supporting dimensions: DimDate (calendar spine), DimVertical, DimBrand (with external brand_id), DimSource, DimCampaignType, DimCampaign (with UniqueConstraint on external_id + source, ad_group_name for brand matching), DimSite (site_id-to-vertical mapping for revenue ingest), ScoringConfig (singleton for optimization weights/windows), WeeklyReport (draft per vertical per week with commentary and brand notes JSON).

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
python manage.py setup_groups     # create the 6 permission groups
python manage.py createsuperuser  # create initial admin account
python manage.py seed_data        # populate demo data (5 brands, 460 days)
python manage.py runserver
```

Visit `http://localhost:8000/` — unauthenticated users are redirected to the login page.

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
| `/weekly-report/` | Weekly report with executive snapshot, brand metrics, and commentary |
| `/weekly-report/export/` | Download weekly report as Word document |
| `/users/` | User management — create accounts and assign groups |
| `/help/` | User guide and help documentation |
| `/accounts/login/` | Login page |
| `/accounts/logout/` | Logout (POST) |
| `/accounts/password_change/` | Change password |

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

## Weekly Report

The weekly report page (`/weekly-report/`) provides a structured performance summary for each vertical:

- **Executive Snapshot** — Stephen Few-style scorecards showing Revenue, Spend, MTS (with bullet graph vs goal), Top Revenue Driver, and Largest YoY Shift. Revenue and Spend cards include color-coded WoW/YoY directional indicators. MTS uses a 3-tier status: green (within ±50 bps of goal), yellow (within ±75 bps), red (beyond ±75 bps).
- **Performance by MFG** — Top 5 brands by revenue plus any flagged as Needs Attention or Pacing Risk, with WoW/YoY change percentages, MTS, alert badges, and per-brand notes.
- **9 Commentary sections** — Summary Statement, Major YoY Shifts, What's Working Well, What Needs Attention, What We're Doing About It, Platform Testing & Experiments, Channel Mix Observations, Risk & Opportunity Outlook, GM Discussion Points. Each has placeholder examples.
- **Save Draft** — Persists all commentary and brand notes per vertical per week (one draft per vertical per week_start, stored in WeeklyReport model with JSONField for brand notes).
- **Word Export** — Download as formatted .docx via python-docx with executive snapshot table, brand metrics, and all commentary sections.

Data is auto-filled from FactOrdersDaily and FactMediaDaily using the same service functions as the Overview page. WoW uses a 7-day offset; YoY uses a 52-week offset for weekday alignment.

## Authentication & Access Control

Middleware-based (`DashboardAccessMiddleware`) — no per-view decorators required.

- **Login required** for all dashboard pages. Unauthenticated requests redirect to `/accounts/login/`. HTMX requests return 204 with `HX-Redirect` header for clean client-side redirect.
- **6 user groups**: Campaign Manager, General Manager, Admin, Reporting, Agency, SuperUser
- **3 access levels**: `full` (GET+POST), `read_only` (GET only, forms hidden), `no_access` (403)
- **Email domain whitelist**: Only approved domains can be used when creating new users
- **Permission matrix** (`dashboard/auth_config.py`):

| Page | Campaign Mgr | General Mgr | Admin | Reporting | Agency | SuperUser |
|------|:---:|:---:|:---:|:---:|:---:|:---:|
| Overview | Full | Full | Full | Full | Full | Full |
| Budgets | RO | Full | Full | RO | RO | Full |
| Campaign Data | Full | — | Full | Full | Full | Full |
| Revenue Data | Full | — | Full | Full | RO | Full |
| Weekly Optimization | Full | Full | Full | Full | Full | Full |
| Weekly Report | Full | RO | Full | Full | RO | Full |
| Verticals | RO | Full | Full | RO | RO | Full |
| Brands | RO | Full | Full | RO | RO | Full |
| Data Dictionary | Full | Full | Full | Full | Full | Full |
| Alert Rules | Full | Full | Full | Full | Full | Full |
| Match Campaigns | Full | RO | Full | Full | Full | Full |
| Site ID Mapping | RO | Full | Full | RO | — | Full |
| Scoring | Full | — | Full | RO | — | Full |
| Help | Full | Full | Full | Full | Full | Full |
| Add User | — | — | Full | — | — | Full |

Key files: `auth_config.py` (matrix + helper), `middleware.py` (enforcement), `context_processors.py` (nav visibility), `management/commands/setup_groups.py` (group creation).

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
- python-docx (Word export)
- HTMX (drill-down interactions)
- Google Charts (trend visualizations)
- Semantic UI (CSS framework)
