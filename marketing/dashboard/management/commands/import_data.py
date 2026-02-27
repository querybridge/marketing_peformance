"""
Interactive CLI tool for importing revenue and campaign CSV files.

Workaround for SCP/SSH workflows when the web upload returns a server error.
Place CSV files in the ``uploads/`` folder and run::

    python manage.py import_data

The command lists available files, lets you choose one, and walks you through
vertical/source selection (for campaign data).  After a successful import the
file is deleted automatically.
"""

import csv
import io
import os
import re
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.core.management.base import BaseCommand
from django.utils.text import slugify

from dashboard.models import (
    DimBrand,
    DimCampaign,
    DimCampaignType,
    DimDate,
    DimSite,
    DimSource,
    DimVertical,
    FactMediaDaily,
    FactOrdersDaily,
)

# Re-use the same column maps and helpers that the web upload uses.
from dashboard.views import (
    COLUMN_MAPS,
    REVENUE_REQUIRED_COLUMNS,
    _BRAND_ID_RE,
    _GENERIC_NAMES,
    _build_column_index,
    _detect_header_row,
    _parse_csv_date,
    _parse_currency,
    _parse_decimal,
    _parse_int,
    _parse_revenue_date,
    _parse_share,
    _resolve_brand_id_from_name,
)

UPLOADS_DIR = Path(__file__).resolve().parents[3] / "uploads"


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────

def _pick(prompt_text, options, allow_zero=False):
    """Display a numbered menu and return the chosen option."""
    for i, (label, _) in enumerate(options, 1):
        print(f"  {i}) {label}")
    if allow_zero:
        print("  0) Cancel")
    while True:
        raw = input(f"\n{prompt_text}: ").strip()
        try:
            choice = int(raw)
        except ValueError:
            print("  Please enter a number.")
            continue
        if allow_zero and choice == 0:
            return None
        if 1 <= choice <= len(options):
            return options[choice - 1][1]
        print(f"  Choose 1–{len(options)}{' (or 0 to cancel)' if allow_zero else ''}.")


def _confirm(msg):
    return input(f"{msg} [y/N] ").strip().lower() in ("y", "yes")


# ──────────────────────────────────────────────────────────────────────────
# Revenue import
# ──────────────────────────────────────────────────────────────────────────

