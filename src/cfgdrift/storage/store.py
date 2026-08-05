"""SQLite repository: baselines (versioned), scan history, ignore rules."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from ..core.model import Baseline, IgnoreRule
from ..rules.ignore import rule_from_row


def utcnow_iso() -> str:
    """Current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS baselines (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    version     INTEGER NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    scan_root   TEXT NOT NULL,
    format      TEXT NOT NULL DEFAULT 'auto',
    data        TEXT NOT NULL,
    UNIQUE(name, version)
);

CREATE TABLE IF NOT EXISTS scans (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    baseline_id  INTEGER,
    mode         TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    added        INTEGER NOT NULL DEFAULT 0,
    removed      INTEGER NOT NULL DEFAULT 0,
    modified     INTEGER NOT NULL DEFAULT 0,
    type_changed INTEGER NOT NULL DEFAULT 0,
    ignored      INTEGER NOT NULL DEFAULT 0,
    total        INTEGER NOT NULL DEFAULT 0,
    max_severity TEXT NOT NULL DEFAULT 'NONE',
    report_json  TEXT NOT NULL,
    FOREIGN KEY (baseline_id) REFERENCES baselines(id)
);

CREATE TABLE IF NOT EXISTS scan_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id     INTEGER NOT NULL,
    key_path    TEXT NOT NULL,
    change_type TEXT NOT NULL,
    severity    TEXT NOT NULL,
    file        TEXT NOT NULL,
    old_value   TEXT,
    new_value   TEXT,
    old_type    TEXT,
    new_type    TEXT,
    rule_id     INTEGER,
    FOREIGN KEY (scan_id) REFERENCES scans(id)
);

CREATE TABLE IF NOT EXISTS ignore_rules (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    baseline_id  INTEGER,
    name         TEXT NOT NULL,
    key_pattern  TEXT NOT NULL,
    match_type   TEXT NOT NULL,
    file_pattern TEXT,
    change_type  TEXT,
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL,
    FOREIGN KEY (baseline_id) REFERENCES baselines(id)
);

-- v0.4.0: alert delivery events (sent/failed only; cooldown-suppressed and
-- connectivity-test sends are never recorded).
CREATE TABLE IF NOT EXISTS alert_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    rule        TEXT NOT NULL,
    baseline    TEXT NOT NULL,
    severity    TEXT NOT NULL,
    status      TEXT NOT NULL,
    target      TEXT,
    drift_count INTEGER NOT NULL DEFAULT 0,
    error       TEXT,
    attempts    INTEGER NOT NULL DEFAULT 1,
    fingerprint TEXT,
    created_at  TEXT NOT NULL,
    -- v0.9.0 (D4): retry bookkeeping — ``retried=1`` marks a new event that
    -- was produced by retrying an earlier event (``retried_from`` = its id).
    retried      INTEGER NOT NULL DEFAULT 0,
    retried_from INTEGER
);

