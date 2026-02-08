"""
ReportLab PDF builder for the Weekly Performance Dashboard.

Generates an A4 landscape PDF with five sections:
  1. Header   — navy banner with title, date range, period label, pacing note
  2. Exceptions panel — colored alert badge row
  3. Brand table — 14-column table with totals, delta coloring, alert badges
  4. Trend charts — Revenue, Spend, MTS line plots
  5. Footer — generation timestamp + metric definitions
"""

import io
from datetime import datetime

from reportlab.graphics import renderPDF
from reportlab.graphics.charts.lineplots import LinePlot
from reportlab.graphics.shapes import Drawing, String
from reportlab.graphics.widgets.markers import makeMarker
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch, mm
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


# ─── Color palette ────────────────────────────────────────────────────────

NAVY = colors.HexColor("#1B1F3B")
LIGHT_GREY = colors.HexColor("#F5F5F5")
WHITE = colors.white
BLACK = colors.black
GREEN = colors.HexColor("#21BA45")
RED = colors.HexColor("#DB2828")
BLUE = colors.HexColor("#2185D0")
ORANGE = colors.HexColor("#F2711C")
PURPLE = colors.HexColor("#A333C8")
GREY = colors.HexColor("#767676")

ALERT_COLORS = {
    "red": RED,
    "green": GREEN,
    "blue": BLUE,
    "orange": ORANGE,
    "grey": GREY,
}

CHART_GREEN = colors.HexColor("#21BA45")
CHART_BLUE = colors.HexColor("#2185D0")
CHART_PURPLE = colors.HexColor("#A333C8")
CHART_GREEN_LIGHT = colors.HexColor("#A8E6CF")
CHART_BLUE_LIGHT = colors.HexColor("#A8D8EA")
CHART_PURPLE_LIGHT = colors.HexColor("#D5A6E6")


# ─── Formatting helpers ──────────────────────────────────────────────────

def _currency(v):
    """Format a number as currency: $1.2M, $12.3K, or $1.23."""
    try:
        v = float(v)
        if abs(v) >= 1_000_000:
            return f"${v / 1_000_000:,.1f}M"
        if abs(v) >= 10_000:
            return f"${v / 1_000:,.1f}K"
        return f"${v:,.2f}"
    except (ValueError, TypeError):
        return "\u2014"


def _pct(v):
    """Format a ratio as +12.3% or -4.5%."""
    try:
        v = float(v) * 100
        sign = "+" if v > 0 else ""
        return f"{sign}{v:.1f}%"
    except (ValueError, TypeError):
        return "\u2014"


def _mts(v):
    """Format MTS ratio as percentage: 0.2345 -> 23.5%."""
    try:
        return f"{float(v) * 100:.1f}%"
    except (ValueError, TypeError):
        return "\u2014"


def _bps(v):
    """Format MTS delta as basis points: 0.0050 -> +50 bps."""
    try:
        v = float(v) * 10000
        sign = "+" if v > 0 else ""
        return f"{sign}{v:.0f} bps"
    except (ValueError, TypeError):
        return "\u2014"


def _intcomma(v):
    """Format integer with commas: 1234 -> 1,234."""
    try:
        return f"{int(v):,}"
    except (ValueError, TypeError):
        return "\u2014"


def _delta_color(v, invert=False):
    """Return color for a delta value. invert=True for MTS (lower is better)."""
    try:
        v = float(v)
        if invert:
            v = -v
        if v > 0.001:
            return GREEN
        if v < -0.001:
            return RED
        return BLACK
    except (ValueError, TypeError):
        return BLACK


# ─── Styles ───────────────────────────────────────────────────────────────

_styles = getSampleStyleSheet()

STYLE_HEADER_TITLE = ParagraphStyle(
    "HeaderTitle",
    parent=_styles["Heading1"],
    fontSize=16,
    textColor=WHITE,
    spaceAfter=2,
    alignment=TA_LEFT,
)

STYLE_HEADER_SUB = ParagraphStyle(
    "HeaderSub",
    parent=_styles["Normal"],
    fontSize=9,
    textColor=colors.HexColor("#CCCCCC"),
    spaceAfter=0,
    alignment=TA_LEFT,
)

