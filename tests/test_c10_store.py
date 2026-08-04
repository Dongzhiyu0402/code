"""C-10 constraint_violations table tests (v0.7.0, T01).

Covers: idempotent schema creation, CRUD + pagination + filters, retention
(90 days configurable through ``CFGDRIFT_CV_RETENTION_DAYS``), hard row cap,
and the every-200-inserts lazy prune trigger.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

from cfgdrift.storage.store import (  # noqa: E402
    _CV_MAX_ROWS,
    _CV_PRUNE_EVERY,
    _CV_RETENTION_DAYS,
    Store,
)


def _v(constraint_id="c1", kind="drift", file="app.yaml", keys=None,
        severity="WARN", detail="msg", created_at=None):
    return {
        "constraint_id": constraint_id,
        "kind": kind,
        "file": file,
        "keys": keys or ["a.b"],
        "severity": severity,
        "detail": detail,
        "created_at": created_at,
    }


def _old_iso(days_ago):
    return (
        datetime.now(timezone.utc) - timedelta(days=days_ago)
    ).isoformat()


@pytest.fixture()
def store(tmp_path):
    s = Store(str(tmp_path / "db" / "cfgdrift.db"))
    yield s
    s.close()


class TestSchemaIdempotent:
    def test_table_created_and_reopen_idempotent(self, tmp_path):
        path = str(tmp_path / "cfgdrift.db")
        s1 = Store(path)
        s1.add_constraint_violations(None, [_v()])
        s1.close()
        # Re-opening must not fail (CREATE TABLE IF NOT EXISTS + indexes).
        s2 = Store(path)
        assert s2.count_constraint_violations() == 1
        s2.close()

    def test_indexes_present(self, store):
        rows = store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE "
            "'idx_cv_%' ORDER BY name"
        ).fetchall()
        names = [r["name"] for r in rows]
        assert "idx_cv_created" in names
        assert "idx_cv_constraint" in names
        assert "idx_cv_scan" in names


class TestCrud:
    def test_add_and_list(self, store):
        n = store.add_constraint_violations(
            7, [_v(), _v(constraint_id="c2", kind="baseline", keys=["x", "y"])]
        )
        assert n == 2
        result = store.list_constraint_violations(limit=10)
        assert result["total"] == 2
        events = result["events"]
        assert events[0]["constraint_id"] == "c2"  # newest first (id DESC)
        assert events[1]["constraint_id"] == "c1"
        assert events[1]["scan_id"] == 7
        assert events[1]["kind"] == "drift"
        assert events[1]["keys"] == ["a.b"]
        assert events[1]["severity"] == "WARN"
        assert events[1]["detail"] == "msg"

    def test_empty_violations_noop(self, store):
        assert store.add_constraint_violations(1, []) == 0
        assert store.count_constraint_violations() == 0

    def test_filter_by_constraint_and_kind(self, store):
        store.add_constraint_violations(
            None, [_v(constraint_id="c1", kind="drift"),
                   _v(constraint_id="c1", kind="baseline"),
                   _v(constraint_id="c2", kind="drift")]
        )
        assert store.list_constraint_violations(constraint_id="c1")["total"] == 2
        assert store.list_constraint_violations(kind="drift")["total"] == 2
        assert store.list_constraint_violations(
            constraint_id="c1", kind="baseline"
        )["total"] == 1

    def test_pagination(self, store):
        store.add_constraint_violations(None, [_v(constraint_id="c%d" % i)
                                               for i in range(10)])
        page1 = store.list_constraint_violations(limit=3, offset=0)
        assert page1["total"] == 10
        assert len(page1["events"]) == 3
        page2 = store.list_constraint_violations(limit=3, offset=3)
        ids = [e["constraint_id"] for e in page2["events"]]
        assert len(ids) == 3
        assert set(ids) <= {"c%d" % i for i in range(10)}

    def test_keys_json_roundtrip(self, store):
        store.add_constraint_violations(
            None, [_v(keys=["a", "b.c", 3])]
        )
        event = store.list_constraint_violations()["events"][0]
        assert event["keys"] == ["a", "b.c", 3]


class TestPrune:
    def test_prune_by_age(self, store):
        store.add_constraint_violations(
            None, [
                _v(constraint_id="old", created_at=_old_iso(200)),
                _v(constraint_id="new", created_at=_old_iso(1)),
            ]
        )
        removed = store.prune_constraint_violations(days=90)
        assert removed == 1
        assert store.count_constraint_violations() == 1
        assert store.list_constraint_violations()["events"][0]["constraint_id"] == "new"

    def test_prune_row_cap(self, store):
        store.add_constraint_violations(None, [_v(constraint_id="c%d" % i)
                                               for i in range(10)])
        removed = store.prune_constraint_violations(days=3650, max_rows=4)
        assert removed == 6
        assert store.count_constraint_violations() == 4

    def test_retention_days_env(self, store, monkeypatch):
        store.add_constraint_violations(
            None, [
                _v(constraint_id="old", created_at=_old_iso(60)),
                _v(constraint_id="new", created_at=_old_iso(1)),
            ]
        )
        # Default 90 days keeps both.
        assert store.prune_constraint_violations() == 0
        # A 30-day retention drops the 60-day-old row.
        monkeypatch.setenv("CFGDRIFT_CV_RETENTION_DAYS", "30")
        assert store.prune_constraint_violations() == 1
        assert store.count_constraint_violations() == 1

    def test_lazy_prune_trigger(self, store):
        store.add_constraint_violations(
            None, [
                _v(constraint_id="old", created_at=_old_iso(200)),
            ]
        )
        # Bump the counter just below the threshold, then one insert triggers.
        store._cv_insert_count = _CV_PRUNE_EVERY - 1
        store.add_constraint_violations(None, [_v(constraint_id="fresh")])
        assert store.count_constraint_violations() == 1
        assert store.list_constraint_violations()["events"][0]["constraint_id"] == "fresh"

    def test_defaults_constants(self):
        assert _CV_PRUNE_EVERY == 200
        assert _CV_RETENTION_DAYS == 90
        assert _CV_MAX_ROWS == 20000

    def test_retention_days_env_invalid_falls_back(self, store, monkeypatch):
        monkeypatch.setenv("CFGDRIFT_CV_RETENTION_DAYS", "not-a-number")
        assert store._cv_retention_days() == _CV_RETENTION_DAYS
