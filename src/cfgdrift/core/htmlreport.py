"""Single-file offline HTML report rendering (v0.5.0).

:func:`HtmlReporter.render_html` turns the 7.6 report ``data`` document
(``store.get_scan`` -> ``SensitiveMasker.mask_payload``) into a complete,
standalone HTML page with **zero external dependencies** — no CDN, no
external fonts, no chart libraries (Q4).  The same renderer is used by the
CLI ``report --html`` and the Web ``GET /api/reports/{id}/html`` endpoint so
both exits are structurally identical (D6).

Severity colors match the Web dashboard CSS variables (Q4):

- CRITICAL ``#ef4444``
- WARN     ``#f59e0b``
- INFO     ``#22c55e``
- NONE     ``#64748b``

Layout: summary cards -> severity distribution bars -> item table.  Items
with ``masked: true`` show a「已脱敏」badge with masked values; items with a
line number render ``file:line`` in the location column (D10).
"""

from __future__ import annotations

import html as _html
from typing import Any, Dict, List

_SEVERITY_COLORS = {
    "CRITICAL": "#ef4444",
    "WARN": "#f59e0b",
    "INFO": "#22c55e",
    "NONE": "#64748b",
}

_SEVERITY_ORDER = ("CRITICAL", "WARN", "INFO", "NONE")

_CHANGE_LABELS = {
    "added": "新增",
    "removed": "删除",
    "modified": "修改",
    "type_changed": "类型变化",
}


def _esc(value: Any) -> str:
    """HTML-escape a value for safe embedding."""
    if value is None:
        return ""
    return _html.escape(str(value), quote=True)


def _fmt_value(value: Any) -> str:
    """Format a drift value for display (JSON-ish, None -> \"null\")."""
    if value is None:
        return "null"
    if isinstance(value, str):
        # Compact strings are shown verbatim; strings that look like
        # structured data (dict/list/scalars) are shown with quoting.
        return value
    try:
        import json

        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


