"""Alert data model, drift payload builder, fingerprint, and substitution utils.

This module is intentionally dependency-free inside the ``cfgdrift.alert``
package (zero intra-package imports) so it can be imported by
``channels`` / ``dispatcher`` / ``cli`` without creating circular imports.
It only depends on the shared engine model (:mod:`cfgdrift.core.model`) and the
standard library.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional

from ..core.model import Severity, to_jsonable

logger = logging.getLogger("cfgdrift.alert.models")


# ---------------------------------------------------------------------------
# Time helpers (ISO-8601 UTC, consistent with the rest of cfgdrift)
# ---------------------------------------------------------------------------

def utcnow_iso() -> str:
    """Current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Substitution helpers
# ---------------------------------------------------------------------------

_ENV_REF = re.compile(r"\{env:([A-Za-z_][A-Za-z0-9_]*)\}")
_CTX_REF = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def expand_env_vars(text: str, env: Optional[Mapping[str, str]] = None) -> str:
    """Replace ``{env:VAR}`` references with environment values.

    A missing variable becomes the empty string and logs a warning (the
    v0.3.0 substitution contract).  This keeps secrets out of ``alerts.yaml``:
    only the *name* of the environment variable is persisted.
    """
    env = env if env is not None else os.environ

    def repl(match: "re.Match[str]") -> str:
        var = match.group(1)
        value = env.get(var)
        if value is None:
            logger.warning(
                "environment variable %r referenced as {env:%s} is not set",
                var,
                var,
            )
            return ""
        return str(value)

    return _ENV_REF.sub(repl, text)


def substitute(
    text: str,
    ctx: Optional[Mapping[str, Any]] = None,
    env: Optional[Mapping[str, str]] = None,
) -> str:
    """Replace ``{env:VAR}`` references first, then context placeholders.

    Context placeholders (``{severity}`` / ``{baseline}`` / ``{target}`` /
    ``{summary}`` / ``{drift_count}`` / ``{version}``) are substituted from
    ``ctx``; unknown placeholders become the empty string and log a warning.
    """
    ctx = ctx if ctx is not None else {}
    out = expand_env_vars(text, env)

    def repl(match: "re.Match[str]") -> str:
        key = match.group(1)
        if key in ctx and ctx[key] is not None:
            return str(ctx[key])
        logger.warning("placeholder {%s} not found in substitution context", key)
        return ""

    return _CTX_REF.sub(repl, out)


