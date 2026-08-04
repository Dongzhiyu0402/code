"""Corpus workspace layout + state.json (v0.7.0).

Layout::

    <workspace>/
      corpus.yaml          # configuration (init template)
      state.json           # incremental fetch state
      repos/               # cloned git repositories (<owner>__<repo>/)
      instances.jsonl      # normalized corpus (export product)

state.json schema (version 1)::

    {
      "version": 1,
      "fetched_at": "2026-08-04T12:00:00+00:00",
      "repos": {
        "nginx/nginx": {
          "local_path": "/abs/path",
          "last_commit": "abc123...",
          "stars": 18000,
          "star_checked": true,
          "instance_count": 47
        }
      }
    }
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict

from .config import CorpusConfig

_STATE_VERSION = 1


class CorpusWorkspace:
    """Manages a corpus workspace directory (config + state + repos)."""

    def __init__(self, root: str) -> None:
        self.root = os.path.abspath(root)

    # -- paths -----------------------------------------------------------

    def config_path(self) -> str:
        return CorpusConfig.default_path(self.root)

    def state_path(self) -> str:
        return os.path.join(self.root, "state.json")

    def instances_path(self) -> str:
        return os.path.join(self.root, "instances.jsonl")

    def repos_dir(self) -> str:
        return os.path.join(self.root, "repos")

    def repo_dir(self, owner: str, repo: str) -> str:
        """Clone target directory ``repos/<owner>__<repo>``."""
        return os.path.join(self.repos_dir(), "%s__%s" % (owner, repo))

    # -- init ------------------------------------------------------------

    def init(self) -> None:
        """Create the workspace directory structure + template files.

        Existing files are never overwritten (idempotent): corpus.yaml is only
        written when absent, state.json is initialized to an empty state.
        """
        os.makedirs(self.repos_dir(), exist_ok=True)
        if not os.path.exists(self.config_path()):
            CorpusConfig.template(self.root).save(self.config_path())
        if not os.path.exists(self.state_path()):
            self.write_state(self.empty_state())

    # -- state -----------------------------------------------------------

    @staticmethod
    def empty_state() -> Dict[str, Any]:
        return {"version": _STATE_VERSION, "fetched_at": None, "repos": {}}

    def read_state(self) -> Dict[str, Any]:
        """Read + validate state.json (empty state when absent)."""
        if not os.path.exists(self.state_path()):
            return self.empty_state()
        try:
            with open(self.state_path(), "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            raise ValueError("corrupt state.json at %s: %s" % (self.state_path(), exc))
        if not isinstance(data, dict):
            raise ValueError("corrupt state.json: expected a mapping")
        data.setdefault("version", _STATE_VERSION)
        data.setdefault("fetched_at", None)
        data.setdefault("repos", {})
        if data.get("version") != _STATE_VERSION:
            raise ValueError(
                "unsupported state version %r (expected %d)"
                % (data.get("version"), _STATE_VERSION)
            )
        if not isinstance(data["repos"], dict):
            raise ValueError("corrupt state.json: 'repos' must be a mapping")
        return data

    def write_state(self, state: Dict[str, Any]) -> None:
        """Persist state.json (creates parent dirs)."""
        os.makedirs(self.root, exist_ok=True)
        tmp = self.state_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.state_path())

    def repo_state(self, state: Dict[str, Any], key: str) -> Dict[str, Any]:
        """Return the mutable repo entry for ``key`` (creating it)."""
        repos = state.setdefault("repos", {})
        entry = repos.get(key)
        if entry is None:
            entry = {
                "local_path": None,
                "last_commit": None,
                "stars": None,
                "star_checked": False,
                "instance_count": 0,
            }
            repos[key] = entry
        if not isinstance(entry, dict):
            raise ValueError("corrupt state.json: repo %r must be a mapping" % key)
        return entry

    def set_repo_error(self, state: Dict[str, Any], key: str, error: str) -> None:
        """Mark a repository as failed with ``error`` (partial-success state).

        The previous progress (``last_commit`` / ``instance_count`` /
        ``local_path``) is intentionally kept: a later transient failure must
        never discard already-collected change pairs.  ``corpus fetch`` skips
        error-marked repositories unless ``--retry-failed`` is given; the user
        can also delete the ``"error"`` marker to retry.
        """
        entry = self.repo_state(state, key)
        entry["error"] = str(error)

    @staticmethod
    def clear_repo_error(entry: Dict[str, Any]) -> None:
        """Remove the error marker from a repo entry after a successful run."""
        entry.pop("error", None)

    def resolve_local_path(self, entry: Dict[str, Any], fallback: str) -> str:
        """Return the git repo path for an entry: recorded local_path or clone."""
        recorded = entry.get("local_path")
        if recorded and os.path.isdir(recorded):
            return recorded
        return fallback
