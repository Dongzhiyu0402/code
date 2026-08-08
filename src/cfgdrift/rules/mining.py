"""Constraint auto-mining (v0.7.0, C-08).

Candidates are mined from historical drift data (``source=scans`` — the
``scan_items`` table grouped by ``scan_id`` as one change unit) or from the
corpus (``source=corpus`` — ``instances.jsonl`` ``diff.feature``, avoiding a
second parse).  Three candidate kinds are produced (Q3):

- ``enum`` / ``range`` — value-domain candidates (distinct ∈ [2, 8] -> enum;
  all-numeric -> range);
- ``conditional_required`` — co-change linkage (confidence ≥ 0.8);
- ``mutual_exclusion`` — value pairs that never co-occur (zero-intersection).

Candidates are **never auto-activated** (D5): ``constraint.enabled`` is
``false`` and ``status`` is ``pending``; promotion is a manual
``cfgdrift constraint add --rule '<json>'`` + ``constraint enable``.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import yaml

logger = logging.getLogger("cfgdrift.rules.mining")

_MIN_CANDIDATES_VERSION = 1
_ENUM_MIN_DISTINCT = 2
_ENUM_MAX_DISTINCT = 8
_CONFIDENCE_THRESHOLD = 0.8
_MUTUAL_TOP_N = 5

#: Kinds produced by the miner (enum/range share the value-domain pass).
CANDIDATE_KINDS = ("enum", "range", "conditional_required", "mutual_exclusion")


@dataclass
class MinedCandidate:
    """One mined constraint candidate (never auto-activated, D5)."""

    id: str
    kind: str  # enum | range | conditional_required | mutual_exclusion
    constraint: dict  # Constraint.to_dict() shape (enabled: false)
    metrics: dict  # {support, confidence, samples, source}
    status: str = "pending"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "constraint": dict(self.constraint),
            "metrics": dict(self.metrics),
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MinedCandidate":
        if not isinstance(data, dict):
            raise ValueError("mined candidate must be a mapping")
        cid = data.get("id")
        kind = data.get("kind")
        constraint = data.get("constraint")
        metrics = data.get("metrics")
        if not cid or not isinstance(cid, str):
            raise ValueError("mined candidate is missing a non-empty 'id'")
        if kind not in CANDIDATE_KINDS:
            raise ValueError(
                "mined candidate %r has invalid kind %r" % (cid, kind)
            )
        if not isinstance(constraint, dict):
            raise ValueError("mined candidate %r 'constraint' must be a mapping" % cid)
        if not isinstance(metrics, dict):
            raise ValueError("mined candidate %r 'metrics' must be a mapping" % cid)
        return cls(
            id=cid,
            kind=kind,
            constraint=constraint,
            metrics=metrics,
            status=str(data.get("status", "pending")),
        )


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _jsonable(value: Any) -> Any:
    return value


def _marker(value: Any) -> str:
    """Hashable marker for a possibly-unhashable config value (JSON)."""
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def _value_counts(values: List[Any]) -> Dict[str, int]:
    """Count values by JSON marker (safe for dict/list values)."""
    counts: Dict[str, int] = {}
    for value in values:
        marker = _marker(value)
        counts[marker] = counts.get(marker, 0) + 1
    return counts


def _dominant_value(values: List[Any]) -> Any:
    """Most common value; ties broken by marker for determinism."""
    if not values:
        return None
    counts = _value_counts(values)
    order = sorted(counts.keys(), key=lambda marker: (-counts[marker], marker))
    best_marker = order[0]
    for value in values:
        if _marker(value) == best_marker:
            return value
    return None


class ConstraintMiner:
    """Mines constraint candidates from scans or corpus data."""

    # -- public API -------------------------------------------------------

    @staticmethod
    def mine_scans(store, min_support: int = 5) -> List[MinedCandidate]:
        """Mine from ``scan_items`` grouped by ``scan_id`` (source=scans)."""
        rows = store.list_scan_items()
        units: List[Dict[str, Any]] = []
        by_scan: Dict[int, Dict[str, Any]] = {}
        for row in rows:
            unit = by_scan.setdefault(row["scan_id"], {})
            unit[row["key_path"]] = row["new_value"]
        units = list(by_scan.values())
        return ConstraintMiner._mine_units(units, min_support, "scans")

    @staticmethod
    def mine_corpus(jsonl_path: str, min_support: int = 5) -> List[MinedCandidate]:
        """Mine from instances.jsonl ``diff.feature`` (source=corpus).

        Each JSONL line is one change unit; values are the post-change
        (``after``) values from ``feature.changed_values`` — no second parse.
        """
        if not os.path.exists(jsonl_path):
            raise ValueError("corpus file not found: %s" % jsonl_path)
        units: List[Dict[str, Any]] = []
        with open(jsonl_path, "r", encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    entry = json.loads(stripped)
                except ValueError as exc:
                    raise ValueError(
                        "corpus line %d: invalid JSON: %s" % (line_no, exc)
                    ) from exc
                feature = (entry or {}).get("diff", {}).get("feature", {})
                if not isinstance(feature, dict):
                    raise ValueError(
                        "corpus line %d: diff.feature must be a mapping" % line_no
                    )
                changed_values = feature.get("changed_values")
                if not isinstance(changed_values, dict):
                    raise ValueError(
                        "corpus line %d: diff.feature.changed_values must be "
                        "a mapping" % line_no
                    )
                unit: Dict[str, Any] = {}
                for key, value_info in changed_values.items():
                    if isinstance(value_info, dict) and "after" in value_info:
                        unit[key] = value_info["after"]
                    else:
                        unit[key] = value_info
                units.append(unit)
        return ConstraintMiner._mine_units(units, min_support, "corpus")

    @staticmethod
    def save_candidates(path: str, candidates: List[MinedCandidate],
                        source: str = "scans", min_support: int = 5) -> None:
        """Persist candidates to ``mined_candidates.yaml`` (version 1)."""
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        payload = {
            "version": _MIN_CANDIDATES_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source": source,
            "min_support": min_support,
            "candidates": [c.to_dict() for c in candidates],
        }
        with open(path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(payload, fh, allow_unicode=True, sort_keys=False)

    @staticmethod
    def mark_promoted(path: str, candidate_id: str) -> None:
        """Mark a candidate ``status: promoted`` atomically (v0.11.0, P0-3).

        Loads the candidate file, flips the matching candidate to
        ``status=promoted``, then writes the whole payload back through a
        temp file + ``os.replace`` so a failed write never corrupts the
        original file.  Raises ``ValueError`` when the id is unknown (or the
        file is missing/corrupt).
        """
        if not os.path.exists(path):
            raise ValueError("mined candidates file not found: %s" % path)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
        except Exception as exc:  # noqa: BLE001 - surface as a readable error
            raise ValueError(
                "mined candidates file %s is corrupt: %s" % (path, exc)
            ) from exc
        if not isinstance(data, dict):
            raise ValueError("mined candidates config must be a mapping at %s" % path)
        raw = data.get("candidates")
        if not isinstance(raw, list):
            raise ValueError(
                "mined candidates 'candidates' must be a list at %s" % path
            )
        entries = [dict(e) for e in raw if isinstance(e, dict)]
        target = next((e for e in entries if e.get("id") == candidate_id), None)
        if target is None:
            raise ValueError(
                "mined candidate %r not found" % candidate_id
            )
        target["status"] = "promoted"
        data["candidates"] = entries
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            yaml.safe_dump(data, fh, allow_unicode=True, sort_keys=False)
        os.replace(tmp, path)

    @staticmethod
    def load_candidates(path: str) -> List[MinedCandidate]:
        """Load + validate mined_candidates.yaml (empty list when absent)."""
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if not isinstance(data, dict):
            raise ValueError("mined candidates config must be a mapping at %s" % path)
        if data.get("version") != _MIN_CANDIDATES_VERSION:
            raise ValueError(
                "unsupported mined candidates version %r (expected %d)"
                % (data.get("version"), _MIN_CANDIDATES_VERSION)
            )
        raw = data.get("candidates") or []
        if not isinstance(raw, list):
            raise ValueError(
                "mined candidates 'candidates' must be a list at %s" % path
            )
        out = []
        for entry in raw:
            if not isinstance(entry, dict):
                raise ValueError(
                    "mined candidates entries must be mappings at %s" % path
                )
            out.append(MinedCandidate.from_dict(entry))
        return out

    # -- shared algorithm -------------------------------------------------

    @staticmethod
    def _mine_units(units: List[Dict[str, Any]], min_support: int,
                    source: str) -> List[MinedCandidate]:
        """Run the three candidate passes over change units (Q3 thresholds)."""
        min_support = max(1, int(min_support))
        candidates: List[MinedCandidate] = []
        counters = {kind: 0 for kind in CANDIDATE_KINDS}

        key_values: Dict[str, List[Any]] = {}
        for unit in units:
            for key, value in unit.items():
                key_values.setdefault(key, []).append(value)

        # 1. value domains: enum (distinct ∈ [2, 8]) > range (all numeric).
        for key in sorted(key_values.keys()):
            values = key_values[key]
            if len(values) < min_support:
                continue
            distinct = [v for v in _distinct(values) if _is_scalar(v)]
            if 2 <= len(distinct) <= _ENUM_MAX_DISTINCT:
                counters["enum"] += 1
                cid = "mined_enum_%d" % counters["enum"]
                constraint = _constraint_base(
                    cid, "enum", "WARN",
                    "%s 必须是 %s 之一（挖掘候选，待人工确认）"
                    % (key, " / ".join(str(v) for v in distinct)),
                    keys=[key], allowed=distinct,
                )
                candidates.append(
                    MinedCandidate(
                        id=cid, kind="enum", constraint=constraint,
                        metrics={
                            "support": len(values),
                            "confidence": 1.0,
                            "samples": len(values),
                            "source": source,
                        },
                    )
                )
                continue
            numeric = [v for v in values if _is_number(v)]
            if len(numeric) == len(values):
                counters["range"] += 1
                cid = "mined_range_%d" % counters["range"]
                lo, hi = min(numeric), max(numeric)
                observed = True
                if "port" in key and 1 <= lo and hi <= 65535:
                    lo, hi = 1, 65535
                    observed = False
                constraint = _constraint_base(
                    cid, "range", "WARN",
                    "%s 必须在 [%s, %s] 范围内（挖掘候选，待人工确认）"
                    % (key, lo, hi),
                    keys=[key], cmin=lo, cmax=hi,
                )
                metrics: Dict[str, Any] = {
                    "support": len(values),
                    "confidence": 1.0,
                    "samples": len(values),
                    "source": source,
                }
                if observed:
                    metrics["observed"] = True
                candidates.append(
                    MinedCandidate(
                        id=cid, kind="range", constraint=constraint,
                        metrics=metrics,
                    )
                )

        # 2. co-change linkage: conditional_required (conf ≥ 0.8).
        cnt: Dict[str, int] = {}
        co: Dict[Tuple[str, str], int] = {}
        for unit in units:
            keys = sorted(set(unit.keys()))
            for key in keys:
                cnt[key] = cnt.get(key, 0) + 1
            for i in range(len(keys)):
                for j in range(i + 1, len(keys)):
                    pair = (keys[i], keys[j])
                    co[pair] = co.get(pair, 0) + 1
        for (a, b), support in sorted(co.items(), key=lambda kv: (-kv[1], kv[0])):
            if support < min_support:
                continue
            confidence = support / max(1, cnt.get(a, 0))
            if confidence < _CONFIDENCE_THRESHOLD:
                continue
            dom = _dominant_value(key_values.get(a, []))
            counters["conditional_required"] += 1
            cid = "mined_conditional_required_%d" % counters["conditional_required"]
            constraint = _constraint_base(
                cid, "conditional_required", "WARN",
                "%s 缺失（%s=%s 需要该字段；挖掘候选，待人工确认）" % (b, a, dom),
                when={"key": a, "value": dom},
                then={"require": [b]},
            )
            candidates.append(
                MinedCandidate(
                    id=cid, kind="conditional_required", constraint=constraint,
                    metrics={
                        "support": support,
                        "confidence": round(confidence, 4),
                        "samples": cnt.get(a, 0),
                        "source": source,
                    },
                )
            )

        # 3. mutual exclusion: zero-intersection value pairs (top-N 5).
        pair_keys = sorted(
            (a, b) for (a, b) in co.keys()
            if cnt.get(a, 0) >= min_support and cnt.get(b, 0) >= min_support
        )
        joint: Dict[tuple, int] = {}
        for unit in units:
            keys = sorted(set(unit.keys()))
            for i in range(len(keys)):
                for j in range(i + 1, len(keys)):
                    a, b = keys[i], keys[j]
                    key = (a, b)
                    marker_pair = (_marker(unit[a]), _marker(unit[b]))
                    joint[(key, marker_pair)] = joint.get((key, marker_pair), 0) + 1
        for a, b in pair_keys:
            va_counts = _value_counts(key_values[a])
            vb_counts = _value_counts(key_values[b])
            va_by_marker = {_marker(v): v for v in key_values[a]}
            vb_by_marker = {_marker(v): v for v in key_values[b]}
            found = []
            for ma, ca in va_counts.items():
                if ca < min_support:
                    continue
                for mb, cb in vb_counts.items():
                    if cb < min_support:
                        continue
                    if joint.get(((a, b), (ma, mb)), 0) > 0:
                        continue
                    found.append(
                        (min(ca, cb), va_by_marker[ma], vb_by_marker[mb])
                    )
            found.sort(key=lambda item: (-item[0], str(item[1]), str(item[2])))
            for support_val, va, vb in found[:_MUTUAL_TOP_N]:
                counters["mutual_exclusion"] += 1
                cid = "mined_mutual_exclusion_%d" % counters["mutual_exclusion"]
                constraint = _constraint_base(
                    cid, "mutual_exclusion", "WARN",
                    "%s=%s 与 %s=%s 互斥（挖掘候选，待人工确认）" % (a, va, b, vb),
                    keys=[a, b], forbid=[[va, vb]],
                )
                candidates.append(
                    MinedCandidate(
                        id=cid, kind="mutual_exclusion", constraint=constraint,
                        metrics={
                            "support": support_val,
                            "confidence": 1.0,
                            "samples": support_val,
                            "source": source,
                        },
                    )
                )

        return candidates


def _distinct(values: List[Any]) -> List[Any]:
    """Order-preserving distinct values (JSON-safe keys for sorting)."""
    seen = []
    seen_set = set()
    for value in values:
        try:
            marker = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            marker = str(value)
        if marker in seen_set:
            continue
        seen_set.add(marker)
        seen.append(value)
    return seen


def _constraint_base(cid: str, ctype: str, severity: str, message: str,
                     keys: Optional[List[str]] = None,
                     cmin: Any = None, cmax: Any = None,
                     allowed: Optional[List[Any]] = None,
                     when: Optional[dict] = None,
                     then: Optional[Any] = None,
                     forbid: Optional[List[list]] = None) -> dict:
    """Build a candidate constraint dict in ``Constraint.to_dict()`` shape.

    ``enabled`` is always ``false`` (D5 — candidates never auto-activate) and
    ``source`` is ``user`` so promotion via ``constraint add --rule`` works.
    """
    return {
        "id": cid,
        "type": ctype,
        "message": message,
        "severity": severity,
        "enabled": False,
        "source": "user",
        "keys": list(keys or []),
        "min": cmin,
        "max": cmax,
        "allowed": allowed,
        "when": when,
        "then": then,
        "forbid": forbid,
    }
