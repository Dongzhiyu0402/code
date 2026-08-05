"""Pure-SVG alert trend renderer (v0.10.0, P0-2 / D5).

A zero-dependency, string-built SVG so the SPA can embed the chart with
``innerHTML`` — no chart library is required.  The data source is always
:meth:`cfgdrift.storage.store.Store.alert_trend` (same aggregation as the
event table), so the chart and the events list can never disagree.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

_WIDTH = 700
_HEIGHT = 180
_PLOT_LEFT = 45
_PLOT_RIGHT = 690
_PLOT_TOP = 12
_PLOT_BOTTOM = 148
_PLOT_WIDTH = _PLOT_RIGHT - _PLOT_LEFT
_PLOT_HEIGHT = _PLOT_BOTTOM - _PLOT_TOP

_COLOR_SENT = "#3b82f6"      # blue
_COLOR_FAILED = "#ef4444"    # red
_COLOR_GRID = "#334155"
_COLOR_TEXT = "#94a3b8"


def _fmt_mmdd(date: str) -> str:
    """``2026-08-04`` -> ``08-04`` (dates already are ``YYYY-MM-DD``)."""
    parts = date.split("-")
    if len(parts) == 3:
        return "%s-%s" % (parts[1], parts[2])
    return date


def render_trend_svg(
    days: List[Dict[str, Any]], rule: Optional[str] = None
) -> str:
    """Render the 14-day (or custom window) stacked-bar SVG.

    ``days`` is the ``alert_trend`` ``days`` list (each entry
    ``{"date", "sent", "failed"}``).  Bars are stacked (failed at the
    bottom, sent on top) so the total height = sent + failed per day.
    All-zero / empty input renders a minimal empty-state SVG instead of
    raising.
    """
    total = sum(d.get("sent", 0) + d.get("failed", 0) for d in days)
    if not days or total == 0:
        return (
            '<svg width="%d" height="%d" viewBox="0 0 %d %d" '
            'xmlns="http://www.w3.org/2000/svg">'
            '<text x="50%%" y="50%%" text-anchor="middle" '
            'fill="%s" font-size="14">暂无告警事件</text>'
            "</svg>"
            % (_WIDTH, _HEIGHT, _WIDTH, _HEIGHT, _COLOR_TEXT)
        )

    n = len(days)
    max_val = max(
        (d.get("sent", 0) + d.get("failed", 0)) for d in days
    )
    max_val = max(1, int(max_val))
    slot = _PLOT_WIDTH / n
    bar_width = max(2.0, slot * 0.62)

    parts: List[str] = [
        '<svg width="%d" height="%d" viewBox="0 0 %d %d" '
        'xmlns="http://www.w3.org/2000/svg">' % (_WIDTH, _HEIGHT, _WIDTH, _HEIGHT)
    ]

    # Horizontal gridlines (5 lines incl. the baseline) + y-axis labels.
    grid_steps = 4
    for i in range(grid_steps + 1):
        frac = i / grid_steps
        y = _PLOT_BOTTOM - frac * _PLOT_HEIGHT
        value = round(max_val * frac)
        parts.append(
            '<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" '
            'stroke-width="1" stroke-dasharray="3 3"/>'
            % (_PLOT_LEFT, y, _PLOT_RIGHT, y, _COLOR_GRID)
        )
        parts.append(
            '<text x="%d" y="%.1f" fill="%s" font-size="9" '
            'text-anchor="end">%d</text>'
            % (_PLOT_LEFT - 6, y + 3, _COLOR_TEXT, value)
        )

    # Bars: failed on the bottom, sent stacked on top.
    for idx, day in enumerate(days):
        sent = int(day.get("sent", 0) or 0)
        failed = int(day.get("failed", 0) or 0)
        x = _PLOT_LEFT + idx * slot + (slot - bar_width) / 2
        failed_h = (failed / max_val) * _PLOT_HEIGHT
        sent_h = (sent / max_val) * _PLOT_HEIGHT
        if failed_h > 0:
            parts.append(
                '<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" '
                'fill="%s"><title>%s failed=%d</title></rect>'
                % (
                    x,
                    _PLOT_BOTTOM - failed_h,
                    bar_width,
                    failed_h,
                    _COLOR_FAILED,
                    day.get("date", ""),
                    failed,
                )
            )
        if sent_h > 0:
            parts.append(
                '<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" '
                'fill="%s"><title>%s sent=%d</title></rect>'
                % (
                    x,
                    _PLOT_BOTTOM - failed_h - sent_h,
                    bar_width,
                    sent_h,
                    _COLOR_SENT,
                    day.get("date", ""),
                    sent,
                )
            )

    # Date ticks: keep roughly 7 labels (first/last always labelled).
    step = max(1, int((n - 1) / 7)) if n > 1 else 1
    for idx, day in enumerate(days):
        if idx % step != 0 and idx != n - 1:
            continue
        x = _PLOT_LEFT + idx * slot + slot / 2
        parts.append(
            '<text x="%.1f" y="%d" fill="%s" font-size="9" '
            'text-anchor="middle">%s</text>'
            % (x, _PLOT_BOTTOM + 14, _COLOR_TEXT, _fmt_mmdd(day.get("date", "")))
        )

    # Legend (top-right).
    legend_x = _PLOT_RIGHT - 96
    parts.append(
        '<rect x="%d" y="%d" width="9" height="9" fill="%s"/>'
        '<text x="%d" y="%d" fill="%s" font-size="10">sent</text>'
        % (legend_x, _PLOT_TOP, _COLOR_SENT, legend_x + 13, _PLOT_TOP + 9,
           _COLOR_TEXT)
    )
    parts.append(
        '<rect x="%d" y="%d" width="9" height="9" fill="%s"/>'
        '<text x="%d" y="%d" fill="%s" font-size="10">failed</text>'
        % (legend_x + 52, _PLOT_TOP, _COLOR_FAILED, legend_x + 65,
           _PLOT_TOP + 9, _COLOR_TEXT)
    )

    parts.append("</svg>")
    return "".join(parts)
