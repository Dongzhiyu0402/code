"""Semantic diff engine and severity classification."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .model import (
    ChangeType,
    DriftItem,
    IgnoreRule,
    ScanSummary,
    Severity,
    join_path,
    type_name,
)


class SeverityEngine:
    """Maps change types to severities (PRD defaults)."""

    _MAPPING = {
        ChangeType.REMOVED: Severity.CRITICAL,
        ChangeType.TYPE_CHANGED: Severity.CRITICAL,
        ChangeType.MODIFIED: Severity.WARN,
        ChangeType.ADDED: Severity.INFO,
    }

    @classmethod
    def classify(cls, change_type: ChangeType) -> Severity:
        return cls._MAPPING[change_type]


class SemanticDiffer:
    """Recursive semantic differ over dict/list/scalar trees."""

    def __init__(self) -> None:
        self._engine = SeverityEngine()

    # -- public API --------------------------------------------------------

    def diff(
        self,
        old: dict,
        new: dict,
        file: str = "",
        rules: Optional[List[IgnoreRule]] = None,
    ) -> Tuple[List[DriftItem], ScanSummary]:
        """Diff two semantic trees for a single file.

        ``old`` / ``new`` are normalized trees.  ``file`` is the relpath used
        in the resulting items.  Returns ``(items, summary)``.
        """
        items: List[DriftItem] = []
        self._diff_node(old, new, [], file, items)
        return self._finalize(items, rules)

    def diff_snapshot(
        self,
        old_snapshot: dict,
        new_snapshot: dict,
        rules: Optional[List[IgnoreRule]] = None,
    ) -> Tuple[List[DriftItem], ScanSummary]:
        """Diff two snapshots ``{relpath: tree}``.

        Handles per-file key-level drift for files present in both snapshots
        and file-level drift (added/removed files) for files present in only
        one snapshot.
        """
        items: List[DriftItem] = []
        old_files = set(old_snapshot.keys())
        new_files = set(new_snapshot.keys())

        for relpath in sorted(old_files & new_files):
            self._diff_node(
                old_snapshot[relpath], new_snapshot[relpath], [], relpath, items
            )
        for relpath in sorted(old_files - new_files):
            items.append(
                DriftItem(
                    key_path="",
                    change_type=ChangeType.REMOVED,
                    severity=Severity.CRITICAL,
                    file=relpath,
                    old_value=old_snapshot[relpath],
                    new_value=None,
                    old_type="dict",
                    new_type=None,
                )
            )
        for relpath in sorted(new_files - old_files):
            items.append(
                DriftItem(
                    key_path="",
                    change_type=ChangeType.ADDED,
                    severity=Severity.INFO,
                    file=relpath,
                    old_value=None,
                    new_value=new_snapshot[relpath],
                    old_type=None,
                    new_type="dict",
                )
            )
        return self._finalize(items, rules)

    # -- internals ---------------------------------------------------------

    def _diff_node(
        self,
        old: Any,
        new: Any,
        path: List[Tuple[str, Any]],
        file: str,
        items: List[DriftItem],
    ) -> None:
        old_t = type_name(old)
        new_t = type_name(new)

        if old_t != new_t:
            items.append(
                DriftItem(
                    key_path=join_path(path),
                    change_type=ChangeType.TYPE_CHANGED,
                    severity=SeverityEngine.classify(ChangeType.TYPE_CHANGED),
                    file=file,
                    old_value=old,
                    new_value=new,
                    old_type=old_t,
                    new_type=new_t,
                )
            )
            return

        if old_t == "dict":
            assert isinstance(old, dict) and isinstance(new, dict)
            for key in sorted(set(old.keys()) | set(new.keys())):
                if key not in old:
                    items.append(
                        DriftItem(
                            key_path=join_path(path + [("key", key)]),
                            change_type=ChangeType.ADDED,
                            severity=SeverityEngine.classify(ChangeType.ADDED),
                            file=file,
                            old_value=None,
                            new_value=new[key],
                            old_type=None,
                            new_type=type_name(new[key]),
                        )
                    )
                elif key not in new:
                    items.append(
                        DriftItem(
                            key_path=join_path(path + [("key", key)]),
                            change_type=ChangeType.REMOVED,
                            severity=SeverityEngine.classify(ChangeType.REMOVED),
                            file=file,
                            old_value=old[key],
                            new_value=None,
                            old_type=type_name(old[key]),
                            new_type=None,
                        )
                    )
                else:
                    self._diff_node(
                        old[key], new[key], path + [("key", key)], file, items
                    )
        elif old_t == "list":
            assert isinstance(old, list) and isinstance(new, list)
            length = max(len(old), len(new))
            for i in range(length):
                if i >= len(old):
                    items.append(
                        DriftItem(
                            key_path=join_path(path + [("index", i)]),
                            change_type=ChangeType.ADDED,
                            severity=SeverityEngine.classify(ChangeType.ADDED),
                            file=file,
                            old_value=None,
                            new_value=new[i],
                            old_type=None,
                            new_type=type_name(new[i]),
                        )
                    )
                elif i >= len(new):
                    items.append(
                        DriftItem(
                            key_path=join_path(path + [("index", i)]),
                            change_type=ChangeType.REMOVED,
                            severity=SeverityEngine.classify(ChangeType.REMOVED),
                            file=file,
                            old_value=old[i],
                            new_value=None,
                            old_type=type_name(old[i]),
                            new_type=None,
                        )
                    )
                else:
                    self._diff_node(
                        old[i], new[i], path + [("index", i)], file, items
                    )
        else:
            # Scalars of the same category.
            if old != new:
                items.append(
                    DriftItem(
                        key_path=join_path(path),
                        change_type=ChangeType.MODIFIED,
                        severity=SeverityEngine.classify(ChangeType.MODIFIED),
                        file=file,
                        old_value=old,
                        new_value=new,
                        old_type=old_t,
                        new_type=new_t,
                    )
                )

    def _finalize(
        self,
        items: List[DriftItem],
        rules: Optional[List[IgnoreRule]],
    ) -> Tuple[List[DriftItem], ScanSummary]:
        rules = rules or []
        kept: List[DriftItem] = []
        ignored_count = 0
        for item in items:
            matched = None
            for rule in rules:
                if rule.matches(item):
                    matched = rule
                    break
            if matched is not None:
                item.rule_id = matched.id if matched.id is not None else 0
                ignored_count += 1
            else:
                kept.append(item)

        summary = ScanSummary()
        for item in kept:
            if item.change_type == ChangeType.ADDED:
                summary.added += 1
            elif item.change_type == ChangeType.REMOVED:
                summary.removed += 1
            elif item.change_type == ChangeType.MODIFIED:
                summary.modified += 1
            elif item.change_type == ChangeType.TYPE_CHANGED:
                summary.type_changed += 1
        summary.ignored = ignored_count
        summary.max_severity = Severity.max_of(
            *(item.severity for item in kept)
        )
        return kept, summary
