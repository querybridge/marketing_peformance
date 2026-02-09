from django.db import models

# ═══════════════════════════════════════════════════════════════════════════
# DIMENSIONS — drill path: Vertical → Brand → Source → CampaignType → Campaign
# Ownership is strictly one-to-one (no campaign spans brands/verticals).
# ═══════════════════════════════════════════════════════════════════════════


class DimDate(models.Model):
    """Calendar spine — one row per day, pre-populated 2024-01-01 → 2027-12-31."""

    date = models.DateField(unique=True)
    year = models.SmallIntegerField(db_index=True)
    quarter = models.SmallIntegerField()                         # 1-4
    month = models.SmallIntegerField()                           # 1-12
    week = models.SmallIntegerField()                            # ISO 1-53
    day_of_week = models.SmallIntegerField()                     # 0=Mon … 6=Sun
    day_of_month = models.SmallIntegerField()
    day_of_year = models.SmallIntegerField()
    is_weekend = models.BooleanField()
    year_month = models.CharField(max_length=7, db_index=True)   # "2026-01"
    year_week = models.CharField(max_length=8, db_index=True)    # "2026-W05"
    week_start = models.DateField()                              # Monday of this week
    month_start = models.DateField()                             # 1st of this month

    class Meta:
        ordering = ["date"]

    def __str__(self):
        return str(self.date)


class DimVertical(models.Model):
    """Top-level business segment grouping brands."""

    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class DimSite(models.Model):
    """Maps backend site IDs to verticals for revenue ingest."""
    site_id = models.IntegerField(unique=True)
    vertical = models.ForeignKey(
        DimVertical, on_delete=models.CASCADE, related_name="sites",
    )
    site_name = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        ordering = ["site_id"]

    def __str__(self):
        return f"Site {self.site_id} → {self.vertical.name}"


class DimBrand(models.Model):
    """P&L-owning brand within a vertical."""

    name = models.CharField(max_length=200)
    slug = models.SlugField()
    brand_id = models.IntegerField(
        null=True, blank=True,
        help_text="External brand identifier used in campaign naming convention",
    )
    vertical = models.ForeignKey(
        DimVertical, on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="brands",
    )

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["brand_id", "vertical"],
                name="unique_brand_id_per_vertical",
                condition=models.Q(brand_id__isnull=False),
            ),
        ]

    def __str__(self):
        return self.name


class DimSource(models.Model):
    """Ad platform — Google Ads, Bing Ads."""

    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class DimCampaignType(models.Model):
    """Strategy bucket — Brand, Non-Brand, Shopping, Retargeting."""

    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class DimCampaign(models.Model):
    """Leaf dimension — one row per campaign as it exists in the ad platform."""

    STATUS = [
        ("active", "Active"),
        ("paused", "Paused"),
        ("completed", "Completed"),
    ]

    AMAZON_TYPES = [
        ("", "—"),
        ("sponsored", "Sponsored Ads"),
        ("feed", "Feed-based Listings"),
    ]

    name = models.CharField(max_length=300)
    external_id = models.CharField(max_length=200, blank=True, default="")
    brand = models.ForeignKey(
        DimBrand, on_delete=models.CASCADE, related_name="campaigns"
    )
    source = models.ForeignKey(
        DimSource, on_delete=models.CASCADE, related_name="campaigns"
    )
    campaign_type = models.ForeignKey(
        DimCampaignType, on_delete=models.CASCADE, related_name="campaigns"
    )
    amazon_type = models.CharField(
        max_length=20, blank=True, default="",
        choices=AMAZON_TYPES,
        help_text="Amazon sub-type: Sponsored Ads or Feed-based Listings",
    )
    status = models.CharField(max_length=20, choices=STATUS, default="active")

    class Meta:
        ordering = ["source", "campaign_type", "name"]

    def __str__(self):
        return f"{self.source} / {self.name}"


