"""CSV report export (v0.9.0, P0-4).

``CsvReporter.render_csv(data)`` turns the *already-masked* 7.6 report
``data`` document (``store.get_scan`` -> ``SensitiveMasker.mask_payload``)
into a UTF-8-BOM CSV string that Excel / WPS open directly.

The CLI ``report --csv PATH`` and the Web ``GET /api/reports/{id}/csv``
endpoint share this single renderer, so both exits are byte-identical (D3).

Columns: ``scan_id, severity, key_path, change_type, file, line, old_value,
new_value, rule, constraint_violations``.  Value cells are serialized with
``json.dumps(ensure_ascii=False)`` (``None`` -> ``null``, matching the
terminal exit); ``masked:true`` items append the「(已脱敏)」marker to their
value cells (the values are already the mask text); the ``rule`` column is
the item ``rule_id``; ``constraint_violations`` is the deduplicated, sorted
list of ``constraint_id`` values joined with ``;``.

Line endings are ``\\r\\n`` (``csv.writer`` ``lineterminator``) and the
output always starts with ``\\ufeff``.  A report without drift items still
produces the header row (with the BOM).
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any, Dict, List, Optional

_CSV_HEADER = [
    "scan_id",
    "severity",
    "key_path",
    "change_type",
    "file",
    "line",
    "old_value",
    "new_value",
    "rule",
    "constraint_violations",
]


class CsvReporter:
    """Renders a masked report ``data`` document as CSV text."""

    @staticmethod
    def render_csv(data: Dict[str, Any]) -> str:
        """Render ``data`` (7.6 report ``data``, masked) to a CSV string.

        Returns a UTF-8 BOM-prefixed string using ``\\r\\n`` line endings.
        """
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\r\n")
        writer.writerow(_CSV_HEADER)

        scan_id = data.get("scan_id", "")
        for item in data.get("items", []) or []:
            masked = bool(item.get("masked", False))
            violations = item.get("constraint_violations", None) or []
            constraint_ids = sorted(
                {
                    str(v.get("constraint_id", ""))
                    for v in violations
                    if v.get("constraint_id")
                }
            )
            writer.writerow(
                [
                    scan_id,
                    item.get("severity", ""),
                    item.get("key_path", ""),
                    item.get("change_type", ""),
                    item.get("file", ""),
                    item.get("line") if item.get("line") is not None else "",
                    CsvReporter._value_cell(item.get("old_value"), masked),
                    CsvReporter._value_cell(item.get("new_value"), masked),
                    item.get("rule_id", ""),
                    ";".join(constraint_ids),
                ]
            )
        return "\ufeff" + buf.getvalue()

    @staticmethod
    def _value_cell(value: Any, masked: bool) -> str:
        """Serialize one drift value (mask marker appended when masked)."""
        text = json.dumps(value, ensure_ascii=False)
        if masked:
            text += "(已脱敏)"
        return text


def render_csv(data: Dict[str, Any]) -> str:
    """Module-level shortcut for :meth:`CsvReporter.render_csv`."""
    return CsvReporter.render_csv(data)