STYLE_SECTION = ParagraphStyle(
    "Section",
    parent=_styles["Heading2"],
    fontSize=11,
    textColor=NAVY,
    spaceBefore=10,
    spaceAfter=4,
)

STYLE_CELL = ParagraphStyle(
    "Cell",
    parent=_styles["Normal"],
    fontSize=7,
    leading=9,
    alignment=TA_RIGHT,
)

STYLE_CELL_LEFT = ParagraphStyle(
    "CellLeft",
    parent=STYLE_CELL,
    alignment=TA_LEFT,
)

STYLE_FOOTER = ParagraphStyle(
    "Footer",
    parent=_styles["Normal"],
    fontSize=7,
    textColor=GREY,
    spaceBefore=8,
)


# ─── Section builders ────────────────────────────────────────────────────

def _build_header(period_info):
    """Navy banner with title, date range, period label, pacing note."""
    title = period_info.get("title", "Weekly Performance Dashboard")
    date_range = period_info.get("date_range", "")
    period_label = period_info.get("period_label", "")
    pacing_note = period_info.get("pacing_note", "")

    header_data = [[
        Paragraph(title, STYLE_HEADER_TITLE),
    ], [
        Paragraph(
            f"{date_range} &nbsp;&nbsp;|&nbsp;&nbsp; {period_label}"
            + (f" &nbsp;&nbsp;|&nbsp;&nbsp; {pacing_note}" if pacing_note else ""),
            STYLE_HEADER_SUB,
        ),
    ]]

    t = Table(header_data, colWidths=[landscape(A4)[0] - 60])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), NAVY),
        ("TOPPADDING", (0, 0), (-1, 0), 10),
        ("BOTTOMPADDING", (0, -1), (-1, -1), 10),
        ("LEFTPADDING", (0, 0), (-1, -1), 14),
        ("RIGHTPADDING", (0, 0), (-1, -1), 14),
    ]))
    return t


def _build_exceptions(exceptions):
    """Colored badge row for alert counts."""
    if not exceptions:
        return Spacer(1, 0)

    badges = []
    for key, info in exceptions.items():
        label = info.get("label", key)
        count = info.get("count", 0)
        color_name = info.get("color", "grey")
        bg = ALERT_COLORS.get(color_name, GREY)

        style = ParagraphStyle(
            f"Badge_{key}",
            parent=_styles["Normal"],
            fontSize=8,
            textColor=WHITE,
            alignment=TA_CENTER,
        )
        badges.append(Paragraph(f"<b>{label}:</b> {count}", style))

    t = Table([badges], colWidths=[120] * len(badges))
    style_cmds = [
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]
    for i, (key, info) in enumerate(exceptions.items()):
        color_name = info.get("color", "grey")
        bg = ALERT_COLORS.get(color_name, GREY)
        style_cmds.append(("BACKGROUND", (i, 0), (i, 0), bg))

    t.setStyle(TableStyle(style_cmds))
    return t


def _alert_badges_text(alerts):
    """Render alert keys as a short comma-joined string."""
    from dashboard.services import ALERT_META
    if not alerts:
        return ""
    labels = [ALERT_META.get(a, {}).get("label", a) for a in alerts]
    return ", ".join(labels)