def payload_context(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Derive a substitution context from a drift/test payload."""
    return {
        "event": payload.get("event", ""),
        "version": payload.get("version", ""),
        "timestamp": payload.get("timestamp", ""),
        "severity": payload.get("severity", ""),
        "baseline": payload.get("baseline", ""),
        "target": payload.get("target", ""),
        "drift_count": payload.get("drift_count", 0),
        "summary": payload.get("summary", ""),
    }


# ---------------------------------------------------------------------------
# AlertRule model
# ---------------------------------------------------------------------------

_VALID_TYPES = ("webhook", "email", "script")


@dataclass
class AlertRule:
    """One alert rule = one delivery channel.

    ``severity`` is the trigger threshold: a report is dispatched only when
    ``report.summary.max_severity.rank >= rule.severity.rank``.  ``baseline``
    scopes the rule to a single baseline (``None`` = all baselines).
    ``config`` holds the channel-specific settings (see ``alerts.yaml``
    schema in ``docs/system_design.md`` appendix B).
    """

    name: str
    type: str  # "webhook" | "email" | "script"
    severity: Severity = Severity.WARN
    baseline: Optional[str] = None
    enabled: bool = True
    config: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name or not isinstance(self.name, str):
            raise ValueError("alert rule name must be a non-empty string")
        if self.type not in _VALID_TYPES:
            raise ValueError(
                "invalid alert type %r (expected one of: %s)"
                % (self.type, ", ".join(_VALID_TYPES))
            )
        if not isinstance(self.severity, Severity):
            self.severity = Severity(self.severity)
        if not isinstance(self.config, dict):
            raise ValueError("alert rule config must be a mapping")

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "type": self.type,
            "severity": self.severity.value,
            "baseline": self.baseline,
            "enabled": self.enabled,
            "config": self.config,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AlertRule":
        """Build a rule from a raw ``alerts.yaml`` entry (validated)."""
        name = data.get("name")
        if not name or not isinstance(name, str):
            raise ValueError("alert rule is missing a non-empty 'name'")
        rule_type = data.get("type")
        if rule_type not in _VALID_TYPES:
            raise ValueError(
                "alert rule %r has invalid type %r (expected one of: %s)"
                % (name, rule_type, ", ".join(_VALID_TYPES))
            )
        severity_value = data.get("severity", "WARN")
        try:
            severity = Severity(str(severity_value).upper())
        except ValueError:
            raise ValueError(
                "alert rule %r has invalid severity %r"
                % (name, severity_value)
            ) from None
        config = data.get("config") or {}
        if not isinstance(config, dict):
            raise ValueError("alert rule %r config must be a mapping" % name)
        baseline = data.get("baseline")
        if baseline is not None and not isinstance(baseline, str):
            raise ValueError("alert rule %r baseline must be a string" % name)
        return cls(
            name=name,
            type=rule_type,
            severity=severity,
            baseline=baseline,
            enabled=bool(data.get("enabled", True)),
            config=config,
        )


# ---------------------------------------------------------------------------
# Drift fingerprint (dedupe key component)
# ---------------------------------------------------------------------------

def _item_signature(item: Any) -> tuple:
    """Extract ``(file, key_path, change_type)`` from a DriftItem or dict.

    The fingerprint deliberately excludes drift values so that value
    oscillation does not produce a new fingerprint (v0.3.0 decision Q3).
    """
    if isinstance(item, dict):
        return (
            str(item.get("file", "")),
            str(item.get("key_path", "")),
            str(item.get("change_type", "")),
        )
    change = item.change_type
    if hasattr(change, "value"):
        change = change.value
    return (
        str(getattr(item, "file", "")),
        str(getattr(item, "key_path", "")),
        str(change),
    )


def drift_fingerprint(baseline_name: str, target: str, items: List[Any]) -> str:
    """Return a stable sha256 fingerprint for a drift report.

    Fingerprint = sha256(canonical({baseline, target, items: sorted
    [(file, key_path, change_type)]})).  Only the drift signature is hashed,
    never the drift values.
    """
    signature = sorted(_item_signature(item) for item in items)
    canonical = json.dumps(
        {
            "baseline": baseline_name,
            "target": target,
            "items": signature,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Payload builder
# ---------------------------------------------------------------------------

def build_drift_payload(
    report,
    baseline_name: str,
    target: str,
    version: str,
) -> Dict[str, Any]:
    """Build the v0.3.0 drift alert payload (B.1.3).

    ``report`` is a :class:`cfgdrift.core.model.Report`.  The payload contains
    only the drift summary and per-item diffs — never channel credentials,
    full configuration content, or environment secrets.
    """
    severity = report.summary.max_severity
    items = []
    for item in report.items:
        items.append(
            {
                "key": item.key_path,
                "baseline": to_jsonable(item.old_value),
                "current": to_jsonable(item.new_value),
                "severity": item.severity.value,
                "file": item.file,
                "change_type": item.change_type.value,
            }
        )
    count = len(items)
    return {
        "event": "cfgdrift.drift",
        "version": version,
        "timestamp": utcnow_iso(),
        "severity": severity.value,
        "baseline": baseline_name,
        "target": target,
        "drift_count": count,
        "drift_items": items,
        "summary": "%d %s drift(s) in baseline %s"
        % (count, severity.value, baseline_name),
    }


def build_test_payload(version: str = "0.3.0") -> Dict[str, Any]:
    """Build a sample payload for ``alert test`` (event=cfgdrift.test)."""
    return {
        "event": "cfgdrift.test",
        "version": version,
        "timestamp": utcnow_iso(),
        "severity": "WARN",
        "baseline": "<test>",
        "target": "<test>",
        "drift_count": 1,
        "drift_items": [
            {
                "key": "test.key",
                "baseline": "old",
                "current": "new",
                "severity": "WARN",
                "file": "test.conf",
                "change_type": "modified",
            }
        ],
        "summary": "1 WARN drift(s) in baseline <test>",
    }
