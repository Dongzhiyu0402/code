"""Constraint auto-mining tests (v0.7.0, C-08 / T04).

Covers: the three candidate passes (enum/range, conditional_required,
mutual_exclusion) from synthetic ``scan_items`` and a fixture corpus JSONL,
min-support thresholds, the per-key enum-before-range rule, mutual top-N, and
the never-auto-activated contract (``enabled: false`` / ``status: pending``).
"""

from __future__ import annotations

import json
import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

from cfgdrift.rules.mining import ConstraintMiner, MinedCandidate  # noqa: E402
from cfgdrift.storage.store import Store  # noqa: E402


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def _add_scan(store, items):
    """Insert one scan (a single change unit) with the given key/value items."""
    payload = {
        "code": 0,
        "data": {
            "summary": {"total": len(items)},
            "items": [
                {
                    "key_path": k,
                    "change_type": "modified",
                    "severity": "WARN",
                    "file": "app.yaml",
                    "old_value": None,
                    "new_value": v,
                    "old_type": None,
                    "new_type": None,
                }
                for k, v in items.items()
            ],
        },
        "message": "ok",
    }
    return store.add_scan(None, "manual", payload)


def _unit_store(tmp_path, units):
    store = Store(str(tmp_path / "cfgdrift.db"))
    for unit in units:
        _add_scan(store, unit)
    return store


class TestValueDomains:
    def test_enum_and_range(self, tmp_path):
        # 9 units: logging.level has 4 distinct values (enum); requests_limit
        # has 9 distinct numeric values -> too many for enum -> range.
        units = []
        levels = ["info", "warn", "error", "debug"]
        for i in range(9):
            units.append({"logging.level": levels[i % 4],
                          "requests_limit": 100 + i * 100})
        store = _unit_store(tmp_path, units)
        try:
            candidates = ConstraintMiner.mine_scans(store, min_support=3)
        finally:
            store.close()
        by_kind = {c.kind: c for c in candidates}
        assert "enum" in by_kind
        assert "range" in by_kind
        enum = by_kind["enum"]
        assert enum.constraint["type"] == "enum"
        assert enum.constraint["keys"] == ["logging.level"]
        assert set(enum.constraint["allowed"]) == {"info", "warn", "error", "debug"}
        assert enum.metrics["support"] == 9
        assert enum.metrics["confidence"] == 1.0
        assert enum.constraint["enabled"] is False
        assert enum.status == "pending"

        rng = by_kind["range"]
        assert rng.constraint["type"] == "range"
        assert rng.constraint["keys"] == ["requests_limit"]
        assert rng.constraint["min"] == 100
        assert rng.constraint["max"] == 900
        assert rng.metrics.get("observed") is True  # not a port key

    def test_port_key_standard_range(self, tmp_path):
        # server.port with 9 distinct values in [1,65535] -> [1, 65535],
        # NOT observed (standard port range heuristic).
        units = [{"server.port": 8080 + i} for i in range(9)]
        store = _unit_store(tmp_path, units)
        try:
            candidates = ConstraintMiner.mine_scans(store, min_support=3)
        finally:
            store.close()
        ranges = [c for c in candidates if c.kind == "range"]
        assert ranges
        rng = ranges[0]
        assert rng.constraint["min"] == 1
        assert rng.constraint["max"] == 65535
        assert "observed" not in rng.metrics

    def test_range_observed_for_non_port_key(self, tmp_path):
        # 10 distinct numeric values -> too many for enum -> observed range.
        store = _unit_store(
            tmp_path,
            [{"replicas": i} for i in range(1, 11)],
        )
        try:
            candidates = ConstraintMiner.mine_scans(store, min_support=2)
        finally:
            store.close()
        ranges = [c for c in candidates if c.kind == "range"]
        assert ranges
        rng = ranges[0]
        assert rng.constraint["min"] == 1
        assert rng.constraint["max"] == 10
        assert rng.metrics.get("observed") is True

    def test_enum_priority_over_range(self, tmp_path):
        # A key with numeric distinct values in [2,8] produces enum, not range.
        store = _unit_store(
            tmp_path,
            [
                {"level": 1},
                {"level": 2},
                {"level": 3},
            ],
        )
        try:
            candidates = ConstraintMiner.mine_scans(store, min_support=2)
        finally:
            store.close()
        level = [c for c in candidates if c.constraint["keys"] == ["level"]]
        assert len(level) == 1
        assert level[0].kind == "enum"

    def test_min_support_threshold(self, tmp_path):
        store = _unit_store(
            tmp_path,
            [
                {"logging.level": "info"},
                {"logging.level": "warn"},
            ],
        )
        try:
            low = ConstraintMiner.mine_scans(store, min_support=1)
            high = ConstraintMiner.mine_scans(store, min_support=3)
        finally:
            store.close()
        assert any(c.kind == "enum" for c in low)
        assert high == []


class TestConditionalRequired:
    def test_cooccurrence_confidence(self, tmp_path):
        store = _unit_store(
            tmp_path,
            [
                {"logging.level": "info", "server.port": 8080},
                {"logging.level": "warn", "server.port": 9090},
                {"logging.level": "error", "server.port": 8080},
                {"logging.level": "debug", "server.port": 8443},
            ],
        )
        try:
            candidates = ConstraintMiner.mine_scans(store, min_support=3)
        finally:
            store.close()
        conds = [c for c in candidates if c.kind == "conditional_required"]
        assert conds
        cond = conds[0]
        assert cond.constraint["type"] == "conditional_required"
        assert "when" in cond.constraint and "key" in cond.constraint["when"]
        assert cond.constraint["then"]["require"]
        assert cond.metrics["support"] == 4
        assert cond.metrics["confidence"] == 1.0
        assert cond.constraint["enabled"] is False

    def test_low_confidence_excluded(self, tmp_path):
        store = _unit_store(
            tmp_path,
            [
                {"a": 1, "b": 1},
                {"a": 2, "b": 2},
                {"a": 3, "b": 3},
                {"a": 4},  # b missing -> confidence 0.75
            ],
        )
        try:
            candidates = ConstraintMiner.mine_scans(store, min_support=2)
        finally:
            store.close()
        conds = [c for c in candidates if c.kind == "conditional_required"]
        assert conds == []


