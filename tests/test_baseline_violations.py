"""Baseline (pre-existing) constraint violations — C-07 (v0.7.0, T02).

Covers:
- ``ConstraintEngine.check_tree`` returns ALL violations with the
  constraint's own severity (Q6);
- ``ConstraintEngine.baseline_violations`` excludes drift-associated
  violations by signature ``(constraint_id, file, frozenset(involved_keys))``;
- ``scan --report-violations`` renders the terminal section + JSON field and
  writes C-10 rows with ``kind='baseline'``;
- default-off zero-noise: without the flag the output matches v0.6.0
  byte-for-byte (no section, no key, no C-10 baseline rows).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
PY = sys.executable

sys.path.insert(0, SRC)

from cfgdrift.core.constraints import ConstraintEngine  # noqa: E402
from cfgdrift.core.model import (  # noqa: E402
    ChangeType,
    Constraint,
    DriftItem,
    Severity,
)
from cfgdrift.core.parser import parse_text  # noqa: E402
from cfgdrift.rules.constraints import resolve  # noqa: E402
from cfgdrift.storage.store import Store  # noqa: E402


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def _run_cli(home, args, store=None):
    env = dict(os.environ)
    env["CFGDRIFT_HOME"] = str(home)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    cmd = [PY, "-m", "cfgdrift.cli"]
    if store:
        cmd += ["--store", str(store)]
    cmd += list(args)
    return subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=180)


def _flat_yaml(port, with_tls=True):
    lines = ["server:", "  port: %d" % port, "  gzip: on"]
    if with_tls:
        lines += ["tls:", "  enabled: true"]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# pure engine
# ---------------------------------------------------------------------------

class TestCheckTree:
    def test_all_violations_with_constraint_severity(self, tmp_path):
        home = str(tmp_path / "home")
        os.makedirs(home, exist_ok=True)
        constraints = resolve(home, [], builtin_enabled=True)
        tree = parse_text(_flat_yaml(8080, with_tls=True), "yaml")  # tls.enabled w/o cert_path
        violations = ConstraintEngine.check_tree(constraints, {"app.yaml": tree})
        ids = {v["constraint_id"] for v in violations}
        assert "http_ssl_cert_required" in ids
        ssl = [v for v in violations if v["constraint_id"] == "http_ssl_cert_required"]
        assert len(ssl) == 2  # cert_path + key_path
        for v in ssl:
            assert v["severity"] == "CRITICAL"  # severity straight from constraint (Q6)
            assert v["file"] == "app.yaml"
            assert set(v["involved_keys"]) == {"tls.enabled", "tls.cert_path"} or \
                set(v["involved_keys"]) == {"tls.enabled", "tls.key_path"}

    def test_check_tree_empty_for_clean_tree(self, tmp_path):
        home = str(tmp_path / "home")
        os.makedirs(home, exist_ok=True)
        constraints = resolve(home, [], builtin_enabled=True)
        tree = parse_text(
            "server:\n  port: 8080\n  gzip: on\ntls:\n  enabled: false\n", "yaml"
        )
        assert ConstraintEngine.check_tree(constraints, {"a.yaml": tree}) == []


class TestBaselineViolations:
    def _setup(self, tmp_path):
        home = str(tmp_path / "home")
        os.makedirs(home, exist_ok=True)
        constraints = resolve(home, [], builtin_enabled=True)
        # new tree: tls.enabled=true but cert/key missing (pre-existing break)
        new_tree = parse_text(_flat_yaml(9090, with_tls=True), "yaml")
        snapshot = {"app.yaml": new_tree}
        return constraints, snapshot

    def test_excludes_drift_associated(self, tmp_path):
        constraints, snapshot = self._setup(tmp_path)
        # Drift touches tls.cert_path -> the cert_path violation is
        # drift-associated; the key_path violation remains as baseline.
        drift_items = [
            DriftItem(
                key_path="tls.cert_path",
                change_type=ChangeType.REMOVED,
                severity=Severity.CRITICAL,
                file="app.yaml",
                constraint_violations=[{
                    "constraint_id": "http_ssl_cert_required",
                    "type": "conditional_required",
                    "message": "tls.cert_path 缺失",
                    "involved_keys": ["tls.enabled", "tls.cert_path"],
                }],
            )
        ]
        bv = ConstraintEngine.baseline_violations(constraints, snapshot, drift_items)
        assert len(bv) == 1
        assert set(bv[0]["involved_keys"]) == {"tls.enabled", "tls.key_path"}

    def test_keeps_unrelated_baseline_violation(self, tmp_path):
        constraints, snapshot = self._setup(tmp_path)
        # Drift touches only server.port -> tls violations are baseline.
        drift_items = [
            DriftItem(
                key_path="server.port",
                change_type=ChangeType.MODIFIED,
                severity=Severity.WARN,
                file="app.yaml",
                old_value=8080,
                new_value=9090,
            )
        ]
        bv = ConstraintEngine.baseline_violations(constraints, snapshot, drift_items)
        assert len(bv) == 2
        assert all(v["constraint_id"] == "http_ssl_cert_required" for v in bv)
        assert all(v["severity"] == "CRITICAL" for v in bv)

    def test_dedupes_by_signature(self, tmp_path):
        constraints, snapshot = self._setup(tmp_path)
        # Two drift items carry the SAME cert_path violation -> excluded once;
        # the key_path violation stays (single row).
        vio = {
            "constraint_id": "http_ssl_cert_required",
            "type": "conditional_required",
            "message": "x",
            "involved_keys": ["tls.enabled", "tls.cert_path"],
        }
        drift_items = [
            DriftItem("tls.cert_path", ChangeType.REMOVED, Severity.CRITICAL,
                      "app.yaml", constraint_violations=[dict(vio)]),
            DriftItem("tls.cert_path", ChangeType.REMOVED, Severity.CRITICAL,
                      "app.yaml", constraint_violations=[dict(vio)]),
        ]
        bv = ConstraintEngine.baseline_violations(
            constraints, snapshot, drift_items
        )
        assert len(bv) == 1
        assert set(bv[0]["involved_keys"]) == {"tls.enabled", "tls.key_path"}


# ---------------------------------------------------------------------------
# CLI: scan --report-violations (subprocess)
# ---------------------------------------------------------------------------

@pytest.fixture()
def scan_env(tmp_path):
    home = tmp_path / "home"
    store_path = tmp_path / "db" / "cfgdrift.db"
    conf = tmp_path / "conf" / "app.yaml"
    _write(str(conf), _flat_yaml(8080, with_tls=True))
    r = _run_cli(home, ["baseline", "create", "env1", "--scan-root", str(conf)],
                 store=store_path)
    assert r.returncode == 0, r.stderr
    return home, store_path, conf


class TestScanReportViolations:
    def test_default_off_zero_noise(self, scan_env):
        home, store_path, conf = scan_env
        _write(str(conf), _flat_yaml(9090, with_tls=True))
        r = _run_cli(home, ["scan", str(conf), "--baseline", "env1"],
                     store=store_path)
        # exit 1 = drift detected (scan contract); zero-noise means no
        # baseline section and no C-10 rows.
        assert r.returncode == 1
        assert "Baseline violations:" not in r.stdout
        assert "constraint" not in r.stdout
        store = Store(str(store_path))
        try:
            assert store.count_constraint_violations() == 0
        finally:
            store.close()

    def test_report_violations_terminal_and_json(self, scan_env):
        home, store_path, conf = scan_env
        _write(str(conf), _flat_yaml(9090, with_tls=True))
        r = _run_cli(
            home,
            ["scan", str(conf), "--baseline", "env1", "--report-violations"],
            store=store_path,
        )
        assert r.returncode == 1
        assert "Baseline violations:" in r.stdout
        assert "[CRITICAL] constraint http_ssl_cert_required" in r.stdout
        assert "tls.enabled, tls.cert_path" in r.stdout

        # Stored JSON report carries baseline_violations (non-empty only).
        store = Store(str(store_path))
        try:
            scans = store.list_scans(limit=1)
            payload = store.get_scan(scans[0]["scan_id"])
            data = payload["data"]
            assert "baseline_violations" in data
            assert len(data["baseline_violations"]) == 2
            assert data["baseline_violations"][0]["severity"] == "CRITICAL"
            # C-10 rows with kind=baseline were written; keys/detail must be
            # populated (involved_keys->keys / message->detail mapping).
            events = store.list_constraint_violations(kind="baseline")
            assert events["total"] == 2
            assert events["events"][0]["severity"] == "CRITICAL"
            for event in events["events"]:
                assert event["keys"], "baseline C-10 row lost involved_keys"
                assert event["detail"], "baseline C-10 row lost detail/message"
        finally:
            store.close()

    def test_no_violations_no_section(self, scan_env):
        home, store_path, conf = scan_env
        # new tree keeps everything valid: add the required tls cert/key.
        _write(
            str(conf),
            "server:\n  port: 9090\n  gzip: on\n"
            "tls:\n  enabled: true\n  cert_path: /c\n  key_path: /k\n",
        )
        r = _run_cli(
            home,
            ["scan", str(conf), "--baseline", "env1", "--report-violations"],
            store=store_path,
        )
        assert r.returncode == 1  # drift: added cert/key + modified port
        assert "Baseline violations:" not in r.stdout
        store = Store(str(store_path))
        try:
            assert store.list_constraint_violations(kind="baseline")["total"] == 0
        finally:
            store.close()

    def test_drift_associated_violation_not_repeated(self, scan_env):
        home, store_path, conf = scan_env
        # Baseline is fully valid (cert/key present). The new file REMOVES
        # tls.cert_path -> the violation IS the drift (associated), so it must
        # NOT appear in the baseline section; drift C-10 rows are written.
        _write(
            str(conf),
            "server:\n  port: 8080\n  gzip: on\n"
            "tls:\n  enabled: true\n  cert_path: /c\n  key_path: /k\n",
        )
        r = _run_cli(home, ["baseline", "create", "env2", "--scan-root",
                            str(conf)], store=store_path)
        assert r.returncode == 0, r.stderr
        _write(str(conf), "server:\n  port: 8080\n  gzip: on\n"
                          "tls:\n  enabled: true\n")
        r = _run_cli(
            home,
            ["scan", str(conf), "--baseline", "env2", "--report-violations"],
            store=store_path,
        )
        assert r.returncode == 1
        assert "Baseline violations:" not in r.stdout
        store = Store(str(store_path))
        try:
            assert store.list_constraint_violations(kind="drift")["total"] >= 1
            assert store.list_constraint_violations(kind="baseline")["total"] == 0
        finally:
            store.close()
