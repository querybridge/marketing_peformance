from django import template

from dashboard.services import ALERT_META

register = template.Library()


@register.filter
def currency(value):
    """$1,234.56"""
    try:
        v = float(value)
        if abs(v) >= 1_000_000:
            return f"${v / 1_000_000:,.1f}M"
        if abs(v) >= 10_000:
            return f"${v / 1_000:,.1f}K"
        return f"${v:,.2f}"
    except (ValueError, TypeError):
        return "—"


@register.filter
def intcomma(value):
    """1,234"""
    try:
        return f"{int(value):,}"
    except (ValueError, TypeError):
        return "—"


@register.filter
def pct(value):
    """+12.3% or −4.5%"""
    try:
        v = float(value) * 100
        sign = "+" if v > 0 else ""
        return f"{sign}{v:.1f}%"
    except (ValueError, TypeError):
        return "—"


@register.filter
def mts_fmt(value):
    """Format MTS ratio as percentage: 0.2345 → 23.5%"""
    try:
        return f"{float(value) * 100:.1f}%"
    except (ValueError, TypeError):
        return "—"


@register.filter
def bps(value):
    """Format MTS delta as basis points: 0.0050 → +50 bps"""
    try:
        v = float(value) * 10000
        sign = "+" if v > 0 else ""
        return f"{sign}{v:.0f} bps"
    except (ValueError, TypeError):
        return "—"


@register.filter
def delta_class(value):
    """CSS class for delta coloring."""
    try:
        v = float(value)
        if v > 0.001:
            return "delta-up"
        if v < -0.001:
            return "delta-down"
        return "delta-flat"
    except (ValueError, TypeError):
        return "delta-flat"


@register.filter
def mts_delta_class(value):
    """For MTS, lower is better — so negative delta is good."""
    try:
        v = float(value)
        if v < -0.001:
            return "delta-up"
        if v > 0.001:
            return "delta-down"
        return "delta-flat"
    except (ValueError, TypeError):
        return "delta-flat"


@register.filter
def good_up(value):
    """Higher is better — positive = good (green), negative = bad (red).
    Use for: revenue delta, revenue vs budget."""
    try:
        v = float(value)
        if v > 0.001:
            return "delta-good"
        if v < -0.001:
            return "delta-bad"
        return "delta-flat"
    except (ValueError, TypeError):
        return "delta-flat"


@register.filter
def good_down(value):
    """Lower is better — negative = good (green), positive = bad (red).
    Use for: spend delta, MTS delta."""
    try:
        v = float(value)
        if v < -0.001:
            return "delta-good"
        if v > 0.001:
            return "delta-bad"
        return "delta-flat"
    except (ValueError, TypeError):
        return "delta-flat"


@register.filter
def mts_vs_goal_class(value):
    """MTS vs budget: within 20 bps = green, within 50 bps = orange, else red.
    Value is a ratio delta (e.g. 0.0030 = 30 bps)."""
    try:
        bps = abs(float(value)) * 10000
        if bps <= 20:
            return "delta-good"
        if bps <= 50:
            return "delta-warn"
        return "delta-bad"
    except (ValueError, TypeError):
        return "delta-flat"


@register.filter
def alert_label(key):
    """Alert key → short badge label."""
    return ALERT_META.get(key, {}).get("label", key)


@register.filter
def alert_icon(key):
    """Alert key → Semantic UI icon class."""
    return ALERT_META.get(key, {}).get("icon", "question circle")


@register.filter
def alert_color(key):
    """Alert key → Semantic UI color class."""
    return ALERT_META.get(key, {}).get("color", "grey")


@register.filter
def alert_tooltip(key):
    """Alert key → one-sentence tooltip."""
    return ALERT_META.get(key, {}).get("tooltip", "")
