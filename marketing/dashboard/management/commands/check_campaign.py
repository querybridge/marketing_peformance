"""Diagnostic: check campaign data for a specific brand."""
from datetime import date

from django.core.management.base import BaseCommand
from django.db.models import Sum

from dashboard.models import DimCampaign, DimBrand, FactMediaDaily
from dashboard import services


class Command(BaseCommand):
    help = "Check campaign-level data for Savoy House"

    def handle(self, **opts):
        today = date.today()
        period = services.resolve_period(preset="this_week", comparison="wow", today=today)
        w = period.current
        self.stdout.write(f"\nPeriod: {w.start} to {w.end}\n")

        # Find Savoy House brand(s)
        savoy_brands = DimBrand.objects.filter(name__icontains="savoy")
        self.stdout.write(f"=== Savoy House DimBrand records ===")
        for b in savoy_brands:
            self.stdout.write(f"  pk={b.id}, brand_id={b.brand_id}, name={b.name}, vertical={b.vertical_id}")

        # Find all DimCampaign records for "[ADL] [PLA] Brands 3"
        self.stdout.write(f"\n=== DimCampaign records for '[ADL] [PLA] Brands 3' ===")
        brands3 = DimCampaign.objects.filter(name__icontains="Brands 3").select_related("brand")
        for c in brands3:
            # Get FactMediaDaily spend
            spend = FactMediaDaily.objects.filter(
                campaign=c,
                date__date__gte=w.start, date__date__lte=w.end,
            ).aggregate(spend=Sum("cost"), conv=Sum("conversion_value"))
            self.stdout.write(
                f"  pk={c.id}, brand={c.brand.name}(pk={c.brand_id}), "
                f"ext_id='{c.external_id}', ad_group='{c.ad_group_name}', "
                f"spend={spend['spend']}, conv_value={spend['conv']}"
            )

        # Find all DimCampaign records for "Savoy House - Dedicated PLA"
        self.stdout.write(f"\n=== DimCampaign records for '159;Savoy House - Dedicated PLA' ===")
        dedicated = DimCampaign.objects.filter(name__icontains="Savoy House - Dedicated PLA").select_related("brand")
        for c in dedicated:
            spend = FactMediaDaily.objects.filter(
                campaign=c,
                date__date__gte=w.start, date__date__lte=w.end,
            ).aggregate(spend=Sum("cost"), conv=Sum("conversion_value"))
            self.stdout.write(
                f"  pk={c.id}, brand={c.brand.name}(pk={c.brand_id}), "
                f"ext_id='{c.external_id}', ad_group='{c.ad_group_name}', "
                f"spend={spend['spend']}, conv_value={spend['conv']}"
            )

        # All campaigns for Savoy House brand(s) with spend
        self.stdout.write(f"\n=== All Savoy House campaigns with spend in period ===")
        for sb in savoy_brands:
            camps = DimCampaign.objects.filter(brand=sb)
            for c in camps:
                agg = FactMediaDaily.objects.filter(
                    campaign=c,
                    date__date__gte=w.start, date__date__lte=w.end,
                ).aggregate(spend=Sum("cost"), conv=Sum("conversion_value"))
                if agg["spend"]:
                    self.stdout.write(
                        f"  {c.name} | ad_group={c.ad_group_name} | "
                        f"spend={agg['spend']} | conv={agg['conv']}"
                    )

        # Per-date detail for specific campaigns
        self.stdout.write(f"\n=== Per-date FactMediaDaily for Savoy House campaigns ===")
        for sb in savoy_brands:
            camps = DimCampaign.objects.filter(brand=sb)
            for c in camps:
                rows = FactMediaDaily.objects.filter(
                    campaign=c,
                    date__date__gte=w.start, date__date__lte=w.end,
                ).select_related("date")
                for r in rows:
                    if r.cost > 0 or r.conversion_value > 0:
                        self.stdout.write(
                            f"  {c.name} | {r.date.date} | cost={r.cost} | "
                            f"conv_val={r.conversion_value} | clicks={r.clicks} | impr={r.impressions}"
                        )
