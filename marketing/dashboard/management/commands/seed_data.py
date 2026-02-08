"""
Seed 460 days of demo data across 5 brands, 2 sources, 18 campaigns.

Alert patterns baked in:
  GearShop      → Doing Well       (strong YOY growth, on-MTS)
  StyleHQ       → Overly Efficient (MTS well below budget)
  PetSupply     → Needs Attention  (revenue collapse last 30 days)
  FixIt Pros    → Pacing Risk      (revenue declining, spend flat)
  CleanHome     → normal           (no flag)
"""

import random
from datetime import date, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand

from dashboard.models import (
    DimBrand,
    DimCampaign,
    DimCampaignType,
    DimDate,
    DimSource,
    DimVertical,
    FactBudget,
    FactMediaDaily,
    FactOrdersDaily,
)

DAYS_BACK = 460  # ~65 weeks — enough for YOY + 90-day window

VERTICALS = [
    ("Home Services", "home-services"),
    ("E-Commerce", "ecommerce"),
]

BRANDS = [
    # (name, slug, vertical_slug)
    ("FixIt Pros", "fixit-pros", "home-services"),
    ("CleanHome", "cleanhome", "home-services"),
    ("GearShop", "gearshop", "ecommerce"),
    ("StyleHQ", "stylehq", "ecommerce"),
    ("PetSupply", "petsupply", "ecommerce"),
]

SOURCES = [("Google Ads", "google-ads"), ("Bing Ads", "bing-ads")]

CAMPAIGN_TYPES = [
    ("Brand", "brand"),
    ("Non-Brand", "non-brand"),
    ("Shopping", "shopping"),
    ("Retargeting", "retargeting"),
]

# (name, brand_slug, source_slug, type_slug, status, daily_spend)
CAMPAIGNS = [
    # FixIt Pros — pacing-risk brand
    ("Brand Terms",            "fixit-pros", "google-ads", "brand",       "active", 120),
    ("Non-Brand Services",     "fixit-pros", "google-ads", "non-brand",   "active", 250),
    ("Retargeting Visitors",   "fixit-pros", "google-ads", "retargeting", "active",  80),
    ("Brand Terms",            "fixit-pros", "bing-ads",   "brand",       "active",  50),
    # CleanHome — normal
    ("Brand Terms",            "cleanhome",  "google-ads", "brand",       "active",  80),
    ("Non-Brand Cleaning",     "cleanhome",  "google-ads", "non-brand",   "active", 150),
    # GearShop — doing-well brand (biggest spender)
    ("Brand Terms",            "gearshop",   "google-ads", "brand",       "active", 300),
    ("Non-Brand Generic",      "gearshop",   "google-ads", "non-brand",   "active", 500),
    ("Shopping Core",          "gearshop",   "google-ads", "shopping",    "active", 400),
    ("Retargeting Cart",       "gearshop",   "google-ads", "retargeting", "active", 150),
    ("Brand Terms",            "gearshop",   "bing-ads",   "brand",       "active", 100),
    ("Shopping Core",          "gearshop",   "bing-ads",   "shopping",    "active", 150),
    # StyleHQ — overly-efficient brand
    ("Brand Terms",            "stylehq",    "google-ads", "brand",       "active", 200),
    ("Non-Brand Fashion",      "stylehq",    "google-ads", "non-brand",   "active", 350),
    ("Shopping Catalog",       "stylehq",    "google-ads", "shopping",    "active", 250),
    # PetSupply — needs-attention brand
    ("Brand Terms",            "petsupply",  "google-ads", "brand",       "active", 100),
    ("Non-Brand Pet Care",     "petsupply",  "google-ads", "non-brand",   "active", 200),
    ("Shopping Feed",          "petsupply",  "google-ads", "shopping",    "paused",   0),
]

# Per (type_slug, source_slug) — ctr, cvr profiles
PROFILES = {
    ("brand",       "google-ads"): {"ctr": 0.12,  "cvr": 0.08},
    ("brand",       "bing-ads"):   {"ctr": 0.10,  "cvr": 0.07},
    ("non-brand",   "google-ads"): {"ctr": 0.04,  "cvr": 0.03},
    ("non-brand",   "bing-ads"):   {"ctr": 0.035, "cvr": 0.025},
    ("shopping",    "google-ads"): {"ctr": 0.05,  "cvr": 0.04},
    ("shopping",    "bing-ads"):   {"ctr": 0.045, "cvr": 0.035},
    ("retargeting", "google-ads"): {"ctr": 0.06,  "cvr": 0.05},
    ("retargeting", "bing-ads"):   {"ctr": 0.05,  "cvr": 0.04},
}
DEFAULT_PROFILE = {"ctr": 0.03, "cvr": 0.025}

