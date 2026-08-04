"""Corpus exporter: change pairs -> normalized JSONL instances (v0.7.0).

Pipeline per change pair (D4 / §1.2.6):

    parse_text(before, fmt) / parse_text(after, fmt)
      -> SemanticDiffer().diff_snapshot({f: before}, {f: after}, constraints=...)
      -> instance dict (metadata / before / after / diff / labels)
      -> one JSONL line (text bodies are NOT persisted — D8).

``export`` re-derives every pair from git (deterministic full rewrite) using
the ``instance_count`` recorded in ``state.json`` as the quota so the output
always matches what ``corpus fetch`` collected.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

from ..core.differ import SemanticDiffer
from ..core.parser import parse_text
from ..core.model import Constraint
from .config import CorpusConfig, CorpusRepository, fmt_for_path
from .fetcher import ChangePair, ChangePairExtractor, GitCloneSource, LocalRepoSource
from .workspace import CorpusWorkspace

logger = logging.getLogger("cfgdrift.corpus.exporter")

_SCHEMA_VERSION = 1
#: Hard cap on co-change pairs before truncation kicks in (D8).
_CO_CHANGE_CAP = 500
#: changed_keys above this threshold forces co_change_capped = True (D8).
_CHANGED_KEYS_TRUNCATE = 50


class CorpusExporter:
    """Builds the normalized ``instances.jsonl`` corpus."""

    def __init__(self) -> None:
        self._differ = SemanticDiffer()
        self._extractor = ChangePairExtractor()

    # -- public API -------------------------------------------------------

    def export(
        self,
        workspace: CorpusWorkspace,
        config: CorpusConfig,
        constraints: Optional[List[Constraint]] = None,
        output: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Full deterministic rewrite of the instances JSONL.

        Returns stats: ``{instances, repos, parse_failures, per_repo}``.
        """
        output_path = os.path.abspath(output or workspace.instances_path())
        state = workspace.read_state()
        repo_entries = state.get("repos", {})

        lines: List[str] = []
        stats: Dict[str, Any] = {
            "instances": 0,
            "repos": 0,
            "parse_failures": 0,
            "per_repo": {},
        }
        for repo in config.repositories:
            entry = repo_entries.get(repo.key)
            if entry is None:
                logger.warning(
                    "corpus export: %s has not been fetched; skipping", repo.key
                )
                continue
            quota = int(entry.get("instance_count", 0) or 0)
            if quota <= 0:
                continue
            try:
                source = self._source_for(repo, entry, workspace)
                source.clone_or_fetch()
            except (ValueError, RuntimeError) as exc:
                logger.warning("corpus export: skipping %s: %s", repo.key, exc)
                continue
            pairs, ext_stats, _ = self._extractor.extract_repo(
                source,
                since=config.effective_since(repo),
                stop_at=None,
                max_pairs=quota,
                glob_pattern=repo.glob,
            )
            stats["parse_failures"] += int(ext_stats.get("parse_failures", 0))
            repo_count = 0
            commit_counters: Dict[str, int] = {}
            for pair in pairs:
                idx = commit_counters.get(pair.commit, 0)
                commit_counters[pair.commit] = idx + 1
                try:
                    instance = self._build_instance(
                        pair, repo.owner, repo.repo, constraints,
                        index_in_commit=idx,
                    )
                except ValueError as exc:
                    stats["parse_failures"] += 1
                    logger.warning(
                        "corpus export: skipped %s@%s: %s",
                        pair.relpath, pair.commit[:8], exc,
                    )
                    continue
                lines.append(json.dumps(instance, ensure_ascii=False))
                repo_count += 1
            stats["instances"] += repo_count
            stats["per_repo"][repo.key] = repo_count
            if repo_count:
                stats["repos"] += 1

        parent = os.path.dirname(output_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as fh:
            for line in lines:
                fh.write(line + "\n")
        return stats

    # -- internals --------------------------------------------------------

    def _source_for(
        self, repo: CorpusRepository, entry: dict, workspace: CorpusWorkspace
    ):
        """Build the git source for a repository (local path or clone)."""
        if repo.is_local():
            return LocalRepoSource(repo.local_path)  # type: ignore[arg-type]
        clone_dir = workspace.repo_dir(repo.owner, repo.repo)
        recorded = entry.get("local_path")
        if recorded and os.path.isdir(recorded):
            return LocalRepoSource(recorded)
        return GitCloneSource(repo.owner, repo.repo, clone_dir)

    def _build_instance(
        self,
        pair: ChangePair,
        owner: str,
        repo: str,
        constraints: Optional[List[Constraint]],
        index_in_commit: int = 0,
    ) -> dict:
        """Build one normalized JSONL instance from a change pair."""
        fmt = fmt_for_path(pair.relpath)
        if fmt is None:  # pragma: no cover - guarded by the config whitelist
            raise ValueError("unsupported config extension: %s" % pair.relpath)

        before_present = bool(pair.before_text and pair.before_text.strip())
        after_present = bool(pair.after_text and pair.after_text.strip())
        before_tree = None
        after_tree = None
        if before_present:
            before_tree = parse_text(pair.before_text, fmt)  # type: ignore[arg-type]
        if after_present:
            after_tree = parse_text(pair.after_text, fmt)  # type: ignore[arg-type]

        old_snapshot = {pair.relpath: before_tree} if before_tree is not None else {}
        new_snapshot = {pair.relpath: after_tree} if after_tree is not None else {}
        items, summary = self._differ.diff_snapshot(
            old_snapshot, new_snapshot, constraints=constraints
        )

        item_dicts = [it.to_dict() for it in items]
        constraint_violations: List[dict] = []
        for it in items:
            constraint_violations.extend(
                list(getattr(it, "constraint_violations", None) or [])
            )

        changed_keys = [it.key_path for it in items if it.key_path]
        changed_values: Dict[str, Any] = {}
        for it in items:
            if it.key_path:
                changed_values[it.key_path] = {
                    "before": it.old_value,
                    "after": it.new_value,
                }
        co_pairs, capped = self._co_change_pairs(changed_keys)

        return {
            "schema_version": _SCHEMA_VERSION,
            "instance_id": "%s-%s-%s-%d" % (
                owner, repo, pair.commit[:7], index_in_commit,
            ),
            "metadata": {
                "owner": owner,
                "repo": repo,
                "path": pair.relpath,
                "commit": pair.commit,
                "commit_time": pair.commit_time,
                "author": pair.author,
                "message": pair.message,
            },
            "file": {"relpath": pair.relpath, "format": fmt},
            "before": {
                "tree": before_tree,
                "parse_ok": True,
                "present": before_present,
            },
            "after": {
                "tree": after_tree,
                "parse_ok": True,
                "present": after_present,
            },
            "diff": {
                "items": item_dicts,
                "summary": summary.to_dict(),
                "constraint_violations": constraint_violations,
                "feature": {
                    "changed_keys": changed_keys,
                    "changed_values": changed_values,
                    "co_change_pairs": co_pairs,
                    "co_change_capped": capped,
                },
            },
            "labels": {
                "severity": summary.max_severity.value,
                "annotation": None,
                "annotator": None,
            },
        }

    @staticmethod
    def _co_change_pairs(changed_keys: List[str]) -> tuple:
        """Unordered pairs of keys changed together (D8 anti-bloat)."""
        keys = list(changed_keys)
        if len(keys) > _CHANGED_KEYS_TRUNCATE:
            keys = keys[:_CHANGED_KEYS_TRUNCATE]
            capped = True
        else:
            capped = False
        pairs: List[List[str]] = []
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                pairs.append([keys[i], keys[j]])
                if len(pairs) >= _CO_CHANGE_CAP:
                    return pairs, True
        return pairs, capped