class TestMutualExclusion:
    def test_zero_intersection_pairs(self, tmp_path):
        store = _unit_store(
            tmp_path,
            [
                {"mode": "dev", "env": "local"},
                {"mode": "dev", "env": "local"},
                {"mode": "prod", "env": "prod"},
                {"mode": "prod", "env": "prod"},
                {"mode": "stage", "env": "stage"},
                {"mode": "stage", "env": "stage"},
            ],
        )
        try:
            candidates = ConstraintMiner.mine_scans(store, min_support=2)
        finally:
            store.close()
        mutuals = [c for c in candidates if c.kind == "mutual_exclusion"]
        assert mutuals
        forbids = set()
        for c in mutuals:
            assert c.constraint["type"] == "mutual_exclusion"
            assert c.constraint["keys"] == ["env", "mode"] or \
                c.constraint["keys"] == ["mode", "env"]
            assert len(c.constraint["forbid"]) == 1
            forbids.add(tuple(c.constraint["forbid"][0]))
            assert c.metrics["confidence"] == 1.0
            assert c.constraint["enabled"] is False
        # dev never co-occurs with prod/local... dev+prod, prod+dev etc.
        assert ("dev", "prod") in forbids or ("prod", "dev") in forbids
        assert ("dev", "stage") in forbids or ("stage", "dev") in forbids

    def test_top_n_per_key_pair(self, tmp_path):
        # mode has values 1..6 each paired with a distinct env, so (mode=n, env=m)
        # zero-intersection pairs are many; each key pair is capped at top-N 5.
        units = []
        for i in range(1, 7):
            for _ in range(2):
                units.append({"mode": i, "env": "e%d" % i})
        store = _unit_store(tmp_path, units)
        try:
            candidates = ConstraintMiner.mine_scans(store, min_support=2)
        finally:
            store.close()
        mutuals = [c for c in candidates if c.kind == "mutual_exclusion"]
        # pair_keys only (mode, env) -> at most 5 candidates (top-N default).
        assert len(mutuals) <= 5


class TestCorpusSource:
    def _fixture_jsonl(self, tmp_path):
        instances = []
        levels = ["info", "warn", "error", "debug"]
        for i in range(9):
            instances.append({"diff": {"feature": {"changed_values": {
                "logging.level": {"before": levels[i % 4], "after": levels[i % 4]},
                "requests_limit": {"before": 100 + i * 100,
                                   "after": 100 + i * 100},
            }}}})
        path = str(tmp_path / "instances.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            for inst in instances:
                fh.write(json.dumps(inst) + "\n")
        return path

    def test_mine_corpus(self, tmp_path):
        path = self._fixture_jsonl(tmp_path)
        candidates = ConstraintMiner.mine_corpus(path, min_support=3)
        by_kind = {c.kind: c for c in candidates}
        assert "enum" in by_kind
        assert "range" in by_kind
        assert "conditional_required" in by_kind
        assert all(c.metrics["source"] == "corpus" for c in candidates)
        assert all(c.constraint["enabled"] is False for c in candidates)

    def test_mine_corpus_missing_file(self, tmp_path):
        with pytest.raises(ValueError):
            ConstraintMiner.mine_corpus(str(tmp_path / "nope.jsonl"), 5)


class TestCandidateFile:
    def test_save_load_roundtrip(self, tmp_path):
        candidates = [
            MinedCandidate(
                id="mined_enum_1",
                kind="enum",
                constraint={
                    "id": "mined_enum_1", "type": "enum", "message": "m",
                    "severity": "WARN", "enabled": False, "source": "user",
                    "keys": ["a"], "allowed": [1, 2],
                },
                metrics={"support": 5, "confidence": 1.0, "samples": 5,
                         "source": "scans"},
            )
        ]
        path = str(tmp_path / "mined_candidates.yaml")
        ConstraintMiner.save_candidates(path, candidates, source="scans",
                                        min_support=5)
        loaded = ConstraintMiner.load_candidates(path)
        assert len(loaded) == 1
        assert loaded[0].id == "mined_enum_1"
        assert loaded[0].constraint["enabled"] is False
        assert loaded[0].status == "pending"

    def test_candidates_never_auto_activated(self, tmp_path):
        # The contract (D5): whatever the source, the persisted constraint is
        # disabled and pending — promoting requires an explicit `add --rule`.
        store = _unit_store(
            tmp_path,
            [
                {"logging.level": "info", "server.port": 8080},
                {"logging.level": "warn", "server.port": 9090},
                {"logging.level": "error", "server.port": 8080},
                {"logging.level": "debug", "server.port": 8443},
            ],
        )
        try:
            candidates = ConstraintMiner.mine_scans(store, min_support=3)
        finally:
            store.close()
        path = str(tmp_path / "mined.yaml")
        ConstraintMiner.save_candidates(path, candidates)
        for c in ConstraintMiner.load_candidates(path):
            assert c.constraint["enabled"] is False
            assert c.status == "pending"