CREATE INDEX IF NOT EXISTS idx_scans_created ON scans(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_scans_severity ON scans(max_severity);
CREATE INDEX IF NOT EXISTS idx_scans_mode ON scans(mode);
CREATE INDEX IF NOT EXISTS idx_scan_items_scan ON scan_items(scan_id);
CREATE INDEX IF NOT EXISTS idx_alert_events_created ON alert_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_alert_events_rule ON alert_events(rule);
CREATE INDEX IF NOT EXISTS idx_alert_events_status ON alert_events(status);

-- v0.7.0: consistency-constraint violations (drift + baseline kinds).
CREATE TABLE IF NOT EXISTS constraint_violations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    constraint_id TEXT NOT NULL,
    scan_id       INTEGER,
    kind          TEXT NOT NULL DEFAULT 'drift',   -- drift | baseline
    file          TEXT NOT NULL DEFAULT '',
    keys          TEXT NOT NULL DEFAULT '[]',      -- JSON array (involved_keys)
    severity      TEXT NOT NULL DEFAULT 'WARN',
    detail        TEXT NOT NULL DEFAULT '',        -- violation message
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cv_created ON constraint_violations(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_cv_constraint ON constraint_violations(constraint_id);
CREATE INDEX IF NOT EXISTS idx_cv_scan ON constraint_violations(scan_id);
"""

# Lazy pruning is triggered every N alert-event inserts (v0.4.0).
_ALERT_EVENT_PRUNE_EVERY = 100
_ALERT_EVENT_RETENTION_DAYS = 30
_ALERT_EVENT_MAX_ROWS = 5000

# v0.7.0: constraint-violation retention (Q4) — 90 days by default, configurable
# through CFGDRIFT_CV_RETENTION_DAYS, lazy prune every N inserts, hard row cap.
_CV_PRUNE_EVERY = 200
_CV_RETENTION_DAYS = 90
_CV_MAX_ROWS = 20000


class Store:
    """SQLite-backed repository for cfgdrift state."""

    def __init__(self, db_path: str) -> None:
        self.db_path = os.path.abspath(db_path)
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        # check_same_thread=False：允许 FastAPI 线程池中的同步端点访问同一连接
        # （本地单用户仪表盘场景）；timeout 让并发写等待 sqlite 文件锁而非立即失败。
        self._conn = sqlite3.connect(
            self.db_path, check_same_thread=False, timeout=5.0
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        # v0.4.0: lazy alert-event pruning counter (prune every N inserts).
        self._alert_insert_count = 0
        # v0.7.0: lazy constraint-violation pruning counter (prune every N inserts).
        self._cv_insert_count = 0
        self.init_schema()

    # -- schema -----------------------------------------------------------

    def init_schema(self) -> None:
        self._conn.executescript(_SCHEMA)
        # Idempotent migration for the v0.4.0 ``line_maps`` column on existing
        # databases (guarded by PRAGMA table_info so re-running is a no-op).
        columns = [
            r["name"] for r in self._conn.execute("PRAGMA table_info(baselines)")
        ]
        if "line_maps" not in columns:
            self._conn.execute("ALTER TABLE baselines ADD COLUMN line_maps TEXT")
        # v0.9.0 (D4): idempotent migration for the alert_events ``retried`` /
        # ``retried_from`` columns on pre-v0.9.0 databases.
        alert_columns = [
            r["name"]
            for r in self._conn.execute("PRAGMA table_info(alert_events)")
        ]
        if "retried" not in alert_columns:
            self._conn.execute(
                "ALTER TABLE alert_events ADD COLUMN retried "
                "INTEGER NOT NULL DEFAULT 0"
            )
        if "retried_from" not in alert_columns:
            self._conn.execute(
                "ALTER TABLE alert_events ADD COLUMN retried_from INTEGER"
            )
        self._conn.commit()

    @staticmethod
    def _row_get(row: sqlite3.Row, key: str, default: Any = None) -> Any:
        """Access a Row column defensively (columns may be absent pre-migration)."""
        try:
            return row[key]
        except (IndexError, KeyError):
            return default

    def close(self) -> None:
        self._conn.close()

    # -- baselines --------------------------------------------------------

    def create_baseline(
        self,
        name: str,
        description: str,
        scan_root: str,
        format: str,
        data: dict,
        line_maps: Optional[dict] = None,
    ) -> Baseline:
        """Create a new baseline version (same name -> version + 1).

        ``line_maps`` (v0.4.0) is an optional ``{relpath: {key_path: line}}``
        map captured when the baseline was created; it is persisted as JSON so
        REMOVED drift items can fall back to old-side line numbers.
        """
        row = self._conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM baselines WHERE name = ?",
            (name,),
        ).fetchone()
        version = int(row["v"]) + 1
        created = utcnow_iso()
        cur = self._conn.execute(
            "INSERT INTO baselines (name, version, description, created_at, "
            "scan_root, format, data, line_maps) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                name,
                version,
                description,
                created,
                os.path.normpath(os.path.abspath(scan_root)),
                format,
                json.dumps(data, ensure_ascii=False),
                json.dumps(line_maps, ensure_ascii=False)
                if line_maps
                else None,
            ),
        )
        self._conn.commit()
        return Baseline(
            id=int(cur.lastrowid),
            name=name,
            version=version,
            description=description,
            created_at=created,
            scan_root=os.path.normpath(os.path.abspath(scan_root)),
            format=format,
            data=data,
            line_maps=line_maps,
        )

    def list_baselines(self) -> List[Baseline]:
        """Return the latest version of each baseline name."""
        rows = self._conn.execute(
            "SELECT b.* FROM baselines b "
            "JOIN (SELECT name AS n, MAX(version) AS v FROM baselines "
            "GROUP BY name) m ON b.name = m.n AND b.version = m.v "
            "ORDER BY b.name"
        ).fetchall()
        return [self._baseline_from_row(r) for r in rows]

    def get_baseline(self, name: str) -> Baseline:
        """Return the latest version of a baseline; raises ValueError."""
        row = self._conn.execute(
            "SELECT * FROM baselines WHERE name = ? ORDER BY version DESC LIMIT 1",
            (name,),
        ).fetchone()
        if row is None:
            raise ValueError("baseline %r not found" % name)
        return self._baseline_from_row(row)

    def show_baseline(self, name: str, version: Optional[int] = None) -> Baseline:
        """Return a specific baseline version (latest when None)."""
        if version is None:
            return self.get_baseline(name)
        row = self._conn.execute(
            "SELECT * FROM baselines WHERE name = ? AND version = ?",
            (name, version),
        ).fetchone()
        if row is None:
            raise ValueError("baseline %r version %d not found" % (name, version))
        return self._baseline_from_row(row)

    def rollback_baseline(self, name: str) -> Baseline:
        """Delete the latest version; the previous version becomes current.

        Raises ValueError when the baseline has only one version (or does not
        exist).
        """
        rows = self._conn.execute(
            "SELECT id, version FROM baselines WHERE name = ? "
            "ORDER BY version DESC",
            (name,),
        ).fetchall()
        if not rows:
            raise ValueError("baseline %r not found" % name)
        if len(rows) == 1:
            raise ValueError(
                "baseline %r has only one version; nothing to roll back" % name
            )
        latest_id = int(rows[0]["id"])
        # Detach scans that reference the deleted version so the FK holds.
        self._conn.execute(
            "UPDATE scans SET baseline_id = NULL WHERE baseline_id = ?",
            (latest_id,),
        )
        self._conn.execute("DELETE FROM baselines WHERE id = ?", (latest_id,))
        self._conn.commit()
        return self.get_baseline(name)

    def _baseline_from_row(self, row: sqlite3.Row) -> Baseline:
        line_maps = self._row_get(row, "line_maps")
        return Baseline(
            id=int(row["id"]),
            name=row["name"],
            version=int(row["version"]),
            description=row["description"] or "",
            created_at=row["created_at"],
            scan_root=row["scan_root"],
            format=row["format"],
            data=json.loads(row["data"] or "{}"),
            line_maps=json.loads(line_maps) if line_maps else None,
        )

    # -- scans ------------------------------------------------------------

    def add_scan(
        self,
        baseline_id: Optional[int],
        mode: str,
        report: Dict[str, Any],
    ) -> int:
        """Persist a report (7.6 structure) and its items; returns scan_id."""
        data = report.get("data", {})
        summary = data.get("summary", {})
        created = data.get("created_at") or utcnow_iso()
        cur = self._conn.execute(
            "INSERT INTO scans (baseline_id, mode, created_at, added, removed, "
            "modified, type_changed, ignored, total, max_severity, report_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                baseline_id,
                mode,
                created,
                int(summary.get("added", 0)),
                int(summary.get("removed", 0)),
                int(summary.get("modified", 0)),
                int(summary.get("type_changed", 0)),
                int(summary.get("ignored", 0)),
                int(summary.get("total", 0)),
                str(summary.get("max_severity", "NONE")),
                json.dumps(report, ensure_ascii=False),
            ),
        )
        scan_id = int(cur.lastrowid)

        for item in data.get("items", []):
            self._conn.execute(
                "INSERT INTO scan_items (scan_id, key_path, change_type, "
                "severity, file, old_value, new_value, old_type, new_type, "
                "rule_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    scan_id,
                    str(item.get("key_path", "")),
                    str(item.get("change_type", "")),
                    str(item.get("severity", "NONE")),
                    str(item.get("file", "")),
                    self._json_or_none(item.get("old_value")),
                    self._json_or_none(item.get("new_value")),
                    item.get("old_type"),
                    item.get("new_type"),
                    item.get("rule_id"),
                ),
            )

        # Patch scan_id into the stored report so it is self-consistent.
        data["scan_id"] = scan_id
        report["data"] = data
        self._conn.execute(
            "UPDATE scans SET report_json = ? WHERE id = ?",
            (json.dumps(report, ensure_ascii=False), scan_id),
        )
        self._conn.commit()
        return scan_id

    @staticmethod
    def _json_or_none(value: Any) -> Optional[str]:
        if value is None:
            return None
        return json.dumps(value, ensure_ascii=False)

    def _scan_row_to_dict(self, r: sqlite3.Row) -> Dict[str, Any]:
        """Assemble a compact scan dict from a scans row.

        Single assembly path shared by ``list_scans`` and ``list_scans_paged``
        so the two endpoints can never drift apart (v0.9.0, D2).
        """
        baseline = None
        if r["baseline_id"] is not None:
            brow = self._conn.execute(
                "SELECT name, version FROM baselines WHERE id = ?",
                (r["baseline_id"],),
            ).fetchone()
            if brow is not None:
                baseline = {"name": brow["name"], "version": brow["version"]}
        return {
            "scan_id": int(r["id"]),
            "baseline_id": r["baseline_id"],
            "mode": r["mode"],
            "created_at": r["created_at"],
            "baseline": baseline,
            "summary": {
                "added": int(r["added"]),
                "removed": int(r["removed"]),
                "modified": int(r["modified"]),
                "type_changed": int(r["type_changed"]),
                "ignored": int(r["ignored"]),
                "total": int(r["total"]),
                "max_severity": r["max_severity"],
            },
        }

    def list_scans(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Return the most recent scans as compact dicts."""
        rows = self._conn.execute(
            "SELECT id, baseline_id, mode, created_at, added, removed, "
            "modified, type_changed, ignored, total, max_severity "
            "FROM scans ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [self._scan_row_to_dict(r) for r in rows]

    @staticmethod
    def _escape_like(text: str) -> str:
        """Escape LIKE metacharacters so user input matches literally (D2)."""
        return (
            str(text)
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )

    def list_scans_paged(
        self,
        q: Optional[str] = None,
        severity: Optional[str] = None,
        mode: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """Return ``{"scans": [ScanCompact...], "total": N}`` (v0.9.0, P0-1).

        ``q`` does a case-insensitive LIKE match against scan id / mode /
        baseline name (left-joined); LIKE metacharacters are escaped so user
        input never acts as a wildcard.  ``severity`` / ``mode`` are exact
        equality filters.  Ordering matches ``list_scans`` (``id DESC``);
        ``limit`` / ``offset`` page the result.
        """
        clauses: List[str] = []
        params: List[Any] = []
        if q:
            # A leading ``#`` is a natural way to search for a scan id
            # (the timeline shows rows as ``#1293``); strip it so the id
            # field (``CAST(id AS TEXT)`` = ``"1293"``) actually matches.
            pattern = "%" + Store._escape_like(str(q).lstrip("#")) + "%"
            clauses.append(
                "(LOWER(CAST(s.id AS TEXT)) LIKE ? ESCAPE '\\' OR "
                "LOWER(s.mode) LIKE ? ESCAPE '\\' OR "
                "LOWER(b.name) LIKE ? ESCAPE '\\')"
            )
            params += [pattern, pattern, pattern]
        if severity:
            clauses.append("s.max_severity = ?")
            params.append(str(severity))
        if mode:
            clauses.append("s.mode = ?")
            params.append(str(mode))
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        join = "LEFT JOIN baselines b ON b.id = s.baseline_id"

        total_row = self._conn.execute(
            "SELECT COUNT(*) AS c FROM scans s %s %s" % (join, where), params
        ).fetchone()
        total = int(total_row["c"])

        rows = self._conn.execute(
            "SELECT s.id, s.baseline_id, s.mode, s.created_at, s.added, "
            "s.removed, s.modified, s.type_changed, s.ignored, s.total, "
            "s.max_severity FROM scans s %s %s "
            "ORDER BY s.id DESC LIMIT ? OFFSET ?" % (join, where),
            params + [int(limit), int(offset)],
        ).fetchall()
        return {
            "scans": [self._scan_row_to_dict(r) for r in rows],
            "total": total,
        }

    def get_scan(self, scan_id: int) -> Dict[str, Any]:
        """Return the full stored report for a scan; raises ValueError."""
        row = self._conn.execute(
            "SELECT report_json FROM scans WHERE id = ?", (int(scan_id),)
        ).fetchone()
        if row is None:
            raise ValueError("scan %d not found" % scan_id)
        return json.loads(row["report_json"])

    def get_scan_created_at(self, scan_id: int) -> str:
        row = self._conn.execute(
            "SELECT created_at FROM scans WHERE id = ?", (int(scan_id),)
        ).fetchone()
        if row is None:
            raise ValueError("scan %d not found" % scan_id)
        return row["created_at"]

    def list_scan_items(
        self, scan_id: Optional[int] = None, limit: int = 100000
    ) -> List[Dict[str, Any]]:
        """Return scan items for mining (v0.7.0, C-08).

        Values are decoded back from their JSON TEXT representation.  With
        ``scan_id=None`` all items are returned (grouping by ``scan_id`` is
        the caller's job — each scan is one "same-change unit").
        """
        if scan_id is None:
            rows = self._conn.execute(
                "SELECT scan_id, key_path, change_type, old_value, new_value, "
                "file FROM scan_items ORDER BY id LIMIT ?",
                (int(limit),),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT scan_id, key_path, change_type, old_value, new_value, "
                "file FROM scan_items WHERE scan_id = ? ORDER BY id LIMIT ?",
                (int(scan_id), int(limit)),
            ).fetchall()
        out = []
        for r in rows:
            out.append(
                {
                    "scan_id": int(r["scan_id"]),
                    "key_path": r["key_path"],
                    "change_type": r["change_type"],
                    "old_value": self._json_load(r["old_value"]),
                    "new_value": self._json_load(r["new_value"]),
                    "file": r["file"],
                }
            )
        return out

    @staticmethod
    def _json_load(value: Optional[str]) -> Any:
        if value is None:
            return None
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return value

    # -- ignore rules -----------------------------------------------------

    def add_rule(self, rule: IgnoreRule) -> int:
        """Insert an ignore rule; returns the new rule id."""
        cur = self._conn.execute(
            "INSERT INTO ignore_rules (baseline_id, name, key_pattern, "
            "match_type, file_pattern, change_type, enabled, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rule.baseline_id,
                rule.name,
                rule.key_pattern,
                rule.match_type,
                rule.file_pattern,
                rule.change_type,
                1 if rule.enabled else 0,
                utcnow_iso(),
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def list_rules(self, baseline_id: Optional[int] = None) -> List[IgnoreRule]:
        """List ignore rules.

        With ``baseline_id=None`` returns global rules only.  With a
        ``baseline_id`` returns global rules plus rules scoped to that
        baseline.
        """
        if baseline_id is None:
            rows = self._conn.execute(
                "SELECT * FROM ignore_rules WHERE baseline_id IS NULL "
                "ORDER BY id"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM ignore_rules WHERE baseline_id IS NULL OR "
                "baseline_id = ? ORDER BY id",
                (int(baseline_id),),
            ).fetchall()
        return [rule_from_row(dict(r)) for r in rows]

    def delete_rule(self, rule_id: int) -> None:
        """Delete an ignore rule; raises ValueError when not found."""
        cur = self._conn.execute(
            "DELETE FROM ignore_rules WHERE id = ?", (int(rule_id),)
        )
        self._conn.commit()
        if cur.rowcount == 0:
            raise ValueError("ignore rule %d not found" % rule_id)

    # -- alert events (v0.4.0) -------------------------------------------

    def add_alert_event(self, event: Dict[str, Any]) -> int:
        """Record one alert delivery event (sent/failed); returns its id.

        Lazy pruning: every ``_ALERT_EVENT_PRUNE_EVERY`` inserts a prune pass
        keeps the table bounded without a separate maintenance job.
        """
        cur = self._conn.execute(
            "INSERT INTO alert_events (rule, baseline, severity, status, "
            "target, drift_count, error, attempts, fingerprint, created_at, "
            "retried, retried_from) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(event.get("rule", "")),
                str(event.get("baseline", "")),
                str(event.get("severity", "NONE")),
                str(event.get("status", "sent")),
                event.get("target"),
                int(event.get("drift_count", 0)),
                event.get("error"),
                int(event.get("attempts", 1)),
                event.get("fingerprint"),
                event.get("created_at") or utcnow_iso(),
                int(event.get("retried", 0) or 0),
                event.get("retried_from"),
            ),
        )
        self._conn.commit()
        event_id = int(cur.lastrowid)
        self._alert_insert_count += 1
        if self._alert_insert_count >= _ALERT_EVENT_PRUNE_EVERY:
            self._alert_insert_count = 0
            self.prune_alert_events(
                days=_ALERT_EVENT_RETENTION_DAYS,
                max_rows=_ALERT_EVENT_MAX_ROWS,
            )
        return event_id

    def list_alert_events(
        self,
        rule: Optional[str] = None,
        status: Optional[str] = None,
        severity: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """Return ``{"events": [...], "total": N}`` with filters + pagination."""
        clauses: List[str] = []
        params: List[Any] = []
        if rule:
            clauses.append("rule = ?")
            params.append(rule)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if severity:
            clauses.append("severity = ?")
            params.append(severity)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        total_row = self._conn.execute(
            "SELECT COUNT(*) AS c FROM alert_events %s" % where, params
        ).fetchone()
        total = int(total_row["c"])

        rows = self._conn.execute(
            "SELECT id, rule, baseline, severity, status, target, drift_count, "
            "error, attempts, fingerprint, created_at, retried, retried_from "
            "FROM alert_events %s "
            "ORDER BY id DESC LIMIT ? OFFSET ?" % where,
            params + [int(limit), int(offset)],
        ).fetchall()
        events = [dict(r) for r in rows]
        return {"events": events, "total": total}

    def get_alert_event(self, event_id: int) -> Dict[str, Any]:
        """Return one alert event row as a dict; raises ValueError (D4)."""
        row = self._conn.execute(
            "SELECT id, rule, baseline, severity, status, target, drift_count, "
            "error, attempts, fingerprint, created_at, retried, retried_from "
            "FROM alert_events WHERE id = ?",
            (int(event_id),),
        ).fetchone()
        if row is None:
            raise ValueError("alert event %d not found" % event_id)
        return dict(row)

    def count_alert_events(self) -> int:
        """Total number of recorded alert events."""
        row = self._conn.execute("SELECT COUNT(*) AS c FROM alert_events").fetchone()
        return int(row["c"])

    def prune_alert_events(self, days: int = 30, max_rows: int = 5000) -> int:
        """Delete events older than ``days`` and cap the table at ``max_rows``.

        Returns the number of deleted rows.
        """
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))
        ).isoformat()
        cur = self._conn.execute(
            "DELETE FROM alert_events WHERE created_at < ?", (cutoff,)
        )
        removed = int(cur.rowcount)
        # Cap by age first, then by row count (delete the oldest excess).
        count_row = self._conn.execute(
            "SELECT COUNT(*) AS c FROM alert_events"
        ).fetchone()
        excess = int(count_row["c"]) - max(0, int(max_rows))
        if excess > 0:
            cur2 = self._conn.execute(
                "DELETE FROM alert_events WHERE id IN ("
                "SELECT id FROM alert_events ORDER BY id ASC LIMIT ?)",
                (excess,),
            )
            removed += int(cur2.rowcount)
        if removed:
            self._conn.commit()
        return removed

    # -- constraint violations (v0.7.0, C-10) ---------------------------

    @staticmethod
    def _cv_retention_days() -> int:
        """Retention days for constraint violations (CFGDRIFT_CV_RETENTION_DAYS)."""
        raw = os.environ.get("CFGDRIFT_CV_RETENTION_DAYS", "")
        if raw.strip():
            try:
                return max(1, int(raw.strip()))
            except (TypeError, ValueError):
                return _CV_RETENTION_DAYS
        return _CV_RETENTION_DAYS

    def add_constraint_violations(
        self, scan_id: Optional[int], violations: List[dict]
    ) -> int:
        """Batch-insert constraint violations (C-10); returns the row count.

        ``scan_id`` may be ``None`` (violations recorded outside a scan); each
        element of ``violations`` carries ``constraint_id`` / ``kind`` /
        ``file`` / ``keys`` (list) / ``severity`` / ``detail`` and optionally
        ``created_at``.  Lazy pruning runs every ``_CV_PRUNE_EVERY`` inserts
        (retention + row cap, mirroring alert events).
        """
        if not violations:
            return 0
        now = utcnow_iso()
        rows = []
        for v in violations:
            keys = v.get("keys") or []
            if not isinstance(keys, list):
                keys = [keys]
            rows.append(
                (
                    str(v.get("constraint_id", "")),
                    v.get("scan_id", scan_id),
                    str(v.get("kind", "drift")),
                    str(v.get("file", "")),
                    json.dumps(keys, ensure_ascii=False),
                    str(v.get("severity", "WARN")),
                    str(v.get("detail", "")),
                    v.get("created_at") or now,
                )
            )
        self._conn.executemany(
            "INSERT INTO constraint_violations (constraint_id, scan_id, kind, "
            "file, keys, severity, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        self._conn.commit()
        count = len(rows)
        self._cv_insert_count += count
        if self._cv_insert_count >= _CV_PRUNE_EVERY:
            self._cv_insert_count = 0
            self.prune_constraint_violations()
        return count

    def list_constraint_violations(
        self,
        constraint_id: Optional[str] = None,
        kind: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """Return ``{"events": [...], "total": N}`` with filters + pagination.

        Event dicts mirror ``list_alert_events``: ``id`` / ``constraint_id`` /
        ``scan_id`` / ``kind`` / ``file`` / ``keys`` (parsed list) /
        ``severity`` / ``detail`` / ``created_at``.
        """
        clauses: List[str] = []
        params: List[Any] = []
        if constraint_id:
            clauses.append("constraint_id = ?")
            params.append(constraint_id)
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        total_row = self._conn.execute(
            "SELECT COUNT(*) AS c FROM constraint_violations %s" % where, params
        ).fetchone()
        total = int(total_row["c"])

        rows = self._conn.execute(
            "SELECT id, constraint_id, scan_id, kind, file, keys, severity, "
            "detail, created_at FROM constraint_violations %s "
            "ORDER BY id DESC LIMIT ? OFFSET ?" % where,
            params + [int(limit), int(offset)],
        ).fetchall()
        events = []
        for r in rows:
            raw_keys = r["keys"] or "[]"
            try:
                keys = json.loads(raw_keys)
                if not isinstance(keys, list):
                    keys = []
            except (ValueError, TypeError):
                keys = []
            events.append(
                {
                    "id": int(r["id"]),
                    "constraint_id": r["constraint_id"],
                    "scan_id": r["scan_id"],
                    "kind": r["kind"],
                    "file": r["file"],
                    "keys": keys,
                    "severity": r["severity"],
                    "detail": r["detail"],
                    "created_at": r["created_at"],
                }
            )
        return {"events": events, "total": total}

    def count_constraint_violations(self) -> int:
        """Total number of recorded constraint violations."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS c FROM constraint_violations"
        ).fetchone()
        return int(row["c"])

    def prune_constraint_violations(
        self, days: Optional[int] = None, max_rows: int = _CV_MAX_ROWS
    ) -> int:
        """Delete violations older than ``days`` and cap the table at rows.

        ``days`` defaults to ``CFGDRIFT_CV_RETENTION_DAYS`` (or 90); the table
        is additionally capped at ``max_rows`` (default 20000).  Returns the
        number of deleted rows.
        """
        if days is None:
            days = self._cv_retention_days()
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))
        ).isoformat()
        cur = self._conn.execute(
            "DELETE FROM constraint_violations WHERE created_at < ?", (cutoff,)
        )
        removed = int(cur.rowcount)
        count_row = self._conn.execute(
            "SELECT COUNT(*) AS c FROM constraint_violations"
        ).fetchone()
        excess = int(count_row["c"]) - max(0, int(max_rows))
        if excess > 0:
            cur2 = self._conn.execute(
                "DELETE FROM constraint_violations WHERE id IN ("
                "SELECT id FROM constraint_violations ORDER BY id ASC LIMIT ?)",
                (excess,),
            )
            removed += int(cur2.rowcount)
        if removed:
            self._conn.commit()
        return removed
