"""Corpus validator: JSONL schema validation + statistics (v0.7.0).

``corpus validate`` reads ``instances.jsonl`` line by line and checks the
§1.2.7 schema.  A corrupt line raises :class:`ValueError` (CLI exit code 2);
the aggregate statistics (instance count / repo count / format distribution /
four change-type distribution / constraint-violation count) are returned as a
dict for the CLI to print.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict

logger = logging.getLogger("cfgdrift.corpus.validator")

_SCHEMA_VERSION = 1
_CHANGE_TYPES = ("added", "removed", "modified", "type_changed")
_REQUIRED_SUMMARY_KEYS = (
    "added", "removed", "modified", "type_changed", "ignored", "total",
    "max_severity",
)
_REQUIRED_METADATA_KEYS = (
    "owner", "repo", "path", "commit", "commit_time", "author", "message",
)


class CorpusValidator:
    """Line-by-line schema validation for the normalized corpus."""

    @staticmethod
    def validate(path: str) -> Dict[str, Any]:
        """Validate ``instances.jsonl``; returns aggregate statistics.

        Raises :class:`ValueError` on the first corrupt line (with the line
        number) or when the file is unreadable.
        """
        if not os.path.exists(path):
            raise ValueError("corpus file not found: %s" % path)
        stats: Dict[str, Any] = {
            "instances": 0,
            "repos": set(),
            "formats": {},
            "changes": {t: 0 for t in _CHANGE_TYPES},
            "constraint_violations": 0,
            "parse_errors": 0,
        }
        with open(path, "r", encoding="utf-8") as fh:
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
                CorpusValidator._check_entry(entry, line_no)
                CorpusValidator._accumulate(entry, stats)
        stats["repos"] = sorted(stats["repos"])
        return stats

    # -- schema ----------------------------------------------------------

    @staticmethod
    def _check_entry(entry: Any, line_no: int) -> None:
        def bad(message: str) -> ValueError:
            return ValueError("corpus line %d: %s" % (line_no, message))

        if not isinstance(entry, dict):
            raise bad("entry must be a mapping")
        if entry.get("schema_version") != _SCHEMA_VERSION:
            raise bad(
                "schema_version must be %d" % _SCHEMA_VERSION
            )
        if not isinstance(entry.get("instance_id"), str) or not entry["instance_id"]:
            raise bad("instance_id must be a non-empty string")

        metadata = entry.get("metadata")
        if not isinstance(metadata, dict):
            raise bad("metadata must be a mapping")
        for key in _REQUIRED_METADATA_KEYS:
            if key not in metadata:
                raise bad("metadata is missing %r" % key)
        if not isinstance(metadata.get("owner"), str) or not isinstance(
            metadata.get("repo"), str
        ):
            raise bad("metadata.owner/repo must be strings")

        file_info = entry.get("file")
        if not isinstance(file_info, dict):
            raise bad("file must be a mapping")
        if not isinstance(file_info.get("relpath"), str) or not file_info["relpath"]:
            raise bad("file.relpath must be a non-empty string")
        if not isinstance(file_info.get("format"), str):
            raise bad("file.format must be a string")

        for side in ("before", "after"):
            node = entry.get(side)
            if not isinstance(node, dict):
                raise bad("%s must be a mapping" % side)
            if "tree" not in node:
                raise bad("%s is missing 'tree'" % side)
            tree = node["tree"]
            if tree is not None and not isinstance(tree, (dict, list)):
                raise bad("%s.tree must be a tree or null" % side)
            if not isinstance(node.get("parse_ok"), bool):
                raise bad("%s.parse_ok must be a boolean" % side)
            if not isinstance(node.get("present"), bool):
                raise bad("%s.present must be a boolean" % side)

        diff = entry.get("diff")
        if not isinstance(diff, dict):
            raise bad("diff must be a mapping")
        items = diff.get("items")
        if not isinstance(items, list):
            raise bad("diff.items must be a list")
        for item in items:
            CorpusValidator._check_item(item, line_no)
        summary = diff.get("summary")
        if not isinstance(summary, dict):
            raise bad("diff.summary must be a mapping")
        for key in _REQUIRED_SUMMARY_KEYS:
            if key not in summary:
                raise bad("diff.summary is missing %r" % key)
        violations = diff.get("constraint_violations")
        if not isinstance(violations, list):
            raise bad("diff.constraint_violations must be a list")
        for violation in violations:
            if not isinstance(violation, dict):
                raise bad("diff.constraint_violations entries must be mappings")
            for key in ("constraint_id", "type", "message", "involved_keys"):
                if key not in violation:
                    raise bad(
                        "diff.constraint_violations entry is missing %r" % key
                    )

        feature = diff.get("feature")
        if not isinstance(feature, dict):
            raise bad("diff.feature must be a mapping")
        if not isinstance(feature.get("changed_keys"), list):
            raise bad("diff.feature.changed_keys must be a list")
        if not isinstance(feature.get("changed_values"), dict):
            raise bad("diff.feature.changed_values must be a mapping")
        pairs = feature.get("co_change_pairs")
        if not isinstance(pairs, list):
            raise bad("diff.feature.co_change_pairs must be a list")
        for pair in pairs:
            if not isinstance(pair, list) or len(pair) != 2:
                raise bad(
                    "diff.feature.co_change_pairs entries must be [a, b] pairs"
                )
        if not isinstance(feature.get("co_change_capped"), bool):
            raise bad("diff.feature.co_change_capped must be a boolean")

        labels = entry.get("labels")
        if not isinstance(labels, dict):
            raise bad("labels must be a mapping")
        if "severity" not in labels:
            raise bad("labels is missing 'severity'")
        if "annotation" not in labels or "annotator" not in labels:
            raise bad("labels is missing 'annotation'/'annotator'")

    @staticmethod
    def _check_item(item: Any, line_no: int) -> None:
        def bad(message: str) -> ValueError:
            return ValueError("corpus line %d: %s" % (line_no, message))

        if not isinstance(item, dict):
            raise bad("diff.items entries must be mappings")
        if "key_path" not in item or "change_type" not in item:
            raise bad("diff.items entry is missing key_path/change_type")
        if item.get("change_type") not in _CHANGE_TYPES:
            raise bad(
                "diff.items change_type must be one of: %s"
                % ", ".join(_CHANGE_TYPES)
            )
        if "severity" not in item or "file" not in item:
            raise bad("diff.items entry is missing severity/file")

    # -- statistics ------------------------------------------------------

    @staticmethod
    def _accumulate(entry: dict, stats: Dict[str, Any]) -> None:
        stats["instances"] += 1
        metadata = entry["metadata"]
        stats["repos"].add("%s/%s" % (metadata["owner"], metadata["repo"]))
        fmt = entry["file"]["format"]
        stats["formats"][fmt] = stats["formats"].get(fmt, 0) + 1
        for item in entry["diff"]["items"]:
            ctype = item.get("change_type")
            if ctype in stats["changes"]:
                stats["changes"][ctype] += 1
        stats["constraint_violations"] += len(
            entry["diff"]["constraint_violations"]
        )
        if (entry["before"]["present"] and not entry["before"]["parse_ok"]) or (
            entry["after"]["present"] and not entry["after"]["parse_ok"]
        ):
            stats["parse_errors"] += 1
