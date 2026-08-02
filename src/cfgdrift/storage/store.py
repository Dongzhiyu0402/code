"""SQLite repository: baselines (versioned), scan history, ignore rules."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
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

CREATE INDEX IF NOT EXISTS idx_scans_created ON scans(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_scan_items_scan ON scan_items(scan_id);
"""


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
        self.init_schema()

    # -- schema -----------------------------------------------------------

    def init_schema(self) -> None:
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

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
    ) -> Baseline:
        """Create a new baseline version (same name -> version + 1)."""
        row = self._conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM baselines WHERE name = ?",
            (name,),
        ).fetchone()
        version = int(row["v"]) + 1
        created = utcnow_iso()
        cur = self._conn.execute(
            "INSERT INTO baselines (name, version, description, created_at, "
            "scan_root, format, data) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                name,
                version,
                description,
                created,
                os.path.normpath(os.path.abspath(scan_root)),
                format,
                json.dumps(data, ensure_ascii=False),
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
        return Baseline(
            id=int(row["id"]),
            name=row["name"],
            version=int(row["version"]),
            description=row["description"] or "",
            created_at=row["created_at"],
            scan_root=row["scan_root"],
            format=row["format"],
            data=json.loads(row["data"] or "{}"),
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

    def list_scans(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Return the most recent scans as compact dicts."""
        rows = self._conn.execute(
            "SELECT id, baseline_id, mode, created_at, added, removed, "
            "modified, type_changed, ignored, total, max_severity "
            "FROM scans ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        out = []
        for r in rows:
            baseline = None
            if r["baseline_id"] is not None:
                brow = self._conn.execute(
                    "SELECT name, version FROM baselines WHERE id = ?",
                    (r["baseline_id"],),
                ).fetchone()
                if brow is not None:
                    baseline = {"name": brow["name"], "version": brow["version"]}
            out.append(
                {
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
            )
        return out

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
