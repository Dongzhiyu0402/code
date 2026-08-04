"""Git history sources and change-pair extraction (v0.7.0, D4).

Three layers:
- :class:`GitHistorySource` (abstract) — git access behind one interface;
- :class:`GitCloneSource` — ``git clone --filter=blob:none --no-checkout`` +
  incremental ``git fetch`` (network; partial clone keeps the working set
  small); :class:`LocalRepoSource` — a local git repository used directly
  (offline / CI-safe);
- :class:`ChangePairExtractor` — turns git history into ``(before, after)``
  change pairs for config files (parse-validated, bounded by quota).

:class:`GitHubApi` performs the best-effort star check (urllib; failure only
warns and never blocks collection).
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import subprocess
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..core.parser import parse_text
from .config import fmt_for_path, is_config_path

logger = logging.getLogger("cfgdrift.corpus.fetcher")


# ---------------------------------------------------------------------------
# git plumbing
# ---------------------------------------------------------------------------

def _run_git(directory: str, args: List[str]) -> Tuple[int, str, str]:
    """Run ``git -C <directory> <args>``; returns ``(code, stdout, stderr)``."""
    cmd = ["git", "-C", directory] + list(args)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )
    except OSError as exc:
        raise RuntimeError("failed to run git: %s" % exc) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("git command timed out: %s" % " ".join(cmd)) from exc
    return proc.returncode, proc.stdout, proc.stderr


def _parse_ok(text: Optional[str], fmt: Optional[str]) -> bool:
    """True when text parses (or is absent); False on a parse error."""
    if text is None or text.strip() == "":
        return True
    if fmt is None:
        return False
    try:
        parse_text(text, fmt)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Change pair
# ---------------------------------------------------------------------------

@dataclass
class ChangePair:
    """One (before, after) change of a config file in a single commit."""

    relpath: str
    commit: str
    commit_time: str  # ISO-8601 UTC
    author: str
    message: str
    before_text: Optional[str]
    after_text: Optional[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "relpath": self.relpath,
            "commit": self.commit,
            "commit_time": self.commit_time,
            "author": self.author,
            "message": self.message,
            "before_text": self.before_text,
            "after_text": self.after_text,
        }


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

class GitHistorySource:
    """Abstract git history access used by the change-pair extractor."""

    def clone_or_fetch(self) -> None:
        """Ensure the repository is available (clone or incremental fetch)."""
        raise NotImplementedError

    def list_config_files(self, glob_pattern: Optional[str] = None) -> List[str]:
        """List config-whitelisted files at HEAD, optionally filtered by glob."""
        raise NotImplementedError

    def repo_log(self, since: Optional[str] = None) -> List[Dict[str, Any]]:
        """Repo-wide commit log, newest first (no merges)."""
        raise NotImplementedError

    def changed_files(self, commit: str,
                      glob_pattern: Optional[str] = None) -> List[str]:
        """Config-whitelisted files changed by ``commit`` (sorted)."""
        raise NotImplementedError

    def show(self, commit: str, relpath: str) -> Optional[str]:
        """Return the file content at ``commit`` (None when unavailable)."""
        raise NotImplementedError


class GitCloneSource(GitHistorySource):
    """Subprocess git source backed by a clone (partial clone, no checkout)."""

    def __init__(self, owner: str, repo: str, directory: str) -> None:
        self.owner = owner
        self.repo = repo
        self.url = "https://github.com/%s/%s.git" % (owner, repo)
        self.dir = os.path.abspath(directory)

    def _is_repo(self) -> bool:
        code, _, _ = _run_git(self.dir, ["rev-parse", "--git-dir"])
        return code == 0

    def clone_or_fetch(self) -> None:
        if not os.path.isdir(self.dir) or not self._is_repo():
            parent = os.path.dirname(self.dir)
            if parent:
                os.makedirs(parent, exist_ok=True)
            code, out, err = _run_git(
                parent,
                [
                    "clone", "--filter=blob:none", "--no-checkout",
                    self.url, self.dir,
                ],
            )
            if code != 0:
                raise RuntimeError(
                    "git clone failed for %s: %s" % (self.url, (err or out).strip())
                )
            return
        code, out, err = _run_git(
            self.dir, ["fetch", "--filter=blob:none", "origin"]
        )
        if code != 0:
            raise RuntimeError(
                "git fetch failed for %s: %s" % (self.url, (err or out).strip())
            )

    def list_config_files(self, glob_pattern: Optional[str] = None) -> List[str]:
        code, out, err = _run_git(self.dir, ["ls-tree", "-r", "--name-only", "HEAD"])
        if code != 0:
            raise RuntimeError("git ls-tree failed: %s" % (err or out).strip())
        files = []
        for line in out.splitlines():
            path = line.strip()
            if not path or not is_config_path(path):
                continue
            if glob_pattern and not fnmatch.fnmatch(path, glob_pattern):
                continue
            files.append(path)
        return sorted(files)

    def repo_log(self, since: Optional[str] = None) -> List[Dict[str, Any]]:
        return _repo_log(self.dir, since)

    def changed_files(self, commit: str,
                      glob_pattern: Optional[str] = None) -> List[str]:
        return _changed_files(self.dir, commit, glob_pattern)

    def show(self, commit: str, relpath: str) -> Optional[str]:
        return _show_file(self.dir, commit, relpath)


class LocalRepoSource(GitHistorySource):
    """A local git repository used directly (offline / CI-safe)."""

    def __init__(self, directory: str) -> None:
        self.dir = os.path.abspath(directory)

    def clone_or_fetch(self) -> None:
        if not os.path.isdir(self.dir):
            raise ValueError("local git repository not found: %s" % self.dir)
        code, _, err = _run_git(self.dir, ["rev-parse", "--git-dir"])
        if code != 0:
            raise ValueError(
                "%s is not a git repository: %s" % (self.dir, (err or "").strip())
            )

    def list_config_files(self, glob_pattern: Optional[str] = None) -> List[str]:
        code, out, err = _run_git(self.dir, ["ls-tree", "-r", "--name-only", "HEAD"])
        if code != 0:
            raise RuntimeError("git ls-tree failed: %s" % (err or out).strip())
        files = []
        for line in out.splitlines():
            path = line.strip()
            if not path or not is_config_path(path):
                continue
            if glob_pattern and not fnmatch.fnmatch(path, glob_pattern):
                continue
            files.append(path)
        return sorted(files)

    def repo_log(self, since: Optional[str] = None) -> List[Dict[str, Any]]:
        return _repo_log(self.dir, since)

    def changed_files(self, commit: str,
                      glob_pattern: Optional[str] = None) -> List[str]:
        return _changed_files(self.dir, commit, glob_pattern)

    def show(self, commit: str, relpath: str) -> Optional[str]:
        return _show_file(self.dir, commit, relpath)


# ---------------------------------------------------------------------------
# Shared git helpers
# ---------------------------------------------------------------------------

def _repo_log(directory: str, since: Optional[str] = None) -> List[Dict[str, Any]]:
    """Repo-wide ``git log`` (no merges), newest first."""
    args = [
        "log", "--no-merges",
        "--format=%H%x09%ct%x09%an <%ae>%x09%s",
    ]
    if since:
        args += ["--since=%s" % since]
    code, out, err = _run_git(directory, args)
    if code != 0:
        raise RuntimeError("git log failed: %s" % (err or out).strip())
    entries: List[Dict[str, Any]] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 3)
        if len(parts) < 4:
            continue
        commit, ts, author, subject = parts
        try:
            commit_time = datetime.fromtimestamp(
                int(ts), tz=timezone.utc
            ).isoformat()
        except (TypeError, ValueError, OverflowError):
            commit_time = ""
        entries.append(
            {
                "commit": commit,
                "time": commit_time,
                "author": author,
                "subject": subject,
            }
        )
    return entries


def _changed_files(directory: str, commit: str,
                   glob_pattern: Optional[str] = None) -> List[str]:
    """Files changed by ``commit`` (config whitelist + optional glob, sorted)."""
    code, out, err = _run_git(
        directory, ["show", "--name-only", "--format=", commit]
    )
    if code != 0:
        raise RuntimeError(
            "git show --name-only failed for %s: %s" % (commit, (err or out).strip())
        )
    files = []
    for line in out.splitlines():
        path = line.strip()
        if not path or not is_config_path(path):
            continue
        if glob_pattern and not fnmatch.fnmatch(path, glob_pattern):
            continue
        files.append(path)
    return sorted(files)


def _show_file(directory: str, commit: str, relpath: str) -> Optional[str]:
    """File content at ``commit:relpath``; None when unavailable."""
    code, out, err = _run_git(directory, ["show", "%s:%s" % (commit, relpath)])
    if code != 0:
        return None
    return out


# ---------------------------------------------------------------------------
# Star check (best-effort)
# ---------------------------------------------------------------------------

class GitHubApi:
    """Minimal GitHub REST client for the star check (Q1, urllib only)."""

    @staticmethod
    def fetch_repo(owner: str, repo: str, token: Optional[str] = None) -> Optional[dict]:
        """GET /repos/{owner}/{repo}; returns the JSON body or None on failure."""
        url = "https://api.github.com/repos/%s/%s" % (owner, repo)
        request = urllib.request.Request(
            url, headers={"User-Agent": "cfgdrift-corpus/0.7.0"}
        )
        if token:
            request.add_header("Authorization", "Bearer %s" % token)
        try:
            with urllib.request.urlopen(request, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - best-effort by contract
            logger.warning(
                "GitHub API star check failed for %s/%s (best-effort): %s",
                owner, repo, exc,
            )
            return None


# ---------------------------------------------------------------------------
# Change pair extraction
# ---------------------------------------------------------------------------

class ChangePairExtractor:
    """Extracts parse-validated (before, after) change pairs from git history."""

    def __init__(self) -> None:
        self.log = logger

    def extract_repo(
        self,
        source: GitHistorySource,
        since: Optional[str] = None,
        stop_at: Optional[str] = None,
        max_pairs: Optional[int] = None,
        glob_pattern: Optional[str] = None,
    ) -> Tuple[List[ChangePair], Dict[str, int], Optional[str]]:
        """Extract change pairs from a source, newest commits first.

        ``stop_at`` (optional) stops at a previously processed commit sha
        (incremental fetch).  ``max_pairs`` (optional) bounds the number of
        pairs returned.  Returns ``(pairs, stats, newest_processed_sha)`` —
        ``newest_processed_sha`` is the newest commit that was (at least
        partially) processed, used to update ``state.json``.
        """
        stats: Dict[str, int] = {
            "commits_seen": 0,
            "files_seen": 0,
            "parse_failures": 0,
            "pairs": 0,
        }
        pairs: List[ChangePair] = []
        newest_processed: Optional[str] = None
        commits = source.repo_log(since)
        for entry in commits:
            commit = entry["commit"]
            if stop_at and commit == stop_at:
                break
            stats["commits_seen"] += 1
            # The log is newest-first, so the first processed commit is the
            # newest; it doubles as the incremental ``last_commit`` boundary.
            if newest_processed is None:
                newest_processed = commit
            files = source.changed_files(commit, glob_pattern)
            for relpath in files:
                stats["files_seen"] += 1
                before = source.show("%s^" % commit, relpath)
                after = source.show(commit, relpath)
                if not before and not after:
                    continue
                fmt = fmt_for_path(relpath)
                if not (_parse_ok(before, fmt) and _parse_ok(after, fmt)):
                    stats["parse_failures"] += 1
                    continue
                pairs.append(
                    ChangePair(
                        relpath=relpath,
                        commit=commit,
                        commit_time=entry["time"],
                        author=entry["author"],
                        message=entry["subject"],
                        before_text=before if before is not None else None,
                        after_text=after if after is not None else None,
                    )
                )
                if max_pairs is not None and len(pairs) >= max_pairs:
                    stats["pairs"] = len(pairs)
                    return pairs, stats, newest_processed
        stats["pairs"] = len(pairs)
        return pairs, stats, newest_processed
