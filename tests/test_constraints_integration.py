"""Integration tests for cfgdrift v0.6.0 — consistency constraints end-to-end (T05).

Covers:

1. Scenario A: tls.enabled false->true without cert_path -> composite alert +
   WARN->CRITICAL + message contains「tls.cert_path 缺失」.
2. Scenario B: server.port 8080->99999 -> JSON contains
   constraint {id: http_port_range, type: range} + severity >= CRITICAL.
3. Scenario C: ``constraint add`` takes effect on the next diff; daemon
   reloads constraints every cycle (D9).
4. ``--builtin off`` disables the built-in library end to end.
5. ``--constraints`` extra file takes effect.
6. Zero-noise: legal changes produce output identical to v0.5.0 (no
   constraint field / no extra lines).
7. The full pre-existing suite (552) stays green when run by the caller —
   this file guards the key regression surface that could trip it.
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

from cfgdrift.core.constraints import BUILTIN_CONSTRAINTS  # noqa: E402
from cfgdrift.core.differ import SemanticDiffer  # noqa: E402
from cfgdrift.daemon.worker import DaemonWorker  # noqa: E402
from cfgdrift.rules.constraints import ConstraintConfig  # noqa: E402
from cfgdrift.storage.store import Store  # noqa: E402

SCENARIO_A_BASELINE = '{"tls": {"enabled": false}, "server": {"port": 8080}}\n'
SCENARIO_A_CURRENT = '{"tls": {"enabled": true}, "server": {"port": 8080}}\n'
SCENARIO_B_BASELINE = '{"server": {"port": 8080}}\n'
SCENARIO_B_CURRENT = '{"server": {"port": 99999}}\n'


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def _run_cli(home, args, store=None):
    env = dict(os.environ)
    env["CFGDRIFT_HOME"] = str(home)
    cmd = [PY, "-m", "cfgdrift.cli"]
    if store:
        cmd += ["--store", str(store)]
    cmd += list(args)
    return subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=180)


def _setup(tmp_path, baseline_text, current_text):
    home = tmp_path / "home"
    store_path = tmp_path / "db" / "cfgdrift.db"
    conf = tmp_path / "conf"
    _write(str(conf / "app.json"), baseline_text)
    r = _run_cli(home, ["baseline", "create", "prod", "--scan-root", str(conf)],
                 store=store_path)
    assert r.returncode == 0, r.stderr
    _write(str(conf / "app.json"), current_text)
    return home, store_path, conf


# ---------------------------------------------------------------------------
# Scenarios A / B
# ---------------------------------------------------------------------------

class TestScenarios:
    def test_scenario_a_composite_alert(self, tmp_path):
        home, store_path, conf = _setup(
            tmp_path, SCENARIO_A_BASELINE, SCENARIO_A_CURRENT)
        r = _run_cli(home, ["diff", str(conf), "--baseline", "prod"],
                     store=store_path)
        assert r.returncode == 1, r.stderr
        out = r.stdout
        assert "[CRITICAL]" in out
        assert "constraint http_ssl_cert_required [conditional_required]" in out
        assert "tls.cert_path 缺失（tls.enabled=true 需要该字段）" in out
        assert "max=CRITICAL" in out

    def test_scenario_b_json(self, tmp_path):
        home, store_path, conf = _setup(
            tmp_path, SCENARIO_B_BASELINE, SCENARIO_B_CURRENT)
        _run_cli(home, ["diff", str(conf), "--baseline", "prod"],
                 store=store_path)
        out = tmp_path / "report.json"
        r = _run_cli(home, ["report", "--json", str(out)], store=store_path)
        assert r.returncode == 0, r.stderr
        doc = json.loads(out.read_text(encoding="utf-8"))
        item = doc["data"]["items"][0]
        assert item["key_path"] == "server.port"
        assert item["severity"] == "CRITICAL"
        cv = item["constraint_violations"][0]
        assert cv["constraint_id"] == "http_port_range"
        assert cv["type"] == "range"

    def test_scenario_b_json_stdout(self, tmp_path):
        # `report --json <path>` writes a file; JSON stdout comes from
        # render_json through the library — assert the JSON contract directly.
        differ = SemanticDiffer()
        items, summary = differ.diff_snapshot(
            {"app.json": {"server": {"port": 8080}}},
            {"app.json": {"server": {"port": 99999}}},
            constraints=BUILTIN_CONSTRAINTS,
        )
        from cfgdrift.core.model import Report

        rep = Report(None, None, "2026-08-04T00:00:00+00:00", "manual",
                     summary, items)
        payload = json.loads(rep.to_json())
        assert payload["data"]["items"][0]["constraint_violations"][0][
            "constraint_id"] == "http_port_range"


# ---------------------------------------------------------------------------
# Scenario C: add -> next diff; daemon next cycle
# ---------------------------------------------------------------------------

class TestScenarioC:
    def test_add_takes_effect_next_diff(self, tmp_path):
        home, store_path, conf = _setup(
            tmp_path, SCENARIO_B_BASELINE, SCENARIO_B_CURRENT)
        # Without the custom rule: only builtin http_port_range fires.
        rule = json.dumps({
            "id": "custom_upper",
            "type": "range",
            "keys": ["server.port"],
            "min": 1,
            "max": 10000,
            "message": "custom upper bound 10000",
        })
        r = _run_cli(home, ["constraint", "add", "--rule", rule],
                     store=store_path)
        assert r.returncode == 0, r.stderr
        r = _run_cli(home, ["diff", str(conf), "--baseline", "prod"],
                     store=store_path)
        assert r.returncode == 1, r.stderr
        assert "constraint custom_upper [range]" in r.stdout
        assert "custom upper bound 10000" in r.stdout

    def test_daemon_worker_reloads_constraints_each_cycle(self, tmp_path):
        """D9: constraint added between cycles takes effect on the next."""
        home = tmp_path / "home"
        store_path = tmp_path / "db" / "cfgdrift.db"
        conf = tmp_path / "conf"
        _write(str(conf / "app.json"), SCENARIO_B_BASELINE)
        _run_cli(home, ["baseline", "create", "prod", "--scan-root", str(conf)],
                 store=store_path)
        _write(str(conf / "app.json"), SCENARIO_B_CURRENT)

        worker = DaemonWorker(
            store_path=str(store_path),
            paths=[str(conf)],
            fmt="auto",
            baseline_name="prod",
            interval=1,
            dispatcher=None,
            home=str(home),
            builtin_enabled=False,  # isolate the user rule
        )
        store = Store(str(store_path))
        try:
            # cycle 1: no user constraints yet -> no constraint output
            worker._constraints = worker._load_constraints()
            worker._scan_one(store, str(conf))
            last = store.get_scan(store.list_scans(limit=1)[0]["scan_id"])
            item = last["data"]["items"][0]
            assert "constraint_violations" not in item
            # add a user constraint
            ConstraintConfig.add_rule(
                str(home / "constraints.yaml"),
                __import__("cfgdrift.core.model", fromlist=["Constraint"])
                .Constraint.from_dict({
                    "id": "user_port",
                    "type": "range",
                    "keys": ["server.port"],
                    "min": 1,
                    "max": 65535,
                    "message": "user port bound",
                }, source="user"),
            )
            # cycle 2 reloads -> violation appears
            worker._constraints = worker._load_constraints()
            worker._scan_one(store, str(conf))
            last = store.get_scan(store.list_scans(limit=1)[0]["scan_id"])
            item = last["data"]["items"][0]
            assert item["constraint_violations"][0]["constraint_id"] == \
                "user_port"
            assert item["severity"] == "CRITICAL"
        finally:
            store.close()
            worker.request_stop()


# ---------------------------------------------------------------------------
# --builtin off / --constraints extra file
# ---------------------------------------------------------------------------

class TestSwitches:
    def test_builtin_off_end_to_end(self, tmp_path):
        home, store_path, conf = _setup(
            tmp_path, SCENARIO_B_BASELINE, SCENARIO_B_CURRENT)
        r = _run_cli(home, ["diff", str(conf), "--baseline", "prod",
                            "--no-builtin"], store=store_path)
        assert r.returncode == 1, r.stderr
        assert "constraint" not in r.stdout
        assert "[WARN]" in r.stdout

    def test_constraints_file_applies(self, tmp_path):
        home, store_path, conf = _setup(
            tmp_path, SCENARIO_B_BASELINE, SCENARIO_B_CURRENT)
        extra = tmp_path / "extra.yaml"
        _write(str(extra), """
