"""corpus.yaml configuration (v0.7.0).

Schema (version 1)::

    version: 1
    since: "2023-01-01"            # optional global ISO date filter
    min_stars: 1000                # optional GitHub star floor (best-effort)
    max_instances: 200             # optional; default 200 (global cap)
    token: ""                      # optional GitHub token (GITHUB_TOKEN env wins)
    repositories:
      - owner: nginx
        repo: nginx
        glob: "conf/*.conf"        # optional glob over repo-relative paths
        since: "2022-01-01"        # optional repo-level since (overrides global)
        local_path: ""             # optional local git repo (offline; skips clone/API)

A corrupt file raises :class:`ValueError` (the CLI surfaces it as exit code 2,
matching severity.yaml / constraints.yaml).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger("cfgdrift.corpus.config")

_CORPUS_CONFIG_VERSION = 1

#: File-type whitelist supported by ``parse_text`` natively.
CONFIG_EXTENSIONS = (".json", ".yaml", ".yml", ".toml", ".ini")

#: Extension -> parse_text format (yml normalized to yaml).
EXTENSION_FORMATS = {
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".ini": "ini",
}


def fmt_for_path(relpath: str) -> Optional[str]:
    """Return the parse_text format for a repo-relative path (None if unknown)."""
    _, ext = os.path.splitext(relpath)
    return EXTENSION_FORMATS.get(ext.lower())


def is_config_path(relpath: str) -> bool:
    """Return True when ``relpath`` matches the five-type config whitelist."""
    _, ext = os.path.splitext(relpath)
    return ext.lower() in CONFIG_EXTENSIONS


@dataclass
class CorpusRepository:
    """One repository entry in corpus.yaml."""

    owner: str = ""
    repo: str = ""
    glob: Optional[str] = None
    since: Optional[str] = None
    local_path: Optional[str] = None

    @property
    def key(self) -> str:
        """Stable state key ``owner/repo`` (falls back to a path-derived key)."""
        if self.owner and self.repo:
            return "%s/%s" % (self.owner, self.repo)
        if self.local_path:
            return os.path.basename(os.path.normpath(self.local_path))
        return "unknown"

    def is_local(self) -> bool:
        return bool(self.local_path)

    def validate(self) -> None:
        """Validate one repository entry; raises ``ValueError`` when corrupt."""
        if not self.is_local() and (not self.owner or not self.repo):
            raise ValueError(
                "corpus repository requires 'owner' + 'repo' or 'local_path'"
            )
        if self.glob is not None and not isinstance(self.glob, str):
            raise ValueError("corpus repository 'glob' must be a string")
        if self.since is not None and not isinstance(self.since, str):
            raise ValueError(
                "corpus repository 'since' must be an ISO date string"
            )

    def to_dict(self) -> dict:
        out: Dict[str, Any] = {}
        if self.owner:
            out["owner"] = self.owner
        if self.repo:
            out["repo"] = self.repo
        if self.glob:
            out["glob"] = self.glob
        if self.since:
            out["since"] = self.since
        if self.local_path:
            out["local_path"] = self.local_path
        return out

    @classmethod
    def from_dict(cls, data: dict, index: int = 0) -> "CorpusRepository":
        if not isinstance(data, dict):
            raise ValueError(
                "corpus.yaml repositories[%d] must be a mapping" % index
            )
        owner = str(data.get("owner", "") or "").strip()
        repo = str(data.get("repo", "") or "").strip()
        local_path = str(data.get("local_path", "") or "").strip() or None
        if not local_path and (not owner or not repo):
            raise ValueError(
                "corpus.yaml repositories[%d] requires 'owner' + 'repo' "
                "or 'local_path'" % index
            )
        glob_pattern = data.get("glob")
        if glob_pattern is not None and not isinstance(glob_pattern, str):
            raise ValueError(
                "corpus.yaml repositories[%d] 'glob' must be a string" % index
            )
        since = data.get("since")
        if since is not None and not isinstance(since, str):
            raise ValueError(
                "corpus.yaml repositories[%d] 'since' must be an ISO date "
                "string" % index
            )
        return cls(
            owner=owner,
            repo=repo,
            glob=glob_pattern,
            since=since,
            local_path=local_path,
        )


@dataclass
class CorpusConfig:
    """Parsed + validated corpus.yaml."""

    version: int = _CORPUS_CONFIG_VERSION
    since: Optional[str] = None
    min_stars: Optional[int] = None
    max_instances: int = 200
    token: str = ""
    repositories: List[CorpusRepository] = field(default_factory=list)

    def effective_token(self) -> str:
        """Token precedence: GITHUB_TOKEN env > corpus.yaml token (Q1)."""
        env_token = os.environ.get("GITHUB_TOKEN", "").strip()
        if env_token:
            return env_token
        return (self.token or "").strip()

    def effective_since(self, repo: CorpusRepository) -> Optional[str]:
        """Repo-level since overrides the global one."""
        return repo.since or self.since

    def validate(self) -> None:
        """Validate the configuration; raises ``ValueError`` when corrupt."""
        if self.version != _CORPUS_CONFIG_VERSION:
            raise ValueError(
                "unsupported corpus config version %r (expected %d)"
                % (self.version, _CORPUS_CONFIG_VERSION)
            )
        if self.since is not None and not isinstance(self.since, str):
            raise ValueError("corpus.yaml 'since' must be an ISO date string")
        if self.min_stars is not None:
            if not isinstance(self.min_stars, (int, float)) or isinstance(
                self.min_stars, bool
            ):
                raise ValueError("corpus.yaml 'min_stars' must be a number")
            if self.min_stars < 0:
                raise ValueError("corpus.yaml 'min_stars' must be >= 0")
        if not isinstance(self.max_instances, (int, float)) or isinstance(
            self.max_instances, bool
        ):
            raise ValueError("corpus.yaml 'max_instances' must be an integer")
        self.max_instances = int(self.max_instances)
        if self.max_instances <= 0:
            raise ValueError("corpus.yaml 'max_instances' must be > 0")
        if not isinstance(self.repositories, list):
            raise ValueError("corpus.yaml 'repositories' must be a list")
        for repo in self.repositories:
            repo.validate()

    @staticmethod
    def default_path(workspace: str) -> str:
        """Return the corpus.yaml path inside a workspace."""
        return os.path.join(workspace, "corpus.yaml")

    @staticmethod
    def load(path: str) -> "CorpusConfig":
        """Load + validate corpus.yaml; raises ``ValueError`` when corrupt."""
        if not os.path.exists(path):
            raise ValueError("corpus config not found: %s" % path)
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if not isinstance(data, dict):
            raise ValueError(
                "corpus config must be a mapping at %s" % path
            )
        version = data.get("version")
        if version != _CORPUS_CONFIG_VERSION:
            raise ValueError(
                "unsupported corpus config version %r (expected %d)"
                % (version, _CORPUS_CONFIG_VERSION)
            )
        raw_repos = data.get("repositories") or []
        if not isinstance(raw_repos, list):
            raise ValueError("corpus config 'repositories' must be a list at %s" % path)
        repositories = [
            CorpusRepository.from_dict(raw, i) for i, raw in enumerate(raw_repos)
        ]
        cfg = CorpusConfig(
            version=version,
            since=data.get("since"),
            min_stars=data.get("min_stars"),
            max_instances=int(data.get("max_instances", 200)),
            token=str(data.get("token", "") or ""),
            repositories=repositories,
        )
        cfg.validate()
        return cfg

    def save(self, path: str) -> None:
        """Persist the configuration (creates parent dirs)."""
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        payload: Dict[str, Any] = {
            "version": self.version,
            "max_instances": self.max_instances,
        }
        if self.since:
            payload["since"] = self.since
        if self.min_stars is not None:
            payload["min_stars"] = self.min_stars
        if self.token:
            payload["token"] = self.token
        payload["repositories"] = [r.to_dict() for r in self.repositories]
        with open(path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(payload, fh, allow_unicode=True, sort_keys=False)

    @classmethod
    def template(cls, workspace: str) -> "CorpusConfig":
        """Return a starter configuration for ``corpus init``."""
        return cls(
            version=_CORPUS_CONFIG_VERSION,
            since="2023-01-01",
            min_stars=1000,
            max_instances=200,
            token="",
            repositories=[
                CorpusRepository(owner="nginx", repo="nginx", glob="conf/*.conf"),
                CorpusRepository(owner="prometheus", repo="prometheus"),
            ],
        )
