"""Report assembly and rendering (terminal + JSON)."""

from __future__ import annotations

import json
from typing import Optional

from .model import ChangeType, Report, Severity, to_jsonable

_CHANGE_LABELS = {
    ChangeType.ADDED: "新增",
    ChangeType.REMOVED: "删除",
    ChangeType.MODIFIED: "修改",
    ChangeType.TYPE_CHANGED: "类型变化",
}

# ANSI colors for terminal rendering.
_COLORS = {
    Severity.CRITICAL: "\x1b[31;1m",  # bold red
    Severity.WARN: "\x1b[33m",  # yellow
    Severity.INFO: "\x1b[36m",  # cyan
    Severity.NONE: "\x1b[0m",
}
_RESET = "\x1b[0m"


def _fmt_value(value) -> str:
    if value is None:
        return "null"
    return json.dumps(to_jsonable(value), ensure_ascii=False)


def _fmt_type(value) -> str:
    if value is None:
        return "-"
    return str(value)


class Reporter:
    """Renders :class:`Report` objects for terminal / JSON output."""

    def render_terminal(self, report: Report, color: bool = True) -> str:
        lines: list[str] = []
        for item in report.items:
            sev = item.severity
            tag = sev.value if color else sev.value
            if color:
                tag = "%s[%s]%s" % (_COLORS[sev], sev.value, _RESET)
            label = _CHANGE_LABELS.get(item.change_type, item.change_type.value)
            where = item.key_path if item.key_path else "(file)"
            old = _fmt_value(item.old_value)
            new = _fmt_value(item.new_value)
            if item.change_type == ChangeType.TYPE_CHANGED:
                detail = "类型变化 (%s -> %s)" % (
                    _fmt_type(item.old_type),
                    _fmt_type(item.new_type),
                )
                lines.append(
                    "%s %s (%s): %s %s -> %s"
                    % (tag, where, item.file, label, old, new)
                )
                lines.append(
                    "%s %s (%s): %s" % (tag, where, item.file, detail)
                )
            else:
                lines.append(
                    "%s %s (%s): %s %s -> %s"
                    % (tag, where, item.file, label, old, new)
                )

        s = report.summary
        lines.append(
            "Summary: added=%d removed=%d modified=%d type_changed=%d "
            "ignored=%d total=%d max=%s"
            % (
                s.added,
                s.removed,
                s.modified,
                s.type_changed,
                s.ignored,
                s.total,
                s.max_severity.value,
            )
        )
        return "\n".join(lines)

    def render_json(self, report: Report) -> str:
        """Render the full 7.6 report JSON document."""
        return json.dumps(
            {"code": 0, "data": report.to_dict(), "message": "ok"},
            ensure_ascii=False,
            indent=2,
        )

    @staticmethod
    def error_json(message: str) -> str:
        """Render an error response (section 7.6)."""
        return json.dumps(
            {"code": 2, "data": None, "message": message},
            ensure_ascii=False,
            indent=2,
        )


def build_report(
    scan_id: Optional[int],
    baseline,
    created_at: str,
    mode: str,
    summary,
    items,
) -> Report:
    """Convenience constructor for :class:`Report`."""
    return Report(
        scan_id=scan_id,
        baseline=baseline,
        created_at=created_at,
        mode=mode,
        summary=summary,
        items=items,
    )
