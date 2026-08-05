"""alerts.yaml load / save / validate + rule CRUD.

The config file lives at ``<home>/alerts.yaml`` (separate from the main
configuration).  On POSIX it is written with mode ``0600`` as a defense in
depth for secrets referenced via ``{env:VAR}``.
"""

from __future__ import annotations

import logging
import os
from typing import List

import yaml

from .models import AlertRule, parse_iso_utc

logger = logging.getLogger("cfgdrift.alert.config")

_CONFIG_VERSION = 1

# Per-channel required configuration keys (B.6.1).
_REQUIRED_CONFIG = {
    "webhook": ("url",),
    "email": ("smtp_host", "smtp_port", "smtp_from", "smtp_to"),
    "script": ("command",),
}


def _chmod_600(path: str) -> None:
    """Restrict the config file to the owning user (POSIX only)."""
    if os.name == "posix":
        try:
            os.chmod(path, 0o600)
        except OSError as exc:  # pragma: no cover - platform edge cases
            logger.warning("failed to chmod 0600 %s: %s", path, exc)


class AlertConfig:
    """Read/write access to the ``alerts.yaml`` rule file."""

    @staticmethod
    def default_path(home: str) -> str:
        """Return the default alerts config path under ``home``."""
        return os.path.join(home, "alerts.yaml")

    @staticmethod
    def load(path: str) -> List[AlertRule]:
        """Load and validate all alert rules.

        Unknown rule types / missing required fields raise ``ValueError`` at
        load time (never silently ignored) so ``alert list`` and daemon
        startup surface configuration errors with exit code 2.
        """
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if not isinstance(data, dict):
            raise ValueError("alerts config must be a mapping at %s" % path)
        version = data.get("version")
        if version != _CONFIG_VERSION:
            raise ValueError(
                "unsupported alerts config version %r (expected %d)"
                % (version, _CONFIG_VERSION)
            )
        rules = data.get("rules") or []
        if not isinstance(rules, list):
            raise ValueError("alerts config 'rules' must be a list at %s" % path)
        out: List[AlertRule] = []
        for raw in rules:
            rule = AlertRule.from_dict(raw)
            AlertConfig._validate_required(rule)
            out.append(rule)
        return out

    @staticmethod
    def _validate_required(rule: AlertRule) -> None:
        """Validate per-type required configuration fields."""
        for key in _REQUIRED_CONFIG.get(rule.type, ()):
            value = rule.config.get(key)
            if value is None or value == "":
                raise ValueError(
                    "alert rule %r (%s) is missing required config %r"
                    % (rule.name, rule.type, key)
                )
            if key == "smtp_to" and isinstance(value, (list, tuple)) and not value:
                raise ValueError(
                    "alert rule %r (%s) requires at least one smtp_to address"
                    % (rule.name, rule.type)
                )
            if key == "smtp_port":
                try:
                    int(value)
                except (TypeError, ValueError):
                    raise ValueError(
                        "alert rule %r smtp_port must be an integer" % rule.name
                    ) from None

    @staticmethod
    def save(path: str, rules: List[AlertRule]) -> None:
        """Persist rules to ``alerts.yaml`` (creates parent dirs, mode 0600)."""
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        payload = {
            "version": _CONFIG_VERSION,
            "rules": [rule.to_dict() for rule in rules],
        }
        with open(path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(payload, fh, allow_unicode=True, sort_keys=False)
        _chmod_600(path)

    @staticmethod
    def add_rule(path: str, rule: AlertRule) -> int:
        """Add a rule (unique by name); returns its index in the list.

        Raises ``ValueError`` when a rule with the same name already exists.
        """
        rules = AlertConfig.load(path)
        for existing in rules:
            if existing.name == rule.name:
                raise ValueError("alert rule %r already exists" % rule.name)
        AlertConfig._validate_required(rule)
        rules.append(rule)
        AlertConfig.save(path, rules)
        return len(rules) - 1

    @staticmethod
    def remove_rule(path: str, name: str) -> None:
        """Remove a rule by name; raises ``ValueError`` when not found."""
        rules = AlertConfig.load(path)
        kept = [r for r in rules if r.name != name]
        if len(kept) == len(rules):
            raise ValueError("alert rule %r not found" % name)
        AlertConfig.save(path, kept)

    @staticmethod
    def list_rules(path: str) -> List[AlertRule]:
        """Return all rules (empty list when the file does not exist)."""
        return AlertConfig.load(path)

    @staticmethod
    def set_enabled(path: str, name: str, enabled: bool) -> None:
        """Enable/disable a rule by name; raises ``ValueError`` when absent.

        Mirrors ``SeverityConfig.set_enabled`` / ``ConstraintConfig.set_enabled``
        (v0.9.0, D6): load -> toggle -> save, so the Web PUT endpoint and the
        CLI ``alert enable/disable`` commands share one write path.
        """
        rules = AlertConfig.load(path)
        found = False
        for rule in rules:
            if rule.name == name:
                rule.enabled = bool(enabled)
                found = True
                break
        if not found:
            raise ValueError("alert rule %r not found" % name)
        AlertConfig.save(path, rules)

    @staticmethod
    def set_mute(path: str, name: str, until: str) -> None:
        """Mute a rule until the given ISO-8601 UTC timestamp (v0.10.0).

        Mirrors ``set_enabled``'s load -> mutate -> save write path so the
        Web PUT endpoint and the CLI ``alert mute`` share one code path.
        ``until`` is validated + normalized by :func:`AlertRule.__post_init__`
        (raises ``ValueError`` for malformed timestamps); unknown rules raise
        ``ValueError`` as well.
        """
        rules = AlertConfig.load(path)
        found = False
        for rule in rules:
            if rule.name == name:
                # Validate + normalize before persisting (raises ValueError
                # for malformed timestamps, D3).
                rule.mute_until = parse_iso_utc(until)
                found = True
                break
        if not found:
            raise ValueError("alert rule %r not found" % name)
        AlertConfig.save(path, rules)

    @staticmethod
    def clear_mute(path: str, name: str) -> None:
        """Remove a rule's mute window (v0.10.0); unknown rule -> ValueError."""
        rules = AlertConfig.load(path)
        found = False
        for rule in rules:
            if rule.name == name:
                rule.mute_until = None
                found = True
                break
        if not found:
            raise ValueError("alert rule %r not found" % name)
        AlertConfig.save(path, rules)