def import_revenue(filepath, stdout):
    """Import a revenue CSV into FactOrdersDaily."""
    if DimSite.objects.count() == 0:
        stdout.write("ERROR: No site-to-vertical mappings found. Upload site mappings first.\n")
        return False

    raw = filepath.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")

    lines = text.splitlines()
    if not lines:
        stdout.write("ERROR: CSV file is empty.\n")
        return False

    reader = csv.reader(io.StringIO(text))
    raw_headers = next(reader)
    headers = [h.strip().lower() for h in raw_headers]

    missing = [col for col in REVENUE_REQUIRED_COLUMNS if col not in headers]
    if missing:
        stdout.write(f"ERROR: Missing required columns: {', '.join(missing)}\n")
        stdout.write(f"  Found: {', '.join(raw_headers)}\n")
        return False

    col_idx = {col: headers.index(col) for col in REVENUE_REQUIRED_COLUMNS}

    site_lookup = {
        s.site_id: s.vertical
        for s in DimSite.objects.select_related("vertical")
    }

    # First pass — parse and validate
    rows_data = []
    row_errors = []
    unknown_site_ids = set()

    for line_num, row in enumerate(reader, start=2):
        if not any(cell.strip() for cell in row):
            continue

        site_id_raw = row[col_idx["site_id"]].strip()
        mfg_id_raw = row[col_idx["mfg id"]].strip()
        date_raw = row[col_idx["date"]].strip()
        orders_raw = row[col_idx["orders"]].strip()
        net_raw = row[col_idx["net sales"]].strip()
        new_raw = row[col_idx["newsales"]].strip()

        if not site_id_raw:
            row_errors.append(f"Row {line_num}: missing site_id.")
            continue
        try:
            site_id_val = int(site_id_raw)
        except (ValueError, TypeError):
            row_errors.append(f"Row {line_num}: invalid site_id '{site_id_raw}'.")
            continue

        if site_id_val not in site_lookup:
            unknown_site_ids.add(site_id_val)
            continue

        if not mfg_id_raw:
            row_errors.append(f"Row {line_num}: missing mfg id.")
            continue
        try:
            mfg_id_val = int(mfg_id_raw)
        except (ValueError, TypeError):
            row_errors.append(f"Row {line_num}: invalid mfg id '{mfg_id_raw}'.")
            continue

        date_val = _parse_revenue_date(date_raw)
        if not date_val:
            row_errors.append(f"Row {line_num}: invalid date '{date_raw}'.")
            continue

        if orders_raw.lower() == "(blank)":
            orders_raw = "0"
        try:
            orders_val = int(orders_raw.replace(",", ""))
        except (ValueError, TypeError):
            row_errors.append(f"Row {line_num}: invalid orders '{orders_raw}'.")
            continue

        try:
            net_val = _parse_currency(net_raw)
        except (InvalidOperation, ValueError, TypeError):
            row_errors.append(f"Row {line_num}: invalid Net Sales '{net_raw}'.")
            continue

        try:
            new_val = _parse_currency(new_raw)
        except (InvalidOperation, ValueError, TypeError):
            row_errors.append(f"Row {line_num}: invalid NewSales '{new_raw}'.")
            continue

        rows_data.append({
            "line": line_num,
            "site_id": site_id_val,
            "mfg_id": mfg_id_val,
            "date": date_val,
            "orders": orders_val,
            "net_revenue": net_val,
            "new_revenue": new_val,
        })

    if unknown_site_ids:
        stdout.write(
            f"ERROR: Unknown site IDs (upload site mapping first): "
            f"{', '.join(str(s) for s in sorted(unknown_site_ids))}\n"
        )
        return False

    # Brand lookup
    brand_cache = {}
    for b in DimBrand.objects.filter(brand_id__isnull=False).select_related("vertical"):
        if b.vertical_id:
            brand_cache[(b.vertical_id, b.brand_id)] = b

    # Aggregate by (brand, date)
    pending = {}
    skipped = 0
    unknown_mfg_ids = set()
    skipped_new_revenue = Decimal("0")
    skipped_net_revenue = Decimal("0")

    for r in rows_data:
        vertical = site_lookup[r["site_id"]]
        cache_key = (vertical.id, r["mfg_id"])
        brand = brand_cache.get(cache_key)
        if not brand:
            unknown_mfg_ids.add(r["mfg_id"])
            skipped_new_revenue += r["new_revenue"]
            skipped_net_revenue += r["net_revenue"]
            continue

        dim_date = DimDate.objects.filter(date=r["date"]).first()
        if not dim_date:
            row_errors.append(f"Row {r['line']}: date {r['date']} not in calendar — skipped.")
            skipped += 1
            continue

        agg_key = (brand.id, dim_date.id)
        if agg_key in pending:
            pending[agg_key]["orders"] += r["orders"]
            pending[agg_key]["net_revenue"] += r["net_revenue"]
            pending[agg_key]["new_revenue"] += r["new_revenue"]
        else:
            pending[agg_key] = {
                "brand": brand,
                "dim_date": dim_date,
                "orders": r["orders"],
                "net_revenue": r["net_revenue"],
                "new_revenue": r["new_revenue"],
            }

    # Write to DB
    created = 0
    updated = 0
    for agg in pending.values():
        _, is_new = FactOrdersDaily.objects.update_or_create(
            brand=agg["brand"],
            date=agg["dim_date"],
            defaults={
                "orders": agg["orders"],
                "net_revenue": agg["net_revenue"],
                "new_revenue": agg["new_revenue"],
            },
        )
        if is_new:
            created += 1
        else:
            updated += 1

    # Summary
    written_new = sum(a["new_revenue"] for a in pending.values())
    written_net = sum(a["net_revenue"] for a in pending.values())

    stdout.write("\n--- Revenue Import Summary ---\n")
    stdout.write(f"  CSV rows parsed : {len(rows_data)}\n")
    stdout.write(f"  DB rows created : {created}\n")
    stdout.write(f"  DB rows updated : {updated}\n")
    stdout.write(f"  Written NewSales: ${written_new:,.2f}\n")
    stdout.write(f"  Written Net Sales: ${written_net:,.2f}\n")
    if unknown_mfg_ids:
        stdout.write(
            f"  Unknown mfg IDs (skipped): {', '.join(str(m) for m in sorted(unknown_mfg_ids))}\n"
        )
        stdout.write(f"  Skipped NewSales: ${skipped_new_revenue:,.2f}\n")
        stdout.write(f"  Skipped Net Sales: ${skipped_net_revenue:,.2f}\n")
    if row_errors:
        stdout.write(f"  Warnings ({len(row_errors)}):\n")
        for e in row_errors[:20]:
            stdout.write(f"    - {e}\n")
        if len(row_errors) > 20:
            stdout.write(f"    ... and {len(row_errors) - 20} more\n")

    return True