def _build_brand_table(brand_rows, totals):
    """Full 16-column brand performance table with totals row."""
    col_headers = [
        "Brand", "Vertical",
        "Spend", "Spend\u0394", "Revenue", "Rev\u0394",
        "Rev Budget", "vs Budget",
        "MTS", "MTS\u0394", "MTS Bgt",
        "Orders", "AOV", "Net CVR", "Mkt CVR", "Alerts",
    ]

    header_style = ParagraphStyle(
        "TH", parent=STYLE_CELL, fontSize=7, textColor=WHITE,
        alignment=TA_CENTER,
    )
    header_row = [Paragraph(f"<b>{h}</b>", header_style) for h in col_headers]

    data_rows = [header_row]
    for row in brand_rows:
        spend_delta_color = _delta_color(row.get("spend_delta"))
        rev_delta_color = _delta_color(row.get("revenue_delta"))
        budget_color = _delta_color(row.get("revenue_vs_budget"))
        mts_delta_color = _delta_color(row.get("mts_delta"), invert=True)

        def _c(val, fmt_fn, color=BLACK):
            """Cell paragraph with optional color."""
            text = fmt_fn(val)
            if color != BLACK:
                hex_c = color.hexval() if hasattr(color, 'hexval') else str(color)
                return Paragraph(
                    f'<font color="{hex_c}">{text}</font>', STYLE_CELL,
                )
            return Paragraph(text, STYLE_CELL)

        data_rows.append([
            Paragraph(str(row.get("name", "")), STYLE_CELL_LEFT),
            Paragraph(str(row.get("vertical", "")), STYLE_CELL_LEFT),
            _c(row.get("spend"), _currency),
            _c(row.get("spend_delta"), _pct, spend_delta_color),
            _c(row.get("revenue"), _currency),
            _c(row.get("revenue_delta"), _pct, rev_delta_color),
            _c(row.get("revenue_budget"), _currency),
            _c(row.get("revenue_vs_budget"), _pct, budget_color),
            _c(row.get("mts"), _mts),
            _c(row.get("mts_delta"), _bps, mts_delta_color),
            _c(row.get("mts_budget"), _mts),
            _c(row.get("orders"), _intcomma),
            _c(row.get("aov"), _currency),
            _c(row.get("net_cvr"), _mts),
            _c(row.get("mkt_cvr"), _mts),
            Paragraph(_alert_badges_text(row.get("alerts", [])), STYLE_CELL_LEFT),
        ])

    # Totals row
    if totals:
        totals_style = ParagraphStyle(
            "TotalsCell", parent=STYLE_CELL, fontSize=7,
        )
        totals_style_left = ParagraphStyle(
            "TotalsCellLeft", parent=STYLE_CELL_LEFT, fontSize=7,
        )
        data_rows.append([
            Paragraph("<b>TOTAL</b>", totals_style_left),
            Paragraph("", totals_style),
            Paragraph(f"<b>{_currency(totals.get('spend'))}</b>", totals_style),
            Paragraph(f"<b>{_pct(totals.get('spend_delta'))}</b>", totals_style),
            Paragraph(f"<b>{_currency(totals.get('revenue'))}</b>", totals_style),
            Paragraph(f"<b>{_pct(totals.get('revenue_delta'))}</b>", totals_style),
            Paragraph(f"<b>{_currency(totals.get('revenue_budget'))}</b>", totals_style),
            Paragraph(f"<b>{_pct(totals.get('revenue_vs_budget'))}</b>", totals_style),
            Paragraph(f"<b>{_mts(totals.get('mts'))}</b>", totals_style),
            Paragraph("", totals_style),
            Paragraph("", totals_style),
            Paragraph(f"<b>{_intcomma(totals.get('orders'))}</b>", totals_style),
            Paragraph(f"<b>{_currency(totals.get('aov'))}</b>", totals_style),
            Paragraph(f"<b>{_mts(totals.get('net_cvr'))}</b>", totals_style),
            Paragraph(f"<b>{_mts(totals.get('mkt_cvr'))}</b>", totals_style),
            Paragraph("", totals_style),
        ])

    # Column widths (total ~800 for landscape A4 minus margins)
    col_widths = [
        72, 52,   # Brand, Vertical
        50, 38,   # Spend, SpendΔ
        50, 38,   # Revenue, RevΔ
        50, 40,   # Rev Budget, vs Budget
        34, 38,   # MTS, MTSΔ
        34,        # MTS Bgt
        34, 40,   # Orders, AOV
        38, 38,   # Net CVR, Mkt CVR
        74,        # Alerts
    ]

    t = Table(data_rows, colWidths=col_widths, repeatRows=1)
    style_cmds = [
        # Header row
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
        ("FONTSIZE", (0, 0), (-1, 0), 7),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 5),
        ("TOPPADDING", (0, 0), (-1, 0), 5),
        # All cells
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 1), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 2),
        ("FONTSIZE", (0, 1), (-1, -1), 7),
        # Grid
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#DDDDDD")),
        # Alternating row backgrounds
    ]
    for i in range(1, len(data_rows)):
        if i % 2 == 0:
            style_cmds.append(
                ("BACKGROUND", (0, i), (-1, i), LIGHT_GREY)
            )

    # Totals row styling
    if totals:
        last = len(data_rows) - 1
        style_cmds.extend([
            ("BACKGROUND", (0, last), (-1, last), colors.HexColor("#E8E8E8")),
            ("LINEABOVE", (0, last), (-1, last), 1.5, NAVY),
        ])

    t.setStyle(TableStyle(style_cmds))
    return t


