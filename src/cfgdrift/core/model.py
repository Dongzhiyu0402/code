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
    # v0.4.0: 1-based source line of the key in the *new* file (falling back
    # to the old file for REMOVED items); None when unavailable.
    line: Optional[int] = None
    # v0.4.0: True when a display exit masked the values (raw values stay in
    # the database; masking is applied only at the four display exits).
    masked: bool = False
    # v0.6.0: constraint violations attached by the consistency engine.  Each
    # element is a ``ConstraintViolation.to_dict()`` shaped dict.  It is only
    # emitted by ``to_dict`` when non-empty (zero-noise contract D7).
    constraint_violations: List[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        out = {
            "key_path": self.key_path,
            "change_type": self.change_type.value,
            "severity": self.severity.value,
            "file": self.file,
            "old_value": to_jsonable(self.old_value),
            "new_value": to_jsonable(self.new_value),
            "old_type": self.old_type,
            "new_type": self.new_type,
            "rule_id": self.rule_id,
            "line": self.line,
            "masked": self.masked,
        }
        if self.constraint_violations:
            out["constraint_violations"] = [
                dict(v) for v in self.constraint_violations
            ]
        return out


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
    # v0.4.0: optional ``{relpath: {key_path: line}}`` captured when the
    # baseline was created (used for REMOVED-item line fallback).
    line_maps: Optional[dict] = None

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
        if include_data and self.line_maps:
            out["line_maps"] = self.line_maps
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


@dataclass
class SeverityRule:
    """A user-defined severity override rule (v0.4.0, severity.yaml).

    Rules are applied *after* the built-in default classification with
    first-match-wins (file order).  All pattern fields are optional regexes
    matched against the corresponding item attribute; ``change_type`` matches
    the change-type string (``added`` / ``removed`` / ``modified`` /
    ``type_changed``).  ``value_pattern`` is matched against the JSON
    serialization of either the old or the new value.
    """

    name: str
    severity: Severity
    change_type: Optional[str] = None
    key_pattern: Optional[str] = None
    value_pattern: Optional[str] = None
    file_pattern: Optional[str] = None
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.name or not isinstance(self.name, str):
            raise ValueError("severity rule name must be a non-empty string")
        if not isinstance(self.severity, Severity):
            self.severity = Severity(str(self.severity).upper())
        if self.change_type is not None and not isinstance(self.change_type, str):
            raise ValueError("severity rule %r change_type must be a string" % self.name)

    def matches(self, item: DriftItem) -> bool:
        """Return True when this rule applies to ``item`` (all set fields)."""
        if not self.enabled:
            return False
        if self.change_type is not None and self.change_type != item.change_type.value:
            return False
        if self.key_pattern is not None:
            try:
                if re.search(self.key_pattern, item.key_path) is None:
                    return False
            except re.error:
                return False
        if self.value_pattern is not None:
            if not self._value_matches(item):
                return False
        if self.file_pattern is not None:
            try:
                if re.search(self.file_pattern, item.file or "") is None:
                    return False
            except re.error:
                return False
        return True

    def _value_matches(self, item: DriftItem) -> bool:
        candidates = []
        if item.old_value is not None:
            candidates.append(json.dumps(to_jsonable(item.old_value), ensure_ascii=False))
        if item.new_value is not None:
            candidates.append(json.dumps(to_jsonable(item.new_value), ensure_ascii=False))
        try:
            return any(re.search(self.value_pattern, c) for c in candidates)
        except re.error:
            return False

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "severity": self.severity.value,
            "change_type": self.change_type,
            "key_pattern": self.key_pattern,
            "value_pattern": self.value_pattern,
            "file_pattern": self.file_pattern,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SeverityRule":
        """Build a validated rule from a raw ``severity.yaml`` entry."""
        name = data.get("name")
        if not name or not isinstance(name, str):
            raise ValueError("severity rule is missing a non-empty 'name'")
        try:
            severity = Severity(str(data.get("severity", "WARN")).upper())
        except ValueError:
            raise ValueError(
                "severity rule %r has invalid severity %r"
                % (name, data.get("severity"))
            ) from None
        return cls(
            name=name,
            severity=severity,
            change_type=data.get("change_type"),
            key_pattern=data.get("key_pattern"),
            value_pattern=data.get("value_pattern"),
            file_pattern=data.get("file_pattern"),
            enabled=bool(data.get("enabled", True)),
        )


# ---------------------------------------------------------------------------
# Consistency constraints (v0.6.0)
# ---------------------------------------------------------------------------

CONSTRAINT_TYPES = (
    "range",
    "enum",
    "conditional_required",
    "correlation",
    "mutual_exclusion",
)

_CORRELATION_OPS = (">=", ">", "<=", "<", "==", "!=")


def _is_number(value: Any) -> bool:
    """Return True for int/float values (bool excluded: bool is an int)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check_when(when: Any, constraint_id: str) -> None:
    """Validate the shared ``when`` field for conditional/correlation types."""
    if not isinstance(when, dict):
        raise ValueError(
            "constraint %r 'when' must be a mapping {key, value}" % constraint_id
        )
    key = when.get("key")
    if not key or not isinstance(key, str):
        raise ValueError(
            "constraint %r 'when.key' must be a non-empty string" % constraint_id
        )
    if "value" not in when:
        raise ValueError(
            "constraint %r 'when' must include a 'value'" % constraint_id
        )


@dataclass
class Constraint:
    """One consistency constraint (v0.6.0).

    ``type`` is one of ``range`` / ``enum`` / ``conditional_required`` /
    ``correlation`` / ``mutual_exclusion`` (see :data:`CONSTRAINT_TYPES`).
    Field meaning depends on the type:

    - ``range``: ``keys`` has exactly one dotted path; ``min`` / ``max``
      (at least one) bound the numeric value.
    - ``enum``: ``keys`` has exactly one dotted path; ``allowed`` lists the
      permitted values.
    - ``conditional_required``: ``when`` = ``{"key", "value"}``;
      ``then`` = ``{"require": [path, ...]}`` (all must exist).
    - ``correlation``: ``when`` = ``{"key", "value"}``; ``then`` is a single
      ``{"key", "op", "value"}`` or a list of them (normalized to a list);
      ``op`` ∈ ``>=,>,<=,<,==,!=``.
    - ``mutual_exclusion``: ``keys`` has at least two paths; optional
      ``forbid`` lists ``[v1, v2]`` pairs (default: any two keys coexisting
      is a conflict).

    ``source`` is ``"builtin"`` (built-in library) or ``"user"``
    (``<home>/constraints.yaml`` / ``--constraints`` files).  A corrupt
    constraint raises ``ValueError`` at construction time (the CLI surfaces
    it as exit code 2).
    """

    id: str
    type: str
    message: str
    severity: Severity = Severity.WARN
    enabled: bool = True
    source: str = "builtin"  # "builtin" | "user"
    keys: List[str] = field(default_factory=list)
    min: Optional[float] = None
    max: Optional[float] = None
    allowed: Optional[List[Any]] = None
    when: Optional[dict] = None
    then: Optional[Any] = None
    forbid: Optional[List[list]] = None

    def __post_init__(self) -> None:
        if not self.id or not isinstance(self.id, str):
            raise ValueError("constraint id must be a non-empty string")
        if self.type not in CONSTRAINT_TYPES:
            raise ValueError(
                "constraint %r has invalid type %r (expected one of: %s)"
                % (self.id, self.type, ", ".join(CONSTRAINT_TYPES))
            )
        if not self.message or not isinstance(self.message, str):
            raise ValueError(
                "constraint %r is missing a non-empty 'message'" % self.id
            )
        if not isinstance(self.severity, Severity):
            self.severity = Severity(str(self.severity).upper())
        if self.source not in ("builtin", "user"):
            raise ValueError(
                "constraint %r has invalid source %r (expected builtin or user)"
                % (self.id, self.source)
            )
        if self.keys is None:
            self.keys = []
        else:
            self.keys = list(self.keys)

        if self.type == "range":
            if len(self.keys) != 1:
                raise ValueError(
                    "range constraint %r requires exactly one key" % self.id
                )
            if self.min is None and self.max is None:
                raise ValueError(
                    "range constraint %r requires 'min' and/or 'max'" % self.id
                )
            for bound in (self.min, self.max):
                if bound is not None and not _is_number(bound):
                    raise ValueError(
                        "range constraint %r min/max must be numbers" % self.id
                    )
        elif self.type == "enum":
            if len(self.keys) != 1:
                raise ValueError(
                    "enum constraint %r requires exactly one key" % self.id
                )
            if not isinstance(self.allowed, list) or not self.allowed:
                raise ValueError(
                    "enum constraint %r requires a non-empty 'allowed' list"
                    % self.id
                )
        elif self.type == "conditional_required":
            _check_when(self.when, self.id)
            then = self.then
            if not isinstance(then, dict):
                raise ValueError(
                    "conditional_required constraint %r 'then' must be a "
                    "mapping {require: [...]}" % self.id
                )
            require = then.get("require")
            if not isinstance(require, list) or not require:
                raise ValueError(
                    "conditional_required constraint %r 'then.require' must "
                    "be a non-empty list" % self.id
                )
            for req in require:
                if not req or not isinstance(req, str):
                    raise ValueError(
                        "conditional_required constraint %r 'then.require' "
                        "entries must be non-empty strings" % self.id
                    )
        elif self.type == "correlation":
            _check_when(self.when, self.id)
            raw = self.then
            items = raw if isinstance(raw, list) else [raw]
            if not items:
                raise ValueError(
                    "correlation constraint %r 'then' must not be empty"
                    % self.id
                )
            normalized = []
            for cond in items:
                if not isinstance(cond, dict):
                    raise ValueError(
                        "correlation constraint %r 'then' entries must be "
                        "mappings {key, op, value}" % self.id
                    )
                ckey = cond.get("key")
                op = cond.get("op")
                if not ckey or not isinstance(ckey, str):
                    raise ValueError(
                        "correlation constraint %r 'then.key' must be a "
                        "non-empty string" % self.id
                    )
                if op not in _CORRELATION_OPS:
                    raise ValueError(
                        "correlation constraint %r has invalid op %r "
                        "(expected one of: %s)"
                        % (self.id, op, ", ".join(_CORRELATION_OPS))
                    )
                if "value" not in cond:
                    raise ValueError(
                        "correlation constraint %r 'then.value' is required"
                        % self.id
                    )
                normalized.append(
                    {"key": ckey, "op": op, "value": cond.get("value")}
                )
            self.then = normalized
        elif self.type == "mutual_exclusion":
            if len(self.keys) < 2:
                raise ValueError(
                    "mutual_exclusion constraint %r requires at least two keys"
                    % self.id
                )
            if self.forbid is not None:
                if not isinstance(self.forbid, list):
                    raise ValueError(
                        "mutual_exclusion constraint %r 'forbid' must be a "
                        "list of [v1, v2] pairs" % self.id
                    )
                normalized = []
                for pair in self.forbid:
                    if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                        raise ValueError(
                            "mutual_exclusion constraint %r 'forbid' entries "
                            "must be [v1, v2] pairs" % self.id
                        )
                    normalized.append(list(pair))
                self.forbid = normalized

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "message": self.message,
            "severity": self.severity.value,
            "enabled": self.enabled,
            "source": self.source,
            "keys": list(self.keys),
            "min": self.min,
            "max": self.max,
            "allowed": list(self.allowed) if self.allowed is not None else None,
            "when": self.when,
            "then": self.then,
            "forbid": (
                [list(p) for p in self.forbid] if self.forbid is not None else None
            ),
        }

    @classmethod
    def from_dict(cls, data: dict, source: str = "user") -> "Constraint":
        """Build a validated constraint from a raw YAML/JSON entry.

        ``source`` defaults to ``"user"`` (constraints.yaml / --constraints);
        the built-in library passes ``source="builtin"``.  Missing or corrupt
        fields raise ``ValueError``.
        """
        if not isinstance(data, dict):
            raise ValueError("constraint must be a mapping")
        cid = data.get("id")
        ctype = data.get("type")
        message = data.get("message")
        if not cid or not isinstance(cid, str):
            raise ValueError("constraint is missing a non-empty 'id'")
        if ctype not in CONSTRAINT_TYPES:
            raise ValueError(
                "constraint %r has invalid type %r (expected one of: %s)"
                % (cid, ctype, ", ".join(CONSTRAINT_TYPES))
            )
        if not message or not isinstance(message, str):
            raise ValueError(
                "constraint %r is missing a non-empty 'message'" % cid
            )
        try:
            severity = Severity(str(data.get("severity", "WARN")).upper())
        except ValueError:
            raise ValueError(
                "constraint %r has invalid severity %r"
                % (cid, data.get("severity"))
            ) from None
        if source not in ("builtin", "user"):
            raise ValueError(
                "constraint %r has invalid source %r" % (cid, source)
            )
        raw_keys = data.get("keys") or []
        if not isinstance(raw_keys, list):
            raise ValueError("constraint %r 'keys' must be a list" % cid)
        return cls(
            id=cid,
            type=ctype,
            message=message,
            severity=severity,
            enabled=bool(data.get("enabled", True)),
            source=source,
            keys=[str(k) for k in raw_keys],
            min=data.get("min"),
            max=data.get("max"),
            allowed=data.get("allowed"),
            when=data.get("when"),
            then=data.get("then"),
            forbid=data.get("forbid"),
        )


@dataclass
class ConstraintViolation:
    """One constraint break attached to a :class:`DriftItem` (v0.6.0)."""

    constraint_id: str
    type: str
    message: str
    involved_keys: List[str]

    def to_dict(self) -> dict:
        return {
            "constraint_id": self.constraint_id,
            "type": self.type,
            "message": self.message,
            "involved_keys": list(self.involved_keys),
        }


@dataclass
class CompareReport:
    """Result of comparing one environment's baseline against a reference."""
    baseline_a: str  # reference environment/baseline
    baseline_b: str  # compared environment/baseline
    created_at: str
    summary: ScanSummary
    items: List[DriftItem] = field(default_factory=list)
    # v0.4.0: baseline versions of the two compared environments (displayed
    # by the CLI header and exported by --json; design 4.1).
    env1_version: Optional[int] = None
    env2_version: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "baseline_a": self.baseline_a,
            "baseline_b": self.baseline_b,
            "created_at": self.created_at,
            "env1_version": self.env1_version,
            "env2_version": self.env2_version,
            "summary": self.summary.to_dict(),
            "items": [item.to_dict() for item in self.items],
        }
