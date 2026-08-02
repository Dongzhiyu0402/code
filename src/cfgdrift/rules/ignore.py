"""Ignore-rule matching engine.

The :class:`IgnoreRule` dataclass lives in ``core.model``; this module
provides validation, construction helpers and a thin matching facade used by
the store and the CLI.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..core.model import DriftItem, IgnoreRule

VALID_MATCH_TYPES = ("path_exact", "path_prefix", "regex")

VALID_CHANGE_TYPES = ("added", "removed", "modified", "type_changed")


def validate_match_type(match_type: str) -> str:
    """Validate a match type; returns it normalized."""
    if match_type not in VALID_MATCH_TYPES:
        raise ValueError(
            "invalid match_type %r (expected one of: %s)"
            % (match_type, ", ".join(VALID_MATCH_TYPES))
        )
    return match_type


def validate_change_type(change_type: Optional[str]) -> Optional[str]:
    """Validate an optional change-type filter."""
    if change_type is None:
        return None
    if change_type not in VALID_CHANGE_TYPES:
        raise ValueError(
            "invalid change_type %r (expected one of: %s)"
            % (change_type, ", ".join(VALID_CHANGE_TYPES))
        )
    return change_type


def make_rule(
    name: str,
    key_pattern: str,
    match_type: str,
    baseline_id: Optional[int] = None,
    file_pattern: Optional[str] = None,
    change_type: Optional[str] = None,
    enabled: bool = True,
    rule_id: Optional[int] = None,
) -> IgnoreRule:
    """Construct a validated :class:`IgnoreRule`."""
    match_type = validate_match_type(match_type)
    change_type = validate_change_type(change_type)
    return IgnoreRule(
        id=rule_id,
        baseline_id=baseline_id,
        name=name,
        key_pattern=key_pattern,
        match_type=match_type,
        file_pattern=file_pattern,
        change_type=change_type,
        enabled=enabled,
    )


def rule_from_row(row: Dict[str, Any]) -> IgnoreRule:
    """Build an :class:`IgnoreRule` from a dict / sqlite3.Row."""
    return IgnoreRule(
        id=row.get("id"),
        baseline_id=row.get("baseline_id"),
        name=row.get("name", ""),
        key_pattern=row.get("key_pattern", ""),
        match_type=row.get("match_type", "path_exact"),
        file_pattern=row.get("file_pattern"),
        change_type=row.get("change_type"),
        enabled=bool(row.get("enabled", 1)),
    )


def filter_items(
    items: List[DriftItem], rules: List[IgnoreRule]
) -> tuple:
    """Filter drift items through rules.

    Returns ``(kept_items, ignored_count)``.  Matched items have their
    ``rule_id`` set and are excluded from the kept list.
    """
    kept: List[DriftItem] = []
    ignored = 0
    for item in items:
        matched: Optional[IgnoreRule] = None
        for rule in rules:
            if rule.matches(item):
                matched = rule
                break
        if matched is not None:
            item.rule_id = matched.id if matched.id is not None else 0
            ignored += 1
        else:
            kept.append(item)
    return kept, ignored