class HtmlReporter:
    """Renders the 7.6 report data as a standalone offline HTML page."""

    @staticmethod
    def render_html(data: dict, title: str = "") -> str:
        """Render ``data`` (the 7.6 ``data`` part) into a full HTML document.

        Expected keys: ``scan_id`` / ``created_at`` / ``mode`` / ``baseline``
        (``{"name", "version"}``, optional) / ``summary`` / ``items``.
        """
        data = data or {}
        scan_id = data.get("scan_id")
        created_at = data.get("created_at", "")
        mode = data.get("mode", "")
        baseline = data.get("baseline") or {}
        summary = data.get("summary") or {}
        items = data.get("items") or []

        doc_title = title or (
            "cfgdrift report" + (" #%s" % scan_id if scan_id is not None else "")
        )
        total = int(summary.get("total", 0) or 0)
        max_sev = str(summary.get("max_severity", "NONE") or "NONE").upper()

        summary_cards = HtmlReporter._summary_cards(
            scan_id, created_at, mode, baseline, total, max_sev
        )
        dist_html = HtmlReporter._severity_distribution(items)
        table_html = HtmlReporter._items_table(items)

        return """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{
    --bg: #0f172a; --panel: #1e293b; --panel-2: #273449;
    --text: #e2e8f0; --muted: #94a3b8; --accent: #38bdf8;
    --critical: #ef4444; --warn: #f59e0b; --info: #22c55e; --none: #64748b;
    --border: #334155;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: "Segoe UI", "Microsoft YaHei", system-ui, sans-serif;
    background: var(--bg); color: var(--text);
    padding: 24px 32px; max-width: 1100px; margin: 0 auto;
  }}
  h1 {{ font-size: 22px; margin-bottom: 4px; }}
  .muted {{ color: var(--muted); font-size: 13px; }}
  .card {{
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 12px; padding: 16px; margin: 16px 0;
  }}
  .stat-row {{ display: flex; gap: 16px; flex-wrap: wrap; }}
  .stat {{ background: var(--panel); border: 1px solid var(--border);
    border-radius: 12px; padding: 14px 18px; min-width: 110px; }}
  .stat .num {{ font-size: 26px; font-weight: 700; }}
  .stat .label {{ font-size: 12px; color: var(--muted); }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--border);
    word-break: break-all; vertical-align: top; }}
  th {{ color: var(--muted); font-weight: 600; }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 999px;
    font-size: 11px; font-weight: 700; }}
  .b-critical {{ background: rgba(239,68,68,.18); color: var(--critical); }}
  .b-warn {{ background: rgba(245,158,11,.18); color: var(--warn); }}
  .b-info {{ background: rgba(34,197,94,.18); color: var(--info); }}
  .b-none {{ background: rgba(100,116,139,.18); color: var(--none); }}
  .masked-badge {{ display: inline-block; padding: 1px 6px; border-radius: 6px;
    font-size: 10px; background: rgba(56,189,248,.15); color: var(--accent);
    margin-left: 4px; }}
  .bar-row {{ display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }}
  .bar-label {{ width: 90px; font-size: 13px; color: var(--muted); }}
  .bar-track {{ flex: 1; background: var(--panel-2); border-radius: 8px;
    height: 18px; overflow: hidden; }}
  .bar-fill {{ height: 100%; border-radius: 8px; }}
  .bar-num {{ width: 40px; text-align: right; font-size: 13px; }}
  .filters {{ display: flex; gap: 8px; flex-wrap: wrap; margin: 12px 0; }}
  .filters button {{ background: var(--panel-2); border: 1px solid var(--border);
    color: var(--text); padding: 6px 12px; border-radius: 8px; cursor: pointer;
    font-size: 12px; }}
  .filters button.active {{ background: var(--accent); color: #082f49; font-weight: 600; }}
  .none {{ display: none; }}
</style>
</head>
<body>
<h1>{title}</h1>
<p class="muted">由 cfgdrift 生成 · 单文件离线报告</p>
{summary_cards}
<div class="card">
  <h2 style="font-size:16px;margin-bottom:10px">严重度分布</h2>
  {dist_html}
</div>
<div class="card">
  <h2 style="font-size:16px;margin-bottom:10px">变更列表</h2>
  <div class="filters" id="sevFilters">
    <button data-sev="" class="active">全部</button>
    <button data-sev="CRITICAL">CRITICAL</button>
    <button data-sev="WARN">WARN</button>
    <button data-sev="INFO">INFO</button>
    <button data-sev="NONE">NONE</button>
  </div>
  <table id="itemsTable">
    <thead><tr><th>严重度</th><th>键路径</th><th>变更类型</th>
      <th>文件 / 行号</th><th>旧值</th><th>新值</th><th>规则</th></tr></thead>
    <tbody>{table_html}</tbody>
  </table>
</div>
<script>
(function () {{
  var filters = document.querySelectorAll("#sevFilters button");
  var rows = document.querySelectorAll("#itemsTable tbody tr");
  filters.forEach(function (btn) {{
    btn.addEventListener("click", function () {{
      filters.forEach(function (b) {{ b.classList.remove("active"); }});
      btn.classList.add("active");
      var sev = btn.getAttribute("data-sev");
      rows.forEach(function (row) {{
        row.classList.toggle("none", sev !== "" && row.getAttribute("data-sev") !== sev);
      }});
    }});
  }});
}})();
</script>
</body>
</html>
""".format(
            title=_esc(doc_title),
            summary_cards=summary_cards,
            dist_html=dist_html,
            table_html=table_html,
        )

    # -- internals --------------------------------------------------------

    @staticmethod
    def _summary_cards(
        scan_id: Any,
        created_at: str,
        mode: str,
        baseline: dict,
        total: int,
        max_sev: str,
    ) -> str:
        """Render the summary card row (drift total + baseline/scan meta)."""
        baseline_text = ""
        if baseline and baseline.get("name"):
            baseline_text = (
                '<div class="stat"><div class="num" style="font-size:16px">%s</div>'
                '<div class="label">基线 v%s</div></div>'
                % (
                    _esc(baseline.get("name")),
                    _esc(baseline.get("version") if baseline.get("version") is not None else "?"),
                )
            )
        return (
            '<div class="stat-row">'
            '<div class="stat"><div class="num">%d</div><div class="label">漂移总数</div></div>'
            '<div class="stat"><div class="num"><span class="badge b-%s">%s</span></div>'
            '<div class="label">最大严重度</div></div>'
            '<div class="stat"><div class="num" style="font-size:16px">#%s</div>'
            '<div class="label">scan_id</div></div>'
            '<div class="stat"><div class="num" style="font-size:16px">%s</div>'
            '<div class="label">mode</div></div>'
            '%s'
            '</div>'
            '<p class="muted" style="margin-top:8px">创建时间：%s</p>'
            % (
                total,
                str(max_sev).lower(),
                _esc(max_sev),
                _esc(scan_id if scan_id is not None else "-"),
                _esc(mode or "-"),
                baseline_text,
                _esc(created_at or "-"),
            )
        )

    @staticmethod
    def _severity_distribution(items: List[dict]) -> str:
        """Render severity distribution bars (counts per severity)."""
        counts: Dict[str, int] = {sev: 0 for sev in _SEVERITY_ORDER}
        for item in items or []:
            sev = str(item.get("severity", "NONE") or "NONE").upper()
            if sev not in counts:
                sev = "NONE"
            counts[sev] += 1
        peak = max(1, max(counts.values()) or 1)
        bars = []
        for sev in _SEVERITY_ORDER:
            n = counts[sev]
            color = _SEVERITY_COLORS[sev]
            width = int(round(n / peak * 100)) if n else 0
            bars.append(
                '<div class="bar-row">'
                '<div class="bar-label">%s</div>'
                '<div class="bar-track"><div class="bar-fill" '
                'style="width:%d%%;background:%s"></div></div>'
                '<div class="bar-num">%d</div></div>'
                % (sev, width, color, n)
            )
        return "".join(bars)

    @staticmethod
    def _items_table(items: List[dict]) -> str:
        """Render the item rows (severity / key / type / file:line / values)."""
        if not items:
            return '<tr><td colspan="7" class="muted">无漂移项</td></tr>'
        rows = []
        for item in items or []:
            sev = str(item.get("severity", "NONE") or "NONE").upper()
            if sev not in _SEVERITY_COLORS:
                sev = "NONE"
            key_path = item.get("key_path") or "(file)"
            change_type = str(item.get("change_type", ""))
            change_label = _CHANGE_LABELS.get(change_type, change_type)
            file = item.get("file") or "-"
            line = item.get("line")
            location = file
            if line is not None and file and file != "-":
                location = "%s:%d" % (file, int(line))
            masked = bool(item.get("masked", False))
            masked_badge = '<span class="masked-badge">已脱敏</span>' if masked else ""
            old_value = _esc(_fmt_value(item.get("old_value")))
            new_value = _esc(_fmt_value(item.get("new_value")))
            rule_id = item.get("rule_id")
            rule_text = "#%s" % rule_id if rule_id is not None else "-"
            rows.append(
                '<tr data-sev="%s">'
                '<td><span class="badge b-%s">%s</span></td>'
                "<td>%s</td>"
                "<td>%s</td>"
                "<td>%s%s</td>"
                "<td>%s</td>"
                "<td>%s</td>"
                "<td>%s</td>"
                "</tr>"
                % (
                    sev,
                    str(sev).lower(),
                    _esc(sev),
                    _esc(key_path),
                    _esc(change_label),
                    _esc(location),
                    masked_badge,
                    old_value,
                    new_value,
                    _esc(rule_text),
                )
            )
        return "".join(rows)
