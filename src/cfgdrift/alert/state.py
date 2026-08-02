"""Alert dedupe / cooldown state persisted to ``alert_state.json``.

This is the v0.3.0 anti-spam store: it keeps one entry per dispatched
``(rule.name, drift_fingerprint)`` with the cooldown window (default 10 min)
so the same drift is not re-sent on every scan cycle.  Success and failure
both write a cooldown; the state file is pruned of entries older than 24h on
load so it cannot grow unboundedly.  A corrupt file is rebuilt as an empty
state (with a warning) — it never blocks the daemon.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger("cfgdrift.alert.state")

_STATE_VERSION = 1
_DEFAULT_COOLDOWN_SECONDS = 600
_PRUNE_HOURS = 24


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _add_seconds(iso: str, seconds: float) -> str:
    return (_parse_iso(iso) + timedelta(seconds=seconds)).isoformat()


class AlertStateStore:
    """JSON-backed store for alert dedupe / cooldown state."""

    def __init__(self, path: str, cooldown_seconds: int = _DEFAULT_COOLDOWN_SECONDS) -> None:
        self.path = path
        self.cooldown_seconds = int(cooldown_seconds)
        self._entries: Dict[str, Dict[str, Any]] = {}
        self.load()

    # -- persistence ------------------------------------------------------

    def load(self) -> None:
        """Load entries; prune entries older than 24h; rebuild on corruption."""
        self._entries = {}
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                doc = json.load(fh)
            entries = doc.get("entries") or {}
            if not isinstance(entries, dict):
                raise ValueError("'entries' must be a mapping")
            for key, entry in entries.items():
                if isinstance(entry, dict):
                    self._entries[str(key)] = entry
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            logger.warning(
                "alert_state.json is corrupt (%s); rebuilding empty state", exc
            )
            self._entries = {}
        self.prune(older_than_hours=_PRUNE_HOURS)

    def save(self) -> None:
        """Atomically persist the current entries."""
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        doc = {"version": _STATE_VERSION, "entries": self._entries}
        tmp_path = self.path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
        os.replace(tmp_path, self.path)

    # -- key helpers ------------------------------------------------------

    @staticmethod
    def key_for(rule_name: str, fingerprint: str) -> str:
        """Dedupe key = sha256(rule.name + ':' + fingerprint)."""
        raw = "%s:%s" % (rule_name, fingerprint)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    # -- query ------------------------------------------------------------

    def is_suppressed(self, key: str, now: Optional[str] = None) -> bool:
        """Return True when ``now`` is inside the cooldown window."""
        entry = self._entries.get(key)
        if entry is None:
            return False
        suppress_until = entry.get("suppress_until")
        if not suppress_until:
            return False
        try:
            return _parse_iso(now or _utcnow_iso()) < _parse_iso(suppress_until)
        except ValueError:
            return False

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        """Return a raw entry (tests / diagnostics)."""
        entry = self._entries.get(key)
        return dict(entry) if entry is not None else None

    def entries(self) -> Dict[str, Dict[str, Any]]:
        """Return a shallow copy of all entries."""
        return dict(self._entries)

    # -- records ----------------------------------------------------------

    def record_success(
        self,
        key: str,
        meta: Dict[str, Any],
        cooldown: Optional[int] = None,
    ) -> None:
        """Record a successful send and arm the cooldown window."""
        window = self.cooldown_seconds if cooldown is None else int(cooldown)
        now = _utcnow_iso()
        entry = dict(meta)
        entry["last_attempt_at"] = now
        entry["last_success_at"] = now
        entry["last_status"] = "sent"
        entry["attempts"] = int(meta.get("attempts", 1))
        entry["suppress_until"] = _add_seconds(now, window)
        self._entries[key] = entry
        self.save()

    def record_failure(self, key: str, meta: Dict[str, Any]) -> None:
        """Record a failed send; same cooldown window to avoid log spam."""
        now = _utcnow_iso()
        entry = dict(meta)
        entry["last_attempt_at"] = now
        entry["last_success_at"] = meta.get("last_success_at")
        entry["last_status"] = "failed"
        entry["attempts"] = int(meta.get("attempts", 1))
        entry["suppress_until"] = _add_seconds(now, self.cooldown_seconds)
        self._entries[key] = entry
        self.save()

    # -- maintenance ------------------------------------------------------

    def prune(self, older_than_hours: int = _PRUNE_HOURS) -> int:
        """Drop entries whose last attempt is older than ``older_than_hours``.

        Returns the number of removed entries.
        """
        cutoff = _add_seconds(_utcnow_iso(), -older_than_hours * 3600)
        removed = 0
        for key in list(self._entries):
            entry = self._entries[key]
            ts = entry.get("last_attempt_at")
            if not ts:
                del self._entries[key]
                removed += 1
                continue
            try:
                if _parse_iso(ts) < _parse_iso(cutoff):
                    del self._entries[key]
                    removed += 1
            except ValueError:
                del self._entries[key]
                removed += 1
        if removed:
            self.save()
        return removed
