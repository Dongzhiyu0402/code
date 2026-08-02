"""Data model: enums, drift items, summary, report, baseline, ignore rule.

This module defines the shared semantic model used across the engine.  All
values stored in the semantic tree are plain ``dict`` / ``list`` / scalars
(``str`` / ``int`` / ``float`` / ``bool`` / ``None``).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time
from enum import Enum
from typing import Any, List, Optional


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class Severity(str, Enum):
    """Severity levels, ordered CRITICAL > WARN > INFO > NONE."""

    CRITICAL = "CRITICAL"
    WARN = "WARN"
    INFO = "INFO"
    NONE = "NONE"

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self]

    @classmethod
    def max_of(cls, *severities: "Severity") -> "Severity":
        """Return the most severe of the given severities (NONE if empty)."""
        best = cls.NONE
        for s in severities:
            if s is not None and s.rank > best.rank:
                best = s
        return best


_SEVERITY_RANK = {
    Severity.NONE: 0,
    Severity.INFO: 1,
    Severity.WARN: 2,
    Severity.CRITICAL: 3,
}


class ChangeType(str, Enum):
    """Kinds of drift detected between two semantic trees."""

    ADDED = "added"
    REMOVED = "removed"
    MODIFIED = "modified"
    TYPE_CHANGED = "type_changed"


# ---------------------------------------------------------------------------
# Key-path helpers (section 7.2 of the system design)
# ---------------------------------------------------------------------------

def escape_segment(segment: Any) -> str:
    """Escape one key-path segment.

    Segments containing ``.``, ``[``, ``]`` or ``\\`` are escaped with a
    backslash so the resulting path is unambiguous.
    """
    text = str(segment)
    return (
        text.replace("\\", "\\\\")
        .replace(".", "\\.")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )


def join_path(parts: List[Any]) -> str:
    """Join path parts into a key-path string.

    Dictionary keys are joined with ``.``; list indices are appended as
    ``[i]``.  Parts are tuples ``("key", value)`` or ``("index", i)``.
    """
    out = ""
    for kind, value in parts:
        if kind == "index":
            out += "[%d]" % int(value)
        else:
            if out:
                out += "."
            out += escape_segment(value)
    return out


def parse_path(path: str) -> List[str]:
    """Split a key-path string back into unescaped segments (best effort).

    Only used for display/tests; the differ builds paths via :func:`join_path`.
    """
    if not path:
        return []
    parts: List[str] = []
    buf: List[str] = []
    escaped = False
    for ch in path:
        if escaped:
            buf.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == ".":
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if escaped:
        buf.append("\\")
    parts.append("".join(buf))
    return parts


# ---------------------------------------------------------------------------
# Type categories (section 7.1)
# ---------------------------------------------------------------------------

def type_name(value: Any) -> Optional[str]:
    """Return the semantic type category of a value.

    Categories: ``str`` / ``int`` / ``float`` / ``bool`` / ``null`` /
    ``list`` / ``dict``.  ``int`` and ``float`` are distinct categories so an
    ``int`` -> ``float`` change is reported as a type change (CRITICAL).
    Datetime-like objects are treated as strings (TOML datetimes are already
    normalized to ISO-8601 strings; PyYAML date objects are normalized by the
    parser).
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (datetime, date, time)):
        return "str"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "dict"
    return type(value).__name__


def to_jsonable(value: Any) -> Any:
    """Convert a semantic-tree value into something JSON serializable."""
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    return value


# ---------------------------------------------------------------------------
# Model classes
# ---------------------------------------------------------------------------

@dataclass
class DriftItem:
    """A single detected drift (key-level or file-level)."""

    key_path: str
    change_type: ChangeType
    severity: Severity
    file: str
    old_value: Any = None
    new_value: Any = None
    old_type: Optional[str] = None
    new_type: Optional[str] = None
    rule_id: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "key_path": self.key_path,
            "change_type": self.change_type.value,
            "severity": self.severity.value,
            "file": self.file,
            "old_value": to_jsonable(self.old_value),
            "new_value": to_jsonable(self.new_value),
            "old_type": self.old_type,
            "new_type": self.new_type,
            "rule_id": self.rule_id,
        }


