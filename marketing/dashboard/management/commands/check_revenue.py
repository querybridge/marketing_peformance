"""Quick diagnostic: show FactOrdersDaily totals vs what brand_table returns."""
from datetime import date

from django.core.management.base import BaseCommand
from django.db.models import Sum, Count

from dashboard.models import FactOrdersDaily, DimBrand
from dashboard import services


class Command(BaseCommand):
    help = "Check FactOrdersDaily data vs brand_table output"

    def handle(self, **opts):
        today = date.today()
        # Check both this_week and this_month
        for preset in ("this_week", "this_month"):
            period = services.resolve_period(preset=preset, comparison="wow", today=today)
            w = period.current
            self.stdout.write(f"\n--- {preset}: {w.start} to {w.end} ({w.days} days) ---")
            qs_p = FactOrdersDaily.objects.filter(
                date__date__gte=w.start, date__date__lte=w.end,
            )
            t = qs_p.aggregate(new=Sum("new_revenue"), rows=Count("id"))
            self.stdout.write(f"  rows={t['rows']}  new_revenue={t['new']}")

        period = services.resolve_period(preset="this_week", comparison="wow", today=today)
        w = period.current

        self.stdout.write(f"\n=== Period: {w.start} to {w.end} ({w.days} days) ===\n")

        # Raw DB totals for the period
        qs = FactOrdersDaily.objects.filter(
            date__date__gte=w.start, date__date__lte=w.end,
        )
        totals = qs.aggregate(
            new=Sum("new_revenue"),
            net=Sum("net_revenue"),
            orders=Sum("orders"),
            rows=Count("id"),
        )
        self.stdout.write(f"FactOrdersDaily rows in period: {totals['rows']}")
        self.stdout.write(f"  new_revenue total: {totals['new']}")
        self.stdout.write(f"  net_revenue total: {totals['net']}")
        self.stdout.write(f"  orders total:      {totals['orders']}")

        # All records (any date)
        all_totals = FactOrdersDaily.objects.aggregate(
            new=Sum("new_revenue"),
            net=Sum("net_revenue"),
            rows=Count("id"),
        )
        self.stdout.write(f"\nAll FactOrdersDaily (any date): {all_totals['rows']} rows")
        self.stdout.write(f"  new_revenue total: {all_totals['new']}")
        self.stdout.write(f"  net_revenue total: {all_totals['net']}")

        # Date range of records in DB
        from django.db.models import Min, Max
        date_range = FactOrdersDaily.objects.aggregate(
            min_date=Min("date__date"),
            max_date=Max("date__date"),
        )
        self.stdout.write(f"  date range: {date_range['min_date']} to {date_range['max_date']}")

        # Per-brand breakdown for the period (top 10 by new_revenue)
        self.stdout.write(f"\n=== Top brands by new_revenue in period ===")
        per_brand = (
            qs.values("brand__name", "brand__brand_id", "brand_id")
            .annotate(new=Sum("new_revenue"), net=Sum("net_revenue"))
            .order_by("-new")[:10]
        )
        for row in per_brand:
            self.stdout.write(
                f"  {row['brand__name']} (brand_id={row['brand__brand_id']}, pk={row['brand_id']}): "
                f"new={row['new']}  net={row['net']}"
            )

        # Now check brand_table output
        self.stdout.write(f"\n=== brand_table(rev_type='new') top brands ===")
        rows = services.brand_table(period, rev_type="new")
        rows.sort(key=lambda r: r.get("revenue", 0), reverse=True)
        for r in rows[:10]:
            self.stdout.write(
                f"  {r['name']} (pk={r['id']}): revenue={r.get('revenue', 0)}  "
                f"spend={r.get('spend', 0)}"
            )

        self.stdout.write("")