# ──────────────────────────────────────────────────────────────────────────
# Campaign / media import
# ──────────────────────────────────────────────────────────────────────────

def import_campaign(filepath, vertical, source, stdout):
    """Import a campaign/media CSV into FactMediaDaily."""
    raw = filepath.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")

    all_lines = text.splitlines()
    if not all_lines:
        stdout.write("ERROR: CSV file is empty.\n")
        return False

    lookup_slug = source.slug

    # Detect header row (handles Google Ads / Bing Ads metadata rows)
    header_row_idx, headers = _detect_header_row(all_lines, lookup_slug)
    metadata_rows_skipped = 0

    if header_row_idx is not None:
        metadata_rows_skipped = header_row_idx
        data_lines = all_lines[header_row_idx + 1:]
    else:
        parsed_first = list(csv.reader([all_lines[0]]))
        headers = parsed_first[0] if parsed_first else []
        data_lines = all_lines[1:]

    col_idx = _build_column_index(headers, lookup_slug)

    # Validate required columns
    requires_clicks = source.slug in ("meta-ads", "amazon-marketplace", "bing-ads")
    required = ["campaign", "date", "conversions", "conversion_value"]
    required.append("cost")
    if requires_clicks:
        required.append("clicks")
    missing = [req for req in required if req not in col_idx]
    if missing:
        hdr_note = ""
        if header_row_idx is not None:
            hdr_note = f" (detected header on row {header_row_idx + 1})"
        stdout.write(
            f"ERROR: Missing required columns: {', '.join(missing)}.{hdr_note}\n"
            f"  Found headers: {', '.join(headers)}\n"
        )
        return False

    if metadata_rows_skipped:
        stdout.write(f"  Skipped {metadata_rows_skipped} metadata row(s) before header.\n")

    reader = csv.reader(io.StringIO("\n".join(data_lines)))

    # Pre-load campaign lookups
    campaigns_by_ext_id = {}
    for c in DimCampaign.objects.filter(source=source).exclude(external_id=""):
        campaigns_by_ext_id[c.external_id] = c

    campaigns_by_name_brand = {}
    campaigns_by_name = {}
    for c in (DimCampaign.objects
              .filter(source=source, brand__vertical=vertical)
              .select_related("brand")):
        campaigns_by_name_brand[(c.name.lower(), c.brand_id)] = c
        campaigns_by_name[c.name.lower()] = c

    types_by_name = {ct.name.lower(): ct for ct in DimCampaignType.objects.all()}

    unknown_brand, _ = DimBrand.objects.get_or_create(
        slug="unknown", vertical=vertical,
        defaults={"name": "Unknown"},
    )

    # Process rows
    created = 0
    updated = 0
    campaigns_created = 0
    types_created = 0
    pending_rows = {}
    has_ad_group_col = "ad_group" in col_idx
    errors = []

    def _cell(row, field):
        idx = col_idx.get(field)
        if idx is None or idx >= len(row):
            return None
        return row[idx]

    data_start_line = metadata_rows_skipped + 2
    for row_num, row in enumerate(reader, start=data_start_line):
        if not any(cell.strip() for cell in row):
            continue

        first_cell = row[0].strip() if row else ""
        if first_cell.lower().startswith(("total", "\u00a9", "(c)")):
            continue

        campaign_name = (_cell(row, "campaign") or "").strip()
        ext_id = (_cell(row, "external_id") or "").strip()
        ad_group_name = (_cell(row, "ad_group") or "").strip()

        if not campaign_name:
            errors.append(f"Row {row_num}: missing campaign name, skipped.")
            continue

        # Resolve brand (ad group takes precedence)
        row_brand = None
        matched_from = "unknown"

        if ad_group_name:
            row_brand = _resolve_brand_id_from_name(ad_group_name, vertical)
            if row_brand:
                matched_from = "ad_group"

        if not row_brand:
            row_brand = _resolve_brand_id_from_name(campaign_name, vertical)
            if row_brand:
                matched_from = "campaign"

        # Campaign lookup — brand-aware
        campaign = None
        name_lower = campaign_name.lower()

        if row_brand:
            key = (name_lower, row_brand.id)
            if key in campaigns_by_name_brand:
                campaign = campaigns_by_name_brand[key]
            elif ext_id and ext_id in campaigns_by_ext_id:
                existing = campaigns_by_ext_id[ext_id]
                if existing.brand_id == row_brand.id:
                    campaign = existing
        else:
            if ext_id and ext_id in campaigns_by_ext_id:
                campaign = campaigns_by_ext_id[ext_id]
            elif name_lower in campaigns_by_name:
                campaign = campaigns_by_name[name_lower]

        if campaign and campaign.source_id != source.id:
            campaign = None

        if campaign:
            update_fields = []
            if ad_group_name and campaign.ad_group_name != ad_group_name:
                campaign.ad_group_name = ad_group_name
                update_fields.append("ad_group_name")
            if row_brand and campaign.brand_id == unknown_brand.id:
                campaign.brand = row_brand
                campaign.matched_from = matched_from
                update_fields.extend(["brand_id", "matched_from"])
            if update_fields:
                campaign.save(update_fields=update_fields)

        if not campaign:
            type_val = (_cell(row, "campaign_type") or "").strip()
            if type_val and type_val.lower() in types_by_name:
                ctype = types_by_name[type_val.lower()]
            elif type_val:
                ctype = DimCampaignType.objects.create(
                    name=type_val, slug=slugify(type_val),
                )
                types_by_name[type_val.lower()] = ctype
                types_created += 1
            else:
                fallback = "uncategorized"
                if fallback in types_by_name:
                    ctype = types_by_name[fallback]
                else:
                    ctype = DimCampaignType.objects.create(
                        name="Uncategorized", slug="uncategorized",
                    )
                    types_by_name[fallback] = ctype
                    types_created += 1

            brand_for_campaign = row_brand or unknown_brand

            use_ext_id = ext_id
            if ext_id and ext_id in campaigns_by_ext_id:
                use_ext_id = ""

            campaign = DimCampaign.objects.create(
                name=campaign_name,
                external_id=use_ext_id,
                brand=brand_for_campaign,
                source=source,
                campaign_type=ctype,
                ad_group_name=ad_group_name,
                matched_from=matched_from,
            )
            if use_ext_id:
                campaigns_by_ext_id[use_ext_id] = campaign
            campaigns_by_name_brand[(name_lower, brand_for_campaign.id)] = campaign
            campaigns_by_name[name_lower] = campaign
            campaigns_created += 1

        # Parse date
        date_val = _parse_csv_date(_cell(row, "date"))
        if not date_val:
            errors.append(f"Row {row_num}: invalid or missing date, skipped.")
            continue

        dim_date = DimDate.objects.filter(date=date_val).first()
        if not dim_date:
            errors.append(f"Row {row_num}: date {date_val} not in calendar, skipped.")
            continue

        # Parse metrics
        impressions = _parse_int(_cell(row, "impressions"))
        clicks = _parse_int(_cell(row, "clicks"))
        cost = _parse_decimal(_cell(row, "cost"))
        conversions = _parse_int(_cell(row, "conversions"))
        conversion_value = _parse_decimal(_cell(row, "conversion_value"))
        impression_share = _parse_share(_cell(row, "impression_share"))
        click_share = _parse_share(_cell(row, "click_share"))

        key = (campaign.id, dim_date.id, ad_group_name)
        if key in pending_rows:
            agg = pending_rows[key]
            agg["impressions"] += impressions
            agg["clicks"] += clicks
            agg["cost"] += cost
            agg["conversions"] += conversions
            agg["conversion_value"] += conversion_value
            if impressions > agg["_max_impr"]:
                agg["_max_impr"] = impressions
                agg["impression_share"] = impression_share
                agg["click_share"] = click_share
        else:
            pending_rows[key] = {
                "campaign": campaign,
                "date": dim_date,
                "ad_group_name": ad_group_name,
                "impressions": impressions,
                "clicks": clicks,
                "cost": cost,
                "conversions": conversions,
                "conversion_value": conversion_value,
                "impression_share": impression_share,
                "click_share": click_share,
                "_max_impr": impressions,
            }

    # Skip campaign-level rows that would duplicate existing ad-group data
    skipped_has_ag = 0
    if not has_ad_group_col:
        camp_date_keys = {
            (agg["campaign"].id, agg["date"].id) for agg in pending_rows.values()
        }
        if camp_date_keys:
            from django.db.models import Q

            existing_ag = set()
            keys_list = list(camp_date_keys)
            CHUNK = 500
            for i in range(0, len(keys_list), CHUNK):
                q = Q()
                for cid, did in keys_list[i:i + CHUNK]:
                    q |= Q(campaign_id=cid, date_id=did)
                existing_ag.update(
                    FactMediaDaily.objects.filter(q)
                    .exclude(ad_group_name="")
                    .values_list("campaign_id", "date_id")
                    .distinct()
                )
            if existing_ag:
                keys_to_drop = [
                    k for k, agg in pending_rows.items()
                    if (agg["campaign"].id, agg["date"].id) in existing_ag
                ]
                for k in keys_to_drop:
                    del pending_rows[k]
                skipped_has_ag = len(keys_to_drop)

    # Write to DB
    for agg in pending_rows.values():
        _, is_created = FactMediaDaily.objects.update_or_create(
            campaign=agg["campaign"],
            date=agg["date"],
            ad_group_name=agg["ad_group_name"],
            defaults={
                "impressions": agg["impressions"],
                "clicks": agg["clicks"],
                "cost": agg["cost"],
                "conversions": agg["conversions"],
                "conversion_value": agg["conversion_value"],
                "impression_share": agg["impression_share"],
                "click_share": agg["click_share"],
            },
        )
        if is_created:
            created += 1
        else:
            updated += 1

    # Clean up stale campaign-level rows superseded by ad-group data
    camp_dates_with_ag = defaultdict(set)
    for agg in pending_rows.values():
        if agg["ad_group_name"]:
            camp_dates_with_ag[agg["campaign"].id].add(agg["date"].id)
    for camp_id, date_ids in camp_dates_with_ag.items():
        FactMediaDaily.objects.filter(
            campaign_id=camp_id, date_id__in=date_ids, ad_group_name="",
        ).delete()

    # Summary
    ad_group_rows = sum(1 for a in pending_rows.values() if a["ad_group_name"])
    total_cost = sum(float(a["cost"]) for a in pending_rows.values())
    total_conv_value = sum(float(a["conversion_value"]) for a in pending_rows.values())

    stdout.write("\n--- Campaign Import Summary ---\n")
    stdout.write(f"  DB rows created    : {created}\n")
    stdout.write(f"  DB rows updated    : {updated}\n")
    stdout.write(f"  Campaigns created  : {campaigns_created}\n")
    if types_created:
        stdout.write(f"  Types created      : {types_created}\n")
    stdout.write(f"  Ad-group rows      : {ad_group_rows}\n")
    stdout.write(f"  Total cost         : ${total_cost:,.2f}\n")
    stdout.write(f"  Total conv. value  : ${total_conv_value:,.2f}\n")
    if skipped_has_ag:
        stdout.write(f"  Skipped (has AG)   : {skipped_has_ag}\n")
    if has_ad_group_col:
        stdout.write(f"  Ad group column    : detected\n")
    if errors:
        stdout.write(f"  Warnings ({len(errors)}):\n")
        for e in errors[:20]:
            stdout.write(f"    - {e}\n")
        if len(errors) > 20:
            stdout.write(f"    ... and {len(errors) - 20} more\n")

    return True