@dataclass
class ScanSummary:
    """Aggregated counters for a scan/diff."""

    added: int = 0
    removed: int = 0
    modified: int = 0
    type_changed: int = 0
    ignored: int = 0

    def __post_init__(self) -> None:
        # ``max_severity`` is a property backed by ``_max_severity``; ensure a
        # sane default exists even when no setter was called (e.g. empty scans).
        if not hasattr(self, "_max_severity"):
            self._max_severity = Severity.NONE

    @property
    def total(self) -> int:
        return self.added + self.removed + self.modified + self.type_changed

    @property
    def max_severity(self) -> Severity:
        return self._max_severity

    @max_severity.setter
    def max_severity(self, value) -> None:
        if isinstance(value, str):
            value = Severity(value)
        self._max_severity = value

    def to_dict(self) -> dict:
        return {
            "added": self.added,
            "removed": self.removed,
            "modified": self.modified,
            "type_changed": self.type_changed,
            "ignored": self.ignored,
            "total": self.total,
            "max_severity": self.max_severity.value,
        }


@dataclass
class Baseline:
    """A stored baseline snapshot (versioned by name)."""

    id: int
    name: str
    version: int
    description: str
    created_at: str
    scan_root: str
    format: str
    data: dict = field(default_factory=dict)

    def to_dict(self, include_data: bool = False) -> dict:
        out = {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "created_at": self.created_at,
            "scan_root": self.scan_root,
            "format": self.format,
        }
        if include_data:
            out["data"] = self.data
        return out


@dataclass
class Report:
    """A full drift report (scan result)."""

    scan_id: Optional[int]
    baseline: Optional[Baseline]
    created_at: str
    mode: str
    summary: ScanSummary
    items: List[DriftItem] = field(default_factory=list)

    def to_dict(self) -> dict:
        baseline_ref = None
        if self.baseline is not None:
            baseline_ref = {
                "name": self.baseline.name,
                "version": self.baseline.version,
            }
        return {
            "scan_id": self.scan_id,
            "mode": self.mode,
            "created_at": self.created_at,
            "baseline": baseline_ref,
            "summary": self.summary.to_dict(),
            "items": [item.to_dict() for item in self.items],
        }

    def to_json(self) -> str:
        return json.dumps(
            {"code": 0, "data": self.to_dict(), "message": "ok"},
            ensure_ascii=False,
            indent=2,
        )


@dataclass
class IgnoreRule:
    """An ignore rule that filters drift items.

    ``match_type`` is one of ``path_exact`` / ``path_prefix`` / ``regex`` and
    is applied against ``item.key_path``.  ``file_pattern`` (optional) is a
    regex matched against ``item.file`` (relpath).  ``change_type`` (optional)
    filters by change type (e.g. ``"added"``).  A rule matches when the key
    pattern AND the optional file pattern AND the optional change type all
    match.
    """

    id: Optional[int]
    baseline_id: Optional[int]
    name: str
    key_pattern: str
    match_type: str
    file_pattern: Optional[str] = None
    change_type: Optional[str] = None
    enabled: bool = True

    def matches(self, item: DriftItem) -> bool:
        if not self.enabled:
            return False
        if self.change_type is not None:
            if self.change_type != item.change_type.value:
                return False
        if self.file_pattern is not None and self.file_pattern:
            if item.file is None or not re.search(self.file_pattern, item.file):
                return False
        if self.match_type == "path_exact":
            return self.key_pattern == item.key_path
        if self.match_type == "path_prefix":
            return item.key_path.startswith(self.key_pattern)
        if self.match_type == "regex":
            try:
                return re.search(self.key_pattern, item.key_path) is not None
            except re.error:
                return False
        return False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "baseline_id": self.baseline_id,
            "name": self.name,
            "key_pattern": self.key_pattern,
            "match_type": self.match_type,
            "file_pattern": self.file_pattern,
            "change_type": self.change_type,
            "enabled": self.enabled,
        }