def _build_trend_charts(trend):
    """Three side-by-side line charts: Revenue, Spend, MTS."""
    if not trend or not trend.get("current"):
        return Spacer(1, 0)

    current = trend["current"]
    compare = trend.get("compare", [])

    chart_width = 235
    chart_height = 140
    charts = []

    # Prepare data series
    cur_revenue = [(i, d.get("revenue", 0)) for i, d in enumerate(current)]
    cur_spend = [(i, d.get("spend", 0)) for i, d in enumerate(current)]
    cur_mts = []
    for i, d in enumerate(current):
        rev = d.get("revenue", 0)
        spend = d.get("spend", 0)
        mts = spend / rev if rev else 0
        cur_mts.append((i, mts))

    cmp_revenue = [(i, d.get("revenue", 0)) for i, d in enumerate(compare)]
    cmp_spend = [(i, d.get("spend", 0)) for i, d in enumerate(compare)]
    cmp_mts = []
    for i, d in enumerate(compare):
        rev = d.get("revenue", 0)
        spend = d.get("spend", 0)
        mts = spend / rev if rev else 0
        cmp_mts.append((i, mts))

    # Date labels for x-axis
    date_labels = [d.get("day", "") for d in current]

    chart_specs = [
        ("Revenue", cur_revenue, cmp_revenue, CHART_GREEN, CHART_GREEN_LIGHT, False),
        ("Spend", cur_spend, cmp_spend, CHART_BLUE, CHART_BLUE_LIGHT, False),
        ("MTS (Cost/Rev)", cur_mts, cmp_mts, CHART_PURPLE, CHART_PURPLE_LIGHT, True),
    ]

    for title, cur_data, cmp_data, main_color, cmp_color, is_pct in chart_specs:
        d = Drawing(chart_width, chart_height + 20)

        # Title
        d.add(String(chart_width / 2, chart_height + 8, title,
                      fontSize=8, fillColor=NAVY, textAnchor="middle"))

        lp = LinePlot()
        lp.x = 25
        lp.y = 15
        lp.width = chart_width - 45
        lp.height = chart_height - 25

        plot_data = [cur_data]
        if cmp_data:
            plot_data.append(cmp_data)

        lp.data = plot_data

        # Current period: solid line
        lp.lines[0].strokeColor = main_color
        lp.lines[0].strokeWidth = 1.5
        lp.lines[0].symbol = makeMarker("Circle")
        lp.lines[0].symbol.size = 2.5
        lp.lines[0].symbol.fillColor = main_color
        lp.lines[0].symbol.strokeColor = main_color

        # Comparison period: dashed line
        if cmp_data:
            lp.lines[1].strokeColor = cmp_color
            lp.lines[1].strokeWidth = 1
            lp.lines[1].strokeDashArray = [4, 2]
            lp.lines[1].symbol = makeMarker("Circle")
            lp.lines[1].symbol.size = 2
            lp.lines[1].symbol.fillColor = cmp_color
            lp.lines[1].symbol.strokeColor = cmp_color

        # X-axis
        lp.xValueAxis.labels.fontSize = 6
        lp.xValueAxis.labels.angle = 0
        lp.xValueAxis.visibleGrid = False

        if date_labels:
            lp.xValueAxis.valueMin = 0
            lp.xValueAxis.valueMax = max(len(date_labels) - 1, 1)

        # Y-axis
        lp.yValueAxis.labels.fontSize = 6
        lp.yValueAxis.visibleGrid = True
        lp.yValueAxis.gridStrokeColor = colors.HexColor("#EEEEEE")
        lp.yValueAxis.strokeColor = colors.HexColor("#CCCCCC")

        d.add(lp)
        charts.append(d)

    # Arrange charts side by side using a table
    t = Table([charts], colWidths=[chart_width] * len(charts))
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
    ]))
    return t