# ═══════════════════════════════════════════════════════════════════════════
# FACT TABLES — three separate grains, three separate data sources
# ═══════════════════════════════════════════════════════════════════════════


class FactMediaDaily(models.Model):
    """Grain: campaign × day.  Source: Google Ads / Bing Ads API or CSV export."""

    campaign = models.ForeignKey(
        DimCampaign, on_delete=models.CASCADE, related_name="media"
    )
    date = models.ForeignKey(
        DimDate, on_delete=models.PROTECT, related_name="media"
    )
    impressions = models.IntegerField(default=0)
    impression_share = models.DecimalField(
        max_digits=5, decimal_places=4, null=True, blank=True,
        help_text="0.0000–1.0000; NULL when platform does not report",
    )
    click_share = models.DecimalField(
        max_digits=5, decimal_places=4, null=True, blank=True,
    )
    clicks = models.IntegerField(default=0)
    cost = models.DecimalField(max_digits=12, decimal_places=2)
    conversions = models.IntegerField(default=0)
    conversion_value = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
    )

    class Meta:
        unique_together = ["campaign", "date"]
        indexes = [models.Index(fields=["date", "campaign"])]

    def __str__(self):
        return f"{self.campaign} | {self.date}"


class FactOrdersDaily(models.Model):
    """Grain: brand × day.  Source: CSV upload from order / revenue system."""

    brand = models.ForeignKey(
        DimBrand, on_delete=models.CASCADE, related_name="orders"
    )
    date = models.ForeignKey(
        DimDate, on_delete=models.PROTECT, related_name="orders"
    )
    orders = models.IntegerField(default=0)
    new_revenue = models.DecimalField(max_digits=12, decimal_places=2)
    net_revenue = models.DecimalField(max_digits=12, decimal_places=2)
    loaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ["brand", "date"]
        indexes = [models.Index(fields=["date", "brand"])]

    def __str__(self):
        return f"{self.brand} | {self.date}"


class FactVerticalBudget(models.Model):
    """Grain: vertical × month.  Stores vertical-level targets independently of brands."""

    vertical = models.ForeignKey(
        DimVertical, on_delete=models.CASCADE, related_name="vertical_budgets"
    )
    month = models.ForeignKey(
        DimDate, on_delete=models.PROTECT, related_name="vertical_budgets",
        help_text="First-of-month row in DimDate",
    )
    revenue_budget = models.DecimalField(max_digits=12, decimal_places=2)
    mts_budget = models.DecimalField(
        max_digits=6, decimal_places=4,
        help_text="Target MTS ratio (Cost ÷ Revenue), e.g. 0.2500",
    )
    cancellation_rate = models.DecimalField(
        max_digits=5, decimal_places=4, default=0,
        help_text="Cancellation rate as a ratio, e.g. 0.0800 = 8%",
    )

    class Meta:
        unique_together = ["vertical", "month"]

    def __str__(self):
        return f"{self.vertical} — {self.month}"


class FactBudget(models.Model):
    """Grain: brand × month.  Source: manual form entry."""

    brand = models.ForeignKey(
        DimBrand, on_delete=models.CASCADE, related_name="budgets"
    )
    month = models.ForeignKey(
        DimDate, on_delete=models.PROTECT, related_name="budgets",
        help_text="First-of-month row in DimDate",
    )
    revenue_budget = models.DecimalField(max_digits=12, decimal_places=2)
    mts_budget = models.DecimalField(
        max_digits=6, decimal_places=4,
        help_text="Target MTS ratio (Cost ÷ Revenue), e.g. 0.2500",
    )
    cancellation_rate = models.DecimalField(
        max_digits=5, decimal_places=4, default=0,
        help_text="Brand cancellation rate as a ratio, e.g. 0.0800 = 8%",
    )
    manually_overridden = models.BooleanField(
        default=False,
        help_text="True if brand budget was manually edited (not auto-allocated).",
    )

    class Meta:
        unique_together = ["brand", "month"]

    def __str__(self):
        return f"{self.brand} — {self.month}"
