"""severity.yaml load / save / validate + rule CRUD (v0.4.0).

The file lives at ``<home>/severity.yaml`` and holds user-defined severity
override rules.  Rules are applied by the differ *after* the built-in default
classification (first-match-wins, file order) so ``summary.max_severity``
reflects the overrides automatically and alert thresholds keep working with
zero changes.

Schema::

    version: 1
    rules:
      - name: tls-critical
        enabled: true
        severity: CRITICAL
        change_type: modified          # optional regex/equality filter
        key_pattern: '.*tls\\.enabled'  # optional regex on key_path
        value_pattern: null            # optional regex on old/new values
        file_pattern: null             # optional regex on file relpath

The model (:class:`cfgdrift.core.model.SeverityRule`) validates each entry at
load time; a corrupt file raises ``ValueError`` (the CLI surfaces it as exit
code 2) so misconfiguration is never silently ignored.
"""

from __future__ import annotations

import logging
import os
import re
from typing import List, Optional

import yaml

from ..core.model import Severity, SeverityRule

logger = logging.getLogger("cfgdrift.rules.severity")

_SEVERITY_CONFIG_VERSION = 1

_VALID_SEVERITIES = ("CRITICAL", "WARN", "INFO", "NONE")


def default_path(home: str) -> str:
    """Return the severity.yaml path under a cfgdrift home directory."""
    return os.path.join(home, "severity.yaml")


def _chmod_600(path: str) -> None:
    """Restrict the config file to the owning user (POSIX only)."""
    if os.name == "posix":
        try:
            os.chmod(path, 0o600)
        except OSError as exc:  # pragma: no cover - platform edge cases
            logger.warning("failed to chmod 0600 %s: %s", path, exc)


class SeverityConfig:
    """Read/write access to the ``severity.yaml`` rule file."""

    @staticmethod
    def load(path: str) -> List[SeverityRule]:
        """Load and validate all severity rules (empty list when absent)."""
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if not isinstance(data, dict):
            raise ValueError("severity config must be a mapping at %s" % path)
        version = data.get("version")
        if version != _SEVERITY_CONFIG_VERSION:
            raise ValueError(
                "unsupported severity config version %r (expected %d)"
                % (version, _SEVERITY_CONFIG_VERSION)
            )
        rules = data.get("rules") or []
        if not isinstance(rules, list):
            raise ValueError("severity config 'rules' must be a list at %s" % path)
        out: List[SeverityRule] = []
        for raw in rules:
            if not isinstance(raw, dict):
                raise ValueError("severity config rules must be mappings at %s" % path)
            out.append(SeverityRule.from_dict(raw))
        return out

    @staticmethod
    def save(path: str, rules: List[SeverityRule]) -> None:
        """Persist rules to ``severity.yaml`` (creates parent dirs, mode 0600)."""
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        payload = {
            "version": _SEVERITY_CONFIG_VERSION,
            "rules": [rule.to_dict() for rule in rules],
        }
        with open(path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(payload, fh, allow_unicode=True, sort_keys=False)
        _chmod_600(path)

    @staticmethod
    def add_rule(path: str, rule: SeverityRule) -> int:
        """Add a rule (unique by name); returns its index in the list.

        Raises ``ValueError`` when a rule with the same name already exists.
        """
        rules = SeverityConfig.load(path)
        for existing in rules:
            if existing.name == rule.name:
                raise ValueError("severity rule %r already exists" % rule.name)
        rules.append(rule)
        SeverityConfig.save(path, rules)
        return len(rules) - 1

    @staticmethod
    def remove_rule(path: str, name: str) -> None:
        """Remove a rule by name; raises ``ValueError`` when not found."""
        rules = SeverityConfig.load(path)
        kept = [r for r in rules if r.name != name]
        if len(kept) == len(rules):
            raise ValueError("severity rule %r not found" % name)
        SeverityConfig.save(path, kept)

    @staticmethod
    def set_enabled(path: str, name: str, enabled: bool) -> None:
        """Enable/disable a rule by name; raises ``ValueError`` when absent."""
        rules = SeverityConfig.load(path)
        found = False
        for rule in rules:
            if rule.name == name:
                rule.enabled = bool(enabled)
                found = True
                break
        if not found:
            raise ValueError("severity rule %r not found" % name)
        SeverityConfig.save(path, rules)

    @staticmethod
    def list_rules(path: str) -> List[SeverityRule]:
        """Return all rules (empty list when the file does not exist)."""
        return SeverityConfig.load(path)


def _validate_regex(field: str, pattern: Optional[str]) -> None:
    """Validate that an optional pattern field is a compilable regex.

    ``None`` and the empty string are treated as "not set" and skipped; any
    other value must compile with :func:`re.compile` or a ``ValueError`` is
    raised (the CLI surfaces it as exit code 2) so an invalid regex is never
    silently accepted at rule-construction time.  Runtime matching in
    :meth:`SeverityRule.matches` keeps its existing try/except fallback.
    """
    if pattern is None or pattern == "":
        return
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ValueError(
            "invalid regex for %s: %s" % (field, exc)
        ) from None


def make_rule(
    name: str,
    severity: str,
    change_type: Optional[str] = None,
    key_pattern: Optional[str] = None,
    value_pattern: Optional[str] = None,
    file_pattern: Optional[str] = None,
    constraint_id: Optional[object] = None,  # v0.8.0: str | List[str]
    enabled: bool = True,
) -> SeverityRule:
    """Construct a validated :class:`SeverityRule` (CLI helper).

    ``constraint_id`` (v0.8.0, optional) is a single constraint id string or
    a list of them; it is normalized to ``List[str]`` by the model.
    """
    if severity not in _VALID_SEVERITIES:
        raise ValueError(
            "invalid severity %r (expected one of: %s)"
            % (severity, ", ".join(_VALID_SEVERITIES))
        )
    if change_type is not None and change_type not in (
        "added",
        "removed",
        "modified",
        "type_changed",
    ):
        raise ValueError(
            "invalid change_type %r (expected one of: added, removed, "
            "modified, type_changed)" % change_type
        )
    # Reject invalid regexes up front (exit 2) instead of accepting them and
    # silently never matching at diff time.
    _validate_regex("key_pattern", key_pattern)
    _validate_regex("value_pattern", value_pattern)
    _validate_regex("file_pattern", file_pattern)
    normalized_constraint_id: Optional[List[str]] = None
    if constraint_id is not None:
        if isinstance(constraint_id, str):
            normalized_constraint_id = [constraint_id]
        elif isinstance(constraint_id, (list, tuple)):
            normalized_constraint_id = [str(c) for c in constraint_id]
        else:
            raise ValueError(
                "constraint_id must be a string or a list of strings"
            )
    return SeverityRule(
        name=name,
        severity=Severity(severity),
        change_type=change_type,
        key_pattern=key_pattern,
        value_pattern=value_pattern,
        file_pattern=file_pattern,
        constraint_id=normalized_constraint_id,
        enabled=enabled,
    )
