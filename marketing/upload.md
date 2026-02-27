# CSV Upload via SSH

Workaround for importing revenue and campaign data when the web upload returns an internal server error.

## Prerequisites

- SSH access to the server
- CSV file ready on your local machine

## Step 1: Copy the file to the server

```bash
scp your_file.csv querybridge@<server-ip>:/Users/querybridge/envs/belamibvm/marketing/uploads/
```

You can upload multiple files at once:

```bash
scp revenue.csv google_ads.csv querybridge@<server-ip>:/Users/querybridge/envs/belamibvm/marketing/uploads/
```

## Step 2: SSH into the server

```bash
ssh querybridge@<server-ip>
```

## Step 3: Run the import tool

```bash
cd /Users/querybridge/envs/belamibvm/marketing
python manage.py import_data
```

## Step 4: Follow the prompts

1. **Pick a file** — the tool lists all `.csv` files in the `uploads/` folder
2. **Select data type:**
   - Revenue CSV (orders, net sales, new sales)
   - Campaign CSV (Google Ads)
   - Campaign CSV (Bing Ads)
3. **For campaign files only** — select the vertical from the list
4. The tool runs the import and prints a summary
5. The file is automatically deleted after a successful import

## CSV Format Reference

### Revenue CSV

Required columns (case-insensitive):

| Column | Example |
|--------|---------|
| site_id | 101 |
| mfg id | 42 |
| Date | 2026-02-15 or 2/15/2026 |
| Orders | 5 |
| Net Sales | $1,234.56 |
| NewSales | $987.65 |

- `site_id` must match an existing site-to-vertical mapping in the database
- `mfg id` must match a brand's `brand_id` within the vertical
- Parenthetical negatives like `($309.00)` are supported

### Campaign CSV — Google Ads

Required columns:

| Column | Example |
|--------|---------|
| Campaign | Brand - Standard PLA |
| Campaign ID | 12345678 |
| Ad group | 42; Brand Name - Product |
| Campaign type | Shopping |
| Day | 2026-02-15 |
| Impr. | 1,200 |
| Clicks | 85 |
| Cost | $125.50 |
| Conversions | 3 |
| Conv. value | $450.00 |

Optional: `Search impr. share`, `Click share`

### Campaign CSV — Bing Ads

Required columns (accepts multiple name variants):

| Column | Accepted Names |
|--------|---------------|
| Campaign | Campaign name, Campaign |
| Campaign ID | Campaign ID |
| Ad group | Ad group, Ad group name |
| Campaign type | Ad distribution, Campaign type |
| Date | Time period, Date, Day |
| Impressions | Impressions |
| Clicks | Clicks |
| Cost | Spend, Cost |
| Conversions | Conversions |
| Conv. value | Revenue, Revenue (Conv.), Conv. value |

Optional: `Impression share %`, `Click share %`

## Troubleshooting

**"No CSV files found"** — Make sure you SCP'd the file to the `uploads/` folder, not the project root.

**"Missing required columns"** — Check that your CSV column headers match the expected names above. The tool auto-detects Google/Bing metadata rows before the header.

**"Unknown site IDs"** — The revenue CSV references site IDs that don't have a vertical mapping. Upload a site mapping first via the web UI at `/sites/upload/`.

**"Unknown mfg IDs"** — The `mfg id` in the revenue CSV doesn't match any brand's `brand_id` in that vertical. Those rows are skipped (not a hard failure).

**Import failed, file NOT deleted** — Fix the issue described in the error output and re-run. The file stays in `uploads/` so you can retry.
