"""constraints.yaml load / save / validate + rule CRUD + effective resolution (v0.6.0).

The file lives at ``<home>/constraints.yaml`` and holds user-defined
consistency constraints.  It mirrors the ``severity.yaml`` convention
(``version: 1`` + ``rules`` list, mode 0600 on POSIX).  Each entry is
validated by :meth:`Constraint.from_dict`; a corrupt file raises
``ValueError`` (the CLI surfaces it as exit code 2) so misconfiguration is
never silently ignored.

Effective constraint resolution (:func:`resolve`, D8)::

    built-in library (when ``builtin_enabled``)
    + <home>/constraints.yaml (when present)
    + every ``--constraints`` extra file (in order; missing file -> ValueError)

merged with *last-id-wins* so a user rule can override a built-in one of the
same ``id``.
"""

from __future__ import annotations

import logging
import os
from typing import List, Optional

import yaml

from ..core.constraints import BUILTIN_CONSTRAINTS
from ..core.model import Constraint

logger = logging.getLogger("cfgdrift.rules.constraints")

_CONSTRAINTS_CONFIG_VERSION = 1


def default_path(home: str) -> str:
    """Return the constraints.yaml path under a cfgdrift home directory."""
    return os.path.join(home, "constraints.yaml")


def _chmod_600(path: str) -> None:
    """Restrict the config file to the owning user (POSIX only)."""
    if os.name == "posix":
        try:
            os.chmod(path, 0o600)
        except OSError as exc:  # pragma: no cover - platform edge cases
            logger.warning("failed to chmod 0600 %s: %s", path, exc)


class ConstraintConfig:
    """Read/write access to the ``constraints.yaml`` rule file."""

    @staticmethod
    def load(path: str) -> List[Constraint]:
        """Load and validate all user constraints (empty list when absent)."""
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if not isinstance(data, dict):
            raise ValueError("constraints config must be a mapping at %s" % path)
        version = data.get("version")
        if version != _CONSTRAINTS_CONFIG_VERSION:
            raise ValueError(
                "unsupported constraints config version %r (expected %d)"
                % (version, _CONSTRAINTS_CONFIG_VERSION)
            )
        rules = data.get("rules") or []
        if not isinstance(rules, list):
            raise ValueError(
                "constraints config 'rules' must be a list at %s" % path
            )
        out: List[Constraint] = []
        for raw in rules:
            if not isinstance(raw, dict):
                raise ValueError(
                    "constraints config rules must be mappings at %s" % path
                )
            out.append(Constraint.from_dict(raw, source="user"))
        return out

    @staticmethod
    def save(path: str, constraints: List[Constraint]) -> None:
        """Persist constraints to ``constraints.yaml`` (creates parent dirs)."""
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        payload = {
            "version": _CONSTRAINTS_CONFIG_VERSION,
            "rules": [c.to_dict() for c in constraints],
        }
        with open(path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(payload, fh, allow_unicode=True, sort_keys=False)
        _chmod_600(path)

    @staticmethod
    def add_rule(path: str, constraint: Constraint) -> int:
        """Add a constraint (unique by id); returns its index in the list.

        Raises ``ValueError`` when a constraint with the same id already
        exists.
        """
        rules = ConstraintConfig.load(path)
        for existing in rules:
            if existing.id == constraint.id:
                raise ValueError(
                    "constraint %r already exists" % constraint.id
                )
        rules.append(constraint)
        ConstraintConfig.save(path, rules)
        return len(rules) - 1

    @staticmethod
    def remove_rule(path: str, constraint_id: str) -> None:
        """Remove a constraint by id; raises ``ValueError`` when not found."""
        rules = ConstraintConfig.load(path)
        kept = [r for r in rules if r.id != constraint_id]
        if len(kept) == len(rules):
            raise ValueError("constraint %r not found" % constraint_id)
        ConstraintConfig.save(path, kept)

    @staticmethod
    def set_enabled(path: str, constraint_id: str, enabled: bool) -> None:
        """Enable/disable a constraint by id; raises when absent."""
        rules = ConstraintConfig.load(path)
        found = False
        for rule in rules:
            if rule.id == constraint_id:
                rule.enabled = bool(enabled)
                found = True
                break
        if not found:
            raise ValueError("constraint %r not found" % constraint_id)
        ConstraintConfig.save(path, rules)

    @staticmethod
    def list_rules(path: str) -> List[Constraint]:
        """Return all user constraints (empty list when the file is absent)."""
        return ConstraintConfig.load(path)


def resolve(
    home: str,
    extra_paths: Optional[List[str]] = None,
    builtin_enabled: bool = True,
) -> List[Constraint]:
    """Resolve the effective constraint list (D8).

    Order: built-in library (if enabled) -> ``<home>/constraints.yaml`` (if
    present) -> each ``--constraints`` extra file in order.  Later entries
    with the same ``id`` override earlier ones (dedupe by id, last wins).

    Raises ``ValueError`` when an explicitly-given extra file is missing (a
    configuration error, surfaced as exit code 2).
    """
    constraints: List[Constraint] = []
    if builtin_enabled:
        constraints.extend(BUILTIN_CONSTRAINTS)
    cfg_path = default_path(home)
    if os.path.exists(cfg_path):
        constraints.extend(ConstraintConfig.load(cfg_path))
    for extra in extra_paths or []:
        if not os.path.exists(extra):
            raise ValueError("constraints file not found: %s" % extra)
        constraints.extend(ConstraintConfig.load(extra))
    by_id: dict = {}
    for constraint in constraints:
        by_id[constraint.id] = constraint
    return list(by_id.values())