def _build_footer():
    """Generation timestamp and metric definitions."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"<b>Generated:</b> {now}",
        "<b>MTS</b> = Media-to-Sales (Cost \u00f7 Revenue) &nbsp;|&nbsp; "
        "<b>AOV</b> = Average Order Value (Revenue \u00f7 Orders) &nbsp;|&nbsp; "
        "<b>Net CVR</b> = Net Conversion Rate (Orders \u00f7 Clicks) &nbsp;|&nbsp; "
        "<b>Mkt CVR</b> = Marketing Conversion Rate (Platform Conversions \u00f7 Clicks)",
        "<b>\u0394</b> = Change vs comparison period &nbsp;|&nbsp; "
        "<b>bps</b> = Basis points (MTS delta \u00d7 10,000)",
    ]
    return Paragraph("<br/>".join(lines), STYLE_FOOTER)


# ─── Totals computation ──────────────────────────────────────────────────

def compute_totals(brand_rows):
    """Aggregate totals across all brand rows."""
    if not brand_rows:
        return {}

    spend = sum(r.get("spend", 0) or 0 for r in brand_rows)
    spend_cmp = sum(r.get("spend_cmp", 0) or 0 for r in brand_rows)
    revenue = sum(r.get("revenue", 0) or 0 for r in brand_rows)
    revenue_cmp = sum(r.get("revenue_cmp", 0) or 0 for r in brand_rows)
    revenue_budget = sum(r.get("revenue_budget", 0) or 0 for r in brand_rows)
    orders = sum(r.get("orders", 0) or 0 for r in brand_rows)
    clicks = sum(r.get("clicks", 0) or 0 for r in brand_rows)
    conversions = sum(r.get("conversions", 0) or 0 for r in brand_rows)

    def _safe_pct(cur, cmp):
        if cmp:
            return (cur - cmp) / cmp
        return None

    mts = spend / revenue if revenue else None

    return {
        "spend": spend,
        "spend_delta": _safe_pct(spend, spend_cmp),
        "revenue": revenue,
        "revenue_delta": _safe_pct(revenue, revenue_cmp),
        "revenue_budget": revenue_budget,
        "revenue_vs_budget": _safe_pct(revenue, revenue_budget) if revenue_budget else None,
        "mts": mts,
        "orders": orders,
        "aov": revenue / orders if orders else None,
        "net_cvr": orders / clicks if clicks else None,
        "mkt_cvr": conversions / clicks if clicks else None,
    }


# ─── Public API ───────────────────────────────────────────────────────────

def build_pdf(brand_rows, exceptions, trend, totals, period_info):
    """
    Build the complete Weekly Performance Dashboard PDF and return bytes.

    Parameters
    ----------
    brand_rows : list[dict]
        Output of services.brand_table().
    exceptions : dict
        Output of services.exceptions_summary().
    trend : dict
        Output of services.daily_trend(), with 'current' and 'compare' keys.
    totals : dict
        Aggregated totals from compute_totals().
    period_info : dict
        Keys: title, date_range, period_label, pacing_note.

    Returns
    -------
    bytes
        Complete PDF document.
    """
    buf = io.BytesIO()

    doc = SimpleDocTemplate(
        buf,
        pagesize=landscape(A4),
        leftMargin=20,
        rightMargin=20,
        topMargin=15,
        bottomMargin=15,
        title="Weekly Performance Dashboard",
    )

    flowables = []

    # 1. Header
    flowables.append(_build_header(period_info))
    flowables.append(Spacer(1, 6))

    # 2. Exceptions panel
    flowables.append(Paragraph("Exceptions", STYLE_SECTION))
    flowables.append(_build_exceptions(exceptions))
    flowables.append(Spacer(1, 6))

    # 3. Brand table
    flowables.append(Paragraph("Brand Performance", STYLE_SECTION))
    flowables.append(_build_brand_table(brand_rows, totals))
    flowables.append(Spacer(1, 10))

    # 4. Trend charts
    flowables.append(Paragraph("Trend", STYLE_SECTION))
    flowables.append(_build_trend_charts(trend))
    flowables.append(Spacer(1, 6))

    # 5. Footer
    flowables.append(_build_footer())

    doc.build(flowables)
    return buf.getvalue()
