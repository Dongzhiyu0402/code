"""Semantic diff engine and severity classification."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .model import (
    ChangeType,
    DriftItem,
    IgnoreRule,
    ScanSummary,
    Severity,
    SeverityRule,
    join_path,
    type_name,
)
from .constraints import ConstraintEngine


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
        severity_rules: Optional[List[SeverityRule]] = None,
        old_lines: Optional[Dict[str, Dict[str, int]]] = None,
        new_lines: Optional[Dict[str, Dict[str, int]]] = None,
        constraints: Optional[List[Any]] = None,  # v0.6.0
    ) -> Tuple[List[DriftItem], ScanSummary]:
        """Diff two semantic trees for a single file.

        ``old`` / ``new`` are normalized trees.  ``file`` is the relpath used
        in the resulting items.  ``severity_rules`` (v0.4.0) override the
        built-in severity classification with first-match-wins before the
        summary is computed; ``old_lines`` / ``new_lines`` attach 1-based
        source lines to each item (new side preferred).  ``constraints``
        (v0.6.0, optional) is a list of consistency constraints evaluated
        against the *new* tree after severity overrides; violations upgrade
        the affected items' severities (see :mod:`cfgdrift.core.constraints`).
        Returns ``(items, summary)``.
        """
        items: List[DriftItem] = []
        self._diff_node(old, new, [], file, items)
        return self._finish(
            items,
            rules,
            severity_rules,
            old_lines,
            new_lines,
            new_tree={file: new} if file else {"": new},
            constraints=constraints,
        )

    def diff_snapshot(
        self,
        old_snapshot: dict,
        new_snapshot: dict,
        rules: Optional[List[IgnoreRule]] = None,
        severity_rules: Optional[List[SeverityRule]] = None,
        old_lines: Optional[Dict[str, Dict[str, int]]] = None,
        new_lines: Optional[Dict[str, Dict[str, int]]] = None,
        constraints: Optional[List[Any]] = None,  # v0.6.0
    ) -> Tuple[List[DriftItem], ScanSummary]:
        """Diff two snapshots ``{relpath: tree}``.

        Handles per-file key-level drift for files present in both snapshots
        and file-level drift (added/removed files) for files present in only
        one snapshot.  ``severity_rules`` / ``old_lines`` / ``new_lines`` are
        the v0.4.0 extensions described in :meth:`diff`; ``constraints``
        (v0.6.0, optional) is evaluated per file against the new snapshot
        tree.
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
        return self._finish(
            items,
            rules,
            severity_rules,
            old_lines,
            new_lines,
            new_tree=new_snapshot,
            constraints=constraints,
        )

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

    def _finish(
        self,
        items: List[DriftItem],
        rules: Optional[List[IgnoreRule]],
        severity_rules: Optional[List[SeverityRule]] = None,
        old_lines: Optional[Dict[str, Dict[str, int]]] = None,
        new_lines: Optional[Dict[str, Dict[str, int]]] = None,
        new_tree: Optional[Dict[str, Any]] = None,  # v0.6.0
        constraints: Optional[List[Any]] = None,  # v0.6.0
    ) -> Tuple[List[DriftItem], ScanSummary]:
        """Post-process: constraints attach -> severity override -> upgrade.

        The custom severity rules run *before* the ignore filter so that
        ``summary.max_severity`` is computed over the overridden severities
        (v0.4.0 decision: alert thresholds keep working with zero changes).
        v0.6.0: consistency constraints are applied after severity overrides
        (C-06: override first, then upgrade) and before ignore filtering —
        ignored items carry their violations out of the output.  Line numbers
        are attached to the final kept items only.

        v0.8.0 (D1): the order inside ``_finish`` becomes
        ``attach -> severity override -> upgrade`` — constraints are first
        *attached* without touching severity so custom severity rules with
        ``constraint_id`` can read ``item.constraint_violations``; the single
        upgrade pass then runs on the overridden severity.  Because the
        upgrade formula is monotone in ``item.severity``, rules without
        ``constraint_id`` produce byte-identical output to v0.7.0.
        """
        if constraints:
            ConstraintEngine.attach(new_tree, items, constraints)
        self._apply_custom_severity(items, severity_rules)
        if constraints:
            ConstraintEngine.upgrade(items, constraints)
        kept, summary = self._finalize(items, rules)
        self._attach_lines(kept, old_lines, new_lines)
        return kept, summary

    def _apply_custom_severity(
        self,
        items: List[DriftItem],
        severity_rules: Optional[List[SeverityRule]],
    ) -> None:
        """Overwrite item severities with the first matching rule (file order)."""
        if not severity_rules:
            return
        for item in items:
            for rule in severity_rules:
                if rule.matches(item):
                    item.severity = rule.severity
                    break  # first-match-wins

    def _attach_lines(
        self,
        items: List[DriftItem],
        old_lines: Optional[Dict[str, Dict[str, int]]],
        new_lines: Optional[Dict[str, Dict[str, int]]],
    ) -> None:
        """Attach ``item.line`` from the new-side map, falling back to old."""
        if not old_lines and not new_lines:
            return
        for item in items:
            line = None
            if new_lines:
                file_map = new_lines.get(item.file)
                if file_map and item.key_path in file_map:
                    line = file_map[item.key_path]
            if line is None and old_lines:
                file_map = old_lines.get(item.file)
                if file_map and item.key_path in file_map:
                    line = file_map[item.key_path]
            item.line = line

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
