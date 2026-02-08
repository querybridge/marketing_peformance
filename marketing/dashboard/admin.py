from django.contrib import admin

from .models import (
    DimBrand,
    DimCampaign,
    DimCampaignType,
    DimDate,
    DimSource,
    DimVertical,
    FactBudget,
    FactMediaDaily,
    FactOrdersDaily,
    FactVerticalBudget,
)


@admin.register(DimDate)
class DimDateAdmin(admin.ModelAdmin):
    list_display = ["date", "year", "quarter", "month", "week", "is_weekend"]
    list_filter = ["year", "quarter"]
    search_fields = ["date"]


@admin.register(DimVertical)
class DimVerticalAdmin(admin.ModelAdmin):
    list_display = ["name", "slug"]


@admin.register(DimBrand)
class DimBrandAdmin(admin.ModelAdmin):
    list_display = ["name", "brand_id", "vertical", "slug"]
    list_filter = ["vertical"]


@admin.register(DimSource)
class DimSourceAdmin(admin.ModelAdmin):
    list_display = ["name", "slug"]


@admin.register(DimCampaignType)
class DimCampaignTypeAdmin(admin.ModelAdmin):
    list_display = ["name", "slug"]


@admin.register(DimCampaign)
class DimCampaignAdmin(admin.ModelAdmin):
    list_display = ["name", "brand", "source", "campaign_type", "amazon_type", "status"]
    list_filter = ["status", "source", "campaign_type", "amazon_type", "brand__vertical"]
    search_fields = ["name"]


@admin.register(FactMediaDaily)
class FactMediaDailyAdmin(admin.ModelAdmin):
    list_display = ["campaign", "date", "cost", "clicks", "conversions"]
    list_filter = ["campaign__source", "campaign__campaign_type"]
    raw_id_fields = ["campaign", "date"]


@admin.register(FactOrdersDaily)
class FactOrdersDailyAdmin(admin.ModelAdmin):
    list_display = ["brand", "date", "orders", "new_revenue", "net_revenue"]
    list_filter = ["brand__vertical"]
    raw_id_fields = ["brand", "date"]


@admin.register(FactVerticalBudget)
class FactVerticalBudgetAdmin(admin.ModelAdmin):
    list_display = ["vertical", "month", "revenue_budget", "mts_budget", "cancellation_rate"]
    list_filter = ["vertical"]
    raw_id_fields = ["month"]


@admin.register(FactBudget)
class FactBudgetAdmin(admin.ModelAdmin):
    list_display = ["brand", "month", "revenue_budget", "mts_budget"]
    list_filter = ["brand__vertical"]
    raw_id_fields = ["brand", "month"]