# ──────────────────────────────────────────────────────────────────────────
# Management command
# ──────────────────────────────────────────────────────────────────────────

class Command(BaseCommand):
    help = "Import revenue or campaign CSV files from the uploads/ folder."

    def handle(self, *args, **options):
        self.stdout.write("\n=== Momentum Data Import Tool ===\n")

        # Ensure uploads dir exists
        UPLOADS_DIR.mkdir(exist_ok=True)

        # List CSV files
        csv_files = sorted(
            f for f in UPLOADS_DIR.iterdir()
            if f.is_file() and f.suffix.lower() == ".csv"
        )

        if not csv_files:
            self.stdout.write(f"No CSV files found in {UPLOADS_DIR}/\n")
            self.stdout.write("SCP your files there and re-run this command.\n")
            return

        self.stdout.write(f"\nCSV files in {UPLOADS_DIR}/:\n")
        file_options = [(f.name, f) for f in csv_files]
        chosen_file = _pick("Select a file", file_options, allow_zero=True)
        if not chosen_file:
            self.stdout.write("Cancelled.\n")
            return

        self.stdout.write(f"\nSelected: {chosen_file.name}\n")

        # Ask file type
        self.stdout.write("\nWhat type of data is this?\n")
        file_type = _pick("Select type", [
            ("Revenue CSV (orders, net sales, new sales)", "revenue"),
            ("Campaign CSV (Google Ads)", "campaign_google"),
            ("Campaign CSV (Bing Ads)", "campaign_bing"),
        ], allow_zero=True)

        if not file_type:
            self.stdout.write("Cancelled.\n")
            return

        if file_type == "revenue":
            self._handle_revenue(chosen_file)
        else:
            source_slug = "google-ads" if file_type == "campaign_google" else "bing-ads"
            self._handle_campaign(chosen_file, source_slug)

    def _handle_revenue(self, filepath):
        self.stdout.write(f"\nImporting revenue data from {filepath.name}...\n")
        success = import_revenue(filepath, self.stdout)
        if success:
            filepath.unlink()
            self.stdout.write(f"\n  File deleted: {filepath.name}\n")
            self.stdout.write("  Done!\n")
        else:
            self.stdout.write("\n  Import failed. File was NOT deleted.\n")

    def _handle_campaign(self, filepath, source_slug):
        # Pick vertical
        verticals = list(DimVertical.objects.order_by("name"))
        if not verticals:
            self.stdout.write("ERROR: No verticals found in the database.\n")
            return

        self.stdout.write("\nSelect vertical:\n")
        vert_options = [(v.name, v) for v in verticals]
        vertical = _pick("Vertical", vert_options, allow_zero=True)
        if not vertical:
            self.stdout.write("Cancelled.\n")
            return

        source = DimSource.objects.filter(slug=source_slug).first()
        if not source:
            self.stdout.write(f"ERROR: Source '{source_slug}' not found in the database.\n")
            return

        self.stdout.write(
            f"\nImporting {source.name} campaign data for {vertical.name} "
            f"from {filepath.name}...\n"
        )

        success = import_campaign(filepath, vertical, source, self.stdout)
        if success:
            filepath.unlink()
            self.stdout.write(f"\n  File deleted: {filepath.name}\n")
            self.stdout.write("  Done!\n")
        else:
            self.stdout.write("\n  Import failed. File was NOT deleted.\n")