# Brand-level MTS profiles and order patterns
BRAND_PROFILES = {
    #                   mts_ratio  mts_noise  yoy_growth  recent_factor
    "fixit-pros":      (0.28,      0.04,      1.05,       0.88),  # pacing risk: recent rev down
    "cleanhome":       (0.24,      0.03,      1.03,       1.00),  # stable
    "gearshop":        (0.20,      0.02,      1.18,       1.10),  # doing well: YOY up
    "stylehq":         (0.17,      0.02,      1.06,       1.02),  # overly efficient: MTS << budget
    "petsupply":       (0.24,      0.03,      1.04,       0.04),  # needs attention: collapse
}

# Budget targets (revenue_budget/month, mts_budget)
BRAND_BUDGETS = {
    "fixit-pros": (150_000, 0.2800),
    "cleanhome":  (80_000,  0.2500),
    "gearshop":   (400_000, 0.2000),
    "stylehq":    (200_000, 0.2200),
    "petsupply":  (120_000, 0.2400),
}


class Command(BaseCommand):
    help = "Seed database with demo marketing data (460 days)"

    def handle(self, *args, **options):
        random.seed(42)

        self._clear()
        self._build_date_dim()
        verts = self._build_verticals()
        brands = self._build_brands(verts)
        sources = self._build_sources()
        types = self._build_types()
        campaigns = self._build_campaigns(brands, sources, types)
        self._build_media(campaigns, sources, types)
        self._build_orders(brands)
        self._build_budgets(brands)

        self.stdout.write(self.style.SUCCESS("Done."))

    # ── helpers ───────────────────────────────────────────────

    def _clear(self):
        self.stdout.write("Clearing existing data ...")
        FactBudget.objects.all().delete()
        FactOrdersDaily.objects.all().delete()
        FactMediaDaily.objects.all().delete()
        DimCampaign.objects.all().delete()
        DimCampaignType.objects.all().delete()
        DimSource.objects.all().delete()
        DimBrand.objects.all().delete()
        DimVertical.objects.all().delete()
        DimDate.objects.all().delete()

    def _build_date_dim(self):
        self.stdout.write("Building DimDate 2024-01-01 → 2027-12-31 ...")
        start = date(2024, 1, 1)
        end = date(2027, 12, 31)
        bulk = []
        cur = start
        while cur <= end:
            iso = cur.isocalendar()
            bulk.append(DimDate(
                date=cur,
                year=cur.year,
                quarter=(cur.month - 1) // 3 + 1,
                month=cur.month,
                week=iso[1],
                day_of_week=cur.weekday(),
                day_of_month=cur.day,
                day_of_year=cur.timetuple().tm_yday,
                is_weekend=cur.weekday() >= 5,
                year_month=cur.strftime("%Y-%m"),
                year_week=f"{iso[0]}-W{iso[1]:02d}",
                week_start=cur - timedelta(days=cur.weekday()),
                month_start=cur.replace(day=1),
            ))
            cur += timedelta(days=1)
        DimDate.objects.bulk_create(bulk)
        self.stdout.write(f"  {len(bulk)} date rows")

    def _build_verticals(self):
        out = {}
        for name, slug in VERTICALS:
            out[slug] = DimVertical.objects.create(name=name, slug=slug)
        return out

    def _build_brands(self, verts):
        out = {}
        for name, slug, v_slug in BRANDS:
            out[slug] = DimBrand.objects.create(
                name=name, slug=slug, vertical=verts[v_slug],
            )
        return out

    def _build_sources(self):
        out = {}
        for name, slug in SOURCES:
            out[slug] = DimSource.objects.create(name=name, slug=slug)
        return out

    def _build_types(self):
        out = {}
        for name, slug in CAMPAIGN_TYPES:
            out[slug] = DimCampaignType.objects.create(name=name, slug=slug)
        return out

    def _build_campaigns(self, brands, sources, types):
        out = []
        for name, b_slug, s_slug, t_slug, status, daily_spend in CAMPAIGNS:
            c = DimCampaign.objects.create(
                name=name,
                brand=brands[b_slug],
                source=sources[s_slug],
                campaign_type=types[t_slug],
                status=status,
            )
            out.append((c, b_slug, s_slug, t_slug, status, daily_spend))
        self.stdout.write(f"  {len(out)} campaigns")
        return out

    def _build_media(self, campaigns, sources, types):
        self.stdout.write("Building FactMediaDaily ...")
        today = date.today()
        start_date = today - timedelta(days=DAYS_BACK)

        # Pre-fetch DimDate PKs
        date_map = {d.date: d for d in DimDate.objects.filter(
            date__gte=start_date, date__lte=today,
        )}

        total = 0
        for camp, b_slug, s_slug, t_slug, status, daily_spend in campaigns:
            if daily_spend == 0:
                continue  # paused with no spend

            profile = PROFILES.get((t_slug, s_slug), DEFAULT_PROFILE)
            bulk = []

            for offset in range(DAYS_BACK + 1):
                d = start_date + timedelta(days=offset)
                if d > today or d not in date_map:
                    continue
                # Paused campaigns: stop 30 days ago
                if status == "paused" and d > today - timedelta(days=30):
                    continue

                dow_mod = 0.70 if d.weekday() >= 5 else 1.0
                spend = daily_spend * dow_mod * (0.85 + random.random() * 0.30)

                impressions = max(1, int(spend / 0.005 * (0.85 + random.random() * 0.30)))
                clicks = max(0, int(impressions * profile["ctr"] * (0.85 + random.random() * 0.30)))
                conversions = max(0, int(clicks * profile["cvr"] * (0.85 + random.random() * 0.30)))
                conv_value = conversions * (50 + random.random() * 100)

                bulk.append(FactMediaDaily(
                    campaign=camp,
                    date=date_map[d],
                    impressions=impressions,
                    clicks=clicks,
                    cost=Decimal(str(round(spend, 2))),
                    conversions=conversions,
                    conversion_value=Decimal(str(round(conv_value, 2))),
                ))

            FactMediaDaily.objects.bulk_create(bulk)
            total += len(bulk)

        self.stdout.write(f"  {total} media rows")

    def _build_orders(self, brands):
        """
        Build FactOrdersDaily at brand × day grain.
        Revenue = brand total spend / brand MTS ratio, then apply patterns.
        """
        self.stdout.write("Building FactOrdersDaily ...")
        today = date.today()
        start_date = today - timedelta(days=DAYS_BACK)

        date_map = {d.date: d for d in DimDate.objects.filter(
            date__gte=start_date, date__lte=today,
        )}

        total = 0
        for b_slug, brand in brands.items():
            mts_base, mts_noise, yoy_growth, recent_factor = BRAND_PROFILES[b_slug]
            collapse_day = 30 if b_slug == "petsupply" else 0

            # Get daily total spend for this brand from media facts
            from django.db.models import Sum as DjSum
            daily_spend = {}
            for row in FactMediaDaily.objects.filter(
                campaign__brand=brand,
            ).values("date__date").annotate(s=DjSum("cost")):
                daily_spend[row["date__date"]] = float(row["s"])

            bulk = []
            for offset in range(DAYS_BACK + 1):
                d = start_date + timedelta(days=offset)
                if d > today or d not in date_map:
                    continue

                spend = daily_spend.get(d, 0)
                if spend == 0:
                    continue

                # Apply MTS noise
                mts = max(0.05, mts_base + random.uniform(-mts_noise, mts_noise))
                revenue = spend / mts

                # Apply recent revenue factor (for alert patterns)
                days_ago = (today - d).days
                if collapse_day and days_ago < collapse_day:
                    revenue *= recent_factor
                elif days_ago < 14:
                    # Gradual application of recent_factor over last 2 weeks
                    blend = 1.0 - (days_ago / 14.0)
                    factor = 1.0 + (recent_factor - 1.0) * blend
                    revenue *= factor

                # YOY growth: scale up recent year vs prior year
                if d.year >= today.year:
                    revenue *= yoy_growth

                orders = max(1, int(revenue / (80 + random.random() * 60)))
                net_revenue = revenue * (0.88 + random.random() * 0.08)

                bulk.append(FactOrdersDaily(
                    brand=brand,
                    date=date_map[d],
                    orders=orders,
                    new_revenue=Decimal(str(round(revenue, 2))),
                    net_revenue=Decimal(str(round(net_revenue, 2))),
                ))

            FactOrdersDaily.objects.bulk_create(bulk)
            total += len(bulk)
            self.stdout.write(f"  {brand.name}: {len(bulk)} days")

        self.stdout.write(f"  {total} order rows total")

    def _build_budgets(self, brands):
        """Monthly budgets for each brand, covering last 18 months."""
        self.stdout.write("Building FactBudget ...")
        today = date.today()

        total = 0
        for b_slug, brand in brands.items():
            rev_budget, mts_budget = BRAND_BUDGETS[b_slug]

            for month_offset in range(-18, 3):
                m = today.month + month_offset
                y = today.year + (m - 1) // 12
                m = (m - 1) % 12 + 1
                first = date(y, m, 1)

                dim_date = DimDate.objects.filter(date=first).first()
                if not dim_date:
                    continue

                FactBudget.objects.create(
                    brand=brand,
                    month=dim_date,
                    revenue_budget=Decimal(str(rev_budget)),
                    mts_budget=Decimal(str(mts_budget)),
                )
                total += 1

        self.stdout.write(f"  {total} budget rows")