version: 1
rules:
  - id: extra_bound
    type: range
    keys: [server.port]
    min: 1
    max: 50000
    message: "extra bound 50000"
""")
        r = _run_cli(home, ["diff", str(conf), "--baseline", "prod",
                            "--constraints", str(extra)], store=store_path)
        assert r.returncode == 1, r.stderr
        assert "constraint extra_bound [range]" in r.stdout
        assert "extra bound 50000" in r.stdout

    def test_user_rule_overrides_builtin_same_id(self, tmp_path):
        # D8: same id in constraints.yaml overrides the built-in library.
        home, store_path, conf = _setup(
            tmp_path, SCENARIO_B_BASELINE, SCENARIO_B_CURRENT)
        override = json.dumps({
            "id": "http_port_range",
            "type": "range",
            "keys": ["server.port"],
            "min": 1,
            "max": 100000,
            "message": "overridden: allow up to 100000",
        })
        _run_cli(home, ["constraint", "add", "--rule", override],
                 store=store_path)
        r = _run_cli(home, ["diff", str(conf), "--baseline", "prod"],
                     store=store_path)
        # 99999 now within [1, 100000] -> no constraint violation
        assert r.returncode == 1, r.stderr
        assert "constraint" not in r.stdout


# ---------------------------------------------------------------------------
# Zero-noise regression surface
# ---------------------------------------------------------------------------

class TestZeroNoise:
    def test_legal_change_terminal_identical_to_v050(self, tmp_path):
        """port 8080->9090 (in range): no constraint line, WARN kept."""
        home, store_path, conf = _setup(
            tmp_path, '{"server": {"port": 8080}}\n',
            '{"server": {"port": 9090}}\n')
        r = _run_cli(home, ["diff", str(conf), "--baseline", "prod"],
                     store=store_path)
        assert r.returncode == 1, r.stderr
        assert "constraint" not in r.stdout
        assert "[WARN]" in r.stdout
        assert "max=WARN" in r.stdout

    def test_legal_change_json_identical_to_v050(self, tmp_path):
        home, store_path, conf = _setup(
            tmp_path, '{"server": {"port": 8080}}\n',
            '{"server": {"port": 9090}}\n')
        _run_cli(home, ["diff", str(conf), "--baseline", "prod"],
                 store=store_path)
        out = tmp_path / "report.json"
        _run_cli(home, ["report", "--json", str(out)], store=store_path)
        doc = json.loads(out.read_text(encoding="utf-8"))
        item = doc["data"]["items"][0]
        assert "constraint_violations" not in item
        assert item["severity"] == "WARN"

    def test_library_identical_with_and_without_constraints(self):
        """No-constraint calls behave byte-identically to v0.5.0."""
        differ = SemanticDiffer()
        old = {"f.json": {"a": 1, "b": "x"}}
        new = {"f.json": {"a": 2, "b": "y"}}
        items_plain, _ = differ.diff_snapshot(old, new)
        items_c, _ = differ.diff_snapshot(
            old, new, constraints=BUILTIN_CONSTRAINTS)
        assert [it.to_dict() for it in items_plain] == \
            [it.to_dict() for it in items_c]
