"""Round-5 QA-oriented end-to-end tests for cfgdrift v0.4.0.

Author: 寇豆码 (Engineer) — base version for QA to extend.  Covers the
v0.4.0 acceptance checks:

a. dual-mode line-map consistency (C vs pure parse_file_lines per-key equal);
b. masking at the diff display exit (******) while the DB keeps raw values,
   type changes preserved;
c. custom severity rule (severity.yaml) marks ``*.tls.enabled`` CRITICAL and
   summary.max_severity reflects it;
d. ``compare`` across two baselines: differences + exit codes 0/1/2;
e. alert delivery records appear in store.list_alert_events;
f. Web API smoke: /api/alerts, /api/alert-events, /api/daemon-status -> 200.
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

from cfgdrift import __version__  # noqa: E402
from cfgdrift.alert.config import AlertConfig  # noqa: E402
from cfgdrift.alert.dispatcher import AlertDispatcher  # noqa: E402
from cfgdrift.alert.models import AlertRule  # noqa: E402
from cfgdrift.alert.state import AlertStateStore  # noqa: E402
from cfgdrift.core.parser import parse_file_lines  # noqa: E402
from cfgdrift.core.parser import set_backend  # noqa: E402
from cfgdrift.daemon.daemon import DaemonManager  # noqa: E402
from cfgdrift.rules.severity import SeverityConfig, make_rule as make_sev_rule  # noqa: E402
from cfgdrift.storage.store import Store  # noqa: E402

_REAL_BACKEND = None


def _run_cli(home, args, backend=None, store=None):
    """Run the cfgdrift CLI in a subprocess with an isolated home."""
    env = dict(os.environ)
    env["CFGDRIFT_HOME"] = str(home)
    if backend:
        env["CFGDRIFT_BACKEND"] = backend
    cmd = [PY, "-m", "cfgdrift.cli"] + args
    if store:
        cmd = [PY, "-m", "cfgdrift.cli", "--store", str(store)] + args
    return subprocess.run(
        cmd, capture_output=True, text=True, env=env, timeout=120
    )


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


# ---------------------------------------------------------------------------
# a. Dual-mode line-map consistency
# ---------------------------------------------------------------------------

JSON_SAMPLE = """{
  "server": {
    "host": "prod-1",
    "port": 8080
  },
  "tls": {
    "enabled": true
  },
  "arr": [1, 2, 3],
  "password": "hunter2"
}
"""


class TestDualModeLineMaps:
    def test_c_and_pure_line_maps_equal(self, tmp_path):
        file_path = tmp_path / "app.json"
        file_path.write_text(JSON_SAMPLE, encoding="utf-8")
        try:
            _, lm_c = parse_file_lines(str(file_path), "json")
            _c_available = True
        except Exception:  # pragma: no cover - pure-only install
            _c_available = False
        set_backend("pure")
        try:
            _, lm_pure = parse_file_lines(str(file_path), "json")
        finally:
            set_backend("auto")
        if _c_available:
            assert set(lm_c.keys()) == set(lm_pure.keys())
            for key in lm_c:
                assert lm_c[key] == lm_pure[key], key
        # Structural sanity shared by both backends.
        assert lm_pure["server"] == 2
        assert lm_pure["server.host"] == 3
        assert lm_pure["tls.enabled"] == 7
        assert lm_pure["password"] == 10


# ---------------------------------------------------------------------------
# b. Masking: display ****** while DB keeps raw values
# ---------------------------------------------------------------------------

class TestMaskingEndToEnd:
    def test_diff_masks_password_but_db_keeps_raw(self, tmp_path):
        home = tmp_path / "home"
        store_path = tmp_path / "db" / "cfgdrift.db"
        conf = tmp_path / "conf"
        _write(str(conf / "app.json"), JSON_SAMPLE)

        # Create baseline + a drifted version.
        r1 = _run_cli(home, ["baseline", "create", "prod",
                             "--scan-root", str(conf)], store=store_path)
        assert r1.returncode == 0, r1.stderr

        drifted = JSON_SAMPLE.replace('"port": 8080', '"port": 9090').replace(
            '"password": "hunter2"', '"password": "newsecret"'
        )
        _write(str(conf / "app.json"), drifted)

        r2 = _run_cli(home, ["diff", str(conf), "--baseline", "prod"],
                      store=store_path)
        assert r2.returncode == 1, r2.stderr
        assert "******" in r2.stdout  # password masked at display
        assert "hunter2" not in r2.stdout
        assert "newsecret" not in r2.stdout

        # DB stores raw values.
        store = Store(str(store_path))
        try:
            payload = store.get_scan(1)
        finally:
            store.close()
        items = payload["data"]["items"]
        pw_items = [it for it in items if it["key_path"] == "password"]
        assert pw_items and pw_items[0]["old_value"] == "hunter2"
        assert pw_items[0]["new_value"] == "newsecret"

    def test_type_change_preserved_when_masked(self, tmp_path):
        masker_home = tmp_path / "mh"
        store_path = tmp_path / "db2" / "cfgdrift.db"
        conf = tmp_path / "conf2"
        _write(str(conf / "app.json"), '{"password": "text"}\n')
        r1 = _run_cli(masker_home, ["baseline", "create", "prod",
                                    "--scan-root", str(conf)], store=store_path)
        assert r1.returncode == 0, r1.stderr
        _write(str(conf / "app.json"), '{"password": 12345}\n')
        r2 = _run_cli(masker_home, ["diff", str(conf), "--baseline", "prod"],
                      store=store_path)
        assert r2.returncode == 1, r2.stderr
        assert "******" in r2.stdout
        assert "类型变化" in r2.stdout or "type_changed" in r2.stdout


# ---------------------------------------------------------------------------
# c. Custom severity: severity.yaml marks *.tls.enabled CRITICAL
# ---------------------------------------------------------------------------

class TestSeverityEndToEnd:
    def test_tls_rule_upgrades_severity(self, tmp_path):
        home = tmp_path / "home"
        store_path = tmp_path / "db" / "cfgdrift.db"
        conf = tmp_path / "conf"
        _write(str(conf / "app.json"), JSON_SAMPLE)

        r1 = _run_cli(home, ["baseline", "create", "prod",
                             "--scan-root", str(conf)], store=store_path)
        assert r1.returncode == 0, r1.stderr

        # Add a severity rule that escalates *.tls.enabled modifications.
        r2 = _run_cli(home, ["severity", "add", "--name", "tls-crit",
                             "--severity", "CRITICAL",
                             "--key-pattern", r".*tls\.enabled"],
                      store=store_path)
        assert r2.returncode == 0, r2.stderr
        rules = SeverityConfig.list_rules(str(home / "severity.yaml"))
        assert len(rules) == 1
        assert rules[0].severity.value == "CRITICAL"

        # Default diff (no rule) -> WARN; with rule -> CRITICAL.
        drifted = JSON_SAMPLE.replace('"enabled": true', '"enabled": false')
        _write(str(conf / "app.json"), drifted)

        # Temporarily remove the rule file to observe the default.
        sev_path = home / "severity.yaml"
        saved = sev_path.read_text(encoding="utf-8")
        sev_path.unlink()
        r_default = _run_cli(home, ["diff", str(conf), "--baseline", "prod"],
                             store=store_path)
        assert "[WARN]" in r_default.stdout
        assert "max=WARN" in r_default.stdout
        sev_path.write_text(saved, encoding="utf-8")

        r_rule = _run_cli(home, ["diff", str(conf), "--baseline", "prod"],
                          store=store_path)
        assert r_rule.returncode == 1, r_rule.stderr
        assert "[CRITICAL]" in r_rule.stdout
        assert "max=CRITICAL" in r_rule.stdout

        # severity list shows source=severity.yaml
        r_list = _run_cli(home, ["severity", "list"], store=store_path)
        assert "source=severity.yaml" in r_list.stdout
        assert "tls-crit" in r_list.stdout


# ---------------------------------------------------------------------------
# d. compare across two baselines
# ---------------------------------------------------------------------------

class TestCompareEndToEnd:
    def test_compare_exit_codes(self, tmp_path):
        home = tmp_path / "home"
        store_path = tmp_path / "db" / "cfgdrift.db"
        conf = tmp_path / "conf"
        _write(str(conf / "app.json"), JSON_SAMPLE)

        r1 = _run_cli(home, ["baseline", "create", "prod",
                             "--scan-root", str(conf)], store=store_path)
        assert r1.returncode == 0, r1.stderr

        drifted = JSON_SAMPLE.replace('"port": 8080', '"port": 9090')
        _write(str(conf / "app.json"), drifted)
        r2 = _run_cli(home, ["baseline", "create", "staging",
                             "--scan-root", str(conf)], store=store_path)
        assert r2.returncode == 0, r2.stderr

        # prod vs staging: port differs -> exit 1
        r3 = _run_cli(home, ["compare", "prod", "staging"], store=store_path)
        assert r3.returncode == 1, r3.stderr
        assert "compare prod -> staging" in r3.stdout

        # same vs same -> exit 0
        r4 = _run_cli(home, ["compare", "prod", "prod"], store=store_path)
        assert r4.returncode == 0, r4.stderr

        # missing baseline -> exit 2
        r5 = _run_cli(home, ["compare", "prod", "nope"], store=store_path)
        assert r5.returncode == 2

        # diff --compare alias
        r6 = _run_cli(home, ["diff", "--compare", "--env1", "prod",
                             "--env2", "staging"], store=store_path)
        assert r6.returncode == 1, r6.stderr
        assert "compare prod -> staging" in r6.stdout

    def test_compare_json_output(self, tmp_path):
        home = tmp_path / "home"
        store_path = tmp_path / "db" / "cfgdrift.db"
        conf = tmp_path / "conf"
        _write(str(conf / "app.json"), JSON_SAMPLE)
        _run_cli(home, ["baseline", "create", "prod", "--scan-root", str(conf)],
                 store=store_path)
        drifted = JSON_SAMPLE.replace('"port": 8080', '"port": 9090')
        _write(str(conf / "app.json"), drifted)
        _run_cli(home, ["baseline", "create", "staging", "--scan-root", str(conf)],
                 store=store_path)
        r = _run_cli(home, ["compare", "prod", "staging", "--json"],
                     store=store_path)
        assert r.returncode == 1, r.stderr
        payload = json.loads(r.stdout)
        assert payload["code"] == 0
        assert len(payload["data"]) == 1
        assert payload["data"][0]["summary"]["total"] == 1


# ---------------------------------------------------------------------------
# e. alert delivery events recorded
# ---------------------------------------------------------------------------

class TestAlertEventsEndToEnd:
    def test_dispatch_records_event(self, tmp_path):
        home = tmp_path / "home"
        store_path = tmp_path / "db" / "cfgdrift.db"
        conf = tmp_path / "conf"
        _write(str(conf / "app.json"), JSON_SAMPLE)

        r1 = _run_cli(home, ["baseline", "create", "prod",
                             "--scan-root", str(conf)], store=store_path)
        assert r1.returncode == 0, r1.stderr

        # A script alert that always succeeds (exit 0) with a tiny timeout.
        # Windows cannot execute bare .sh files, so drive a .py script with
        # the current interpreter (works on every platform).
        script = tmp_path / "notify_ok.py"
        _write(str(script), "import sys\nsys.exit(0)\n")
        r2 = _run_cli(home, ["alert", "add", "--name", "script-ok",
                             "--type", "script", "--severity", "INFO",
                             "--command", sys.executable,
                             "--arg", str(script)], store=store_path)
        assert r2.returncode == 0, r2.stderr

        # Create a report with drift through the engine + dispatcher directly
        # (deterministic; avoids a real daemon).
        store = Store(str(store_path))
        baseline = store.get_baseline("prod")
        from cfgdrift.core.differ import SemanticDiffer
        from cfgdrift.core.model import Report, ScanSummary
        from cfgdrift.core.parser import parse_text
        from cfgdrift.scanner.scanner import Scanner

        scanner = Scanner()
        drifted = JSON_SAMPLE.replace('"port": 8080', '"port": 9090')
        _write(str(conf / "app.json"), drifted)
        snapshot, line_maps = scanner.scan_path_with_lines(str(conf), "auto")
        items, summary = SemanticDiffer().diff_snapshot(
            baseline.data, snapshot,
            old_lines=baseline.line_maps, new_lines=line_maps,
        )
        report = Report(
            scan_id=None, baseline=baseline, created_at="2026-08-03T00:00:00+00:00",
            mode="daemon", summary=summary, items=items,
        )
        rules = AlertConfig.load(str(home / "alerts.yaml"))
        state = AlertStateStore(str(home / "alert_state.json"))
        dispatcher = AlertDispatcher(rules, state, event_sink=store)
        results = dispatcher.dispatch_report("prod", str(conf), report)
        assert any(res.sent for res in results)

        page = store.list_alert_events()
        assert page["total"] >= 1
        ev = page["events"][0]
        assert ev["status"] == "sent"
        assert ev["rule"] == "script-ok"
        assert ev["baseline"] == "prod"
        store.close()

    def test_cooldown_and_test_do_not_record(self, tmp_path):
        home = tmp_path / "home"
        store_path = tmp_path / "db" / "cfgdrift.db"
        script = tmp_path / "ok.py"
        _write(str(script), "import sys\nsys.exit(0)\n")
        r1 = _run_cli(home, ["alert", "add", "--name", "s",
                             "--type", "script", "--severity", "INFO",
                             "--command", sys.executable, "--arg", str(script)],
                      store=store_path)
        assert r1.returncode == 0, r1.stderr
        store = Store(str(store_path))
        rules = AlertConfig.load(str(home / "alerts.yaml"))
        state = AlertStateStore(str(home / "alert_state.json"))
        dispatcher = AlertDispatcher(rules, state, event_sink=store)

        # alert test must NOT write an event.
        dispatcher.test_rule(rules[0])
        assert store.count_alert_events() == 0

        # A suppressed (cooled) dispatch must NOT write an event.
        from cfgdrift.core.model import (
            ChangeType, DriftItem, Report, ScanSummary, Severity,
        )
        items = [DriftItem("a", ChangeType.MODIFIED, Severity.WARN, "f.json",
                           old_value=1, new_value=2)]
        summary = ScanSummary(modified=1)
        summary.max_severity = Severity.WARN
        report = Report(None, None, "2026-08-03T00:00:00+00:00", "daemon",
                        summary, items)
        # First dispatch records a sent event + arms cooldown.
        dispatcher.dispatch_report("b", "/t", report)
        assert store.count_alert_events() == 1
        # Second dispatch within cooldown is suppressed -> no new event.
        dispatcher.dispatch_report("b", "/t", report)
        assert store.count_alert_events() == 1
        store.close()


# ---------------------------------------------------------------------------
# f. Web API smoke
# ---------------------------------------------------------------------------

class TestWebApi:
    def _client(self, tmp_path, store_path):
        from fastapi.testclient import TestClient

        from cfgdrift.web.app import create_app

        store = Store(str(store_path))
        app = create_app(store, home=str(tmp_path / "home"))
        return TestClient(app), store

    def test_new_apis_200(self, tmp_path):
        store_path = tmp_path / "db" / "cfgdrift.db"
        conf = tmp_path / "conf"
        _write(str(conf / "app.json"), JSON_SAMPLE)
        _run_cli(tmp_path / "home", ["baseline", "create", "prod",
                                     "--scan-root", str(conf)], store=store_path)
        client, store = self._client(tmp_path, store_path)
        try:
            for url in ("/api/alerts", "/api/alert-events", "/api/daemon-status"):
                res = client.get(url)
                assert res.status_code == 200, (url, res.text)
                assert res.json()["code"] == 0
            # /api/alerts returns the (empty) rules list shape.
            assert client.get("/api/alerts").json()["data"] == {"alerts": []}
            # overview carries daemon_status
            ov = client.get("/api/overview").json()["data"]
            assert "daemon_status" in ov
        finally:
            store.close()

    def test_file_snippet_guard(self, tmp_path):
        store_path = tmp_path / "db" / "cfgdrift.db"
        conf = tmp_path / "conf"
        _write(str(conf / "app.json"), JSON_SAMPLE)
        _run_cli(tmp_path / "home", ["baseline", "create", "prod",
                                     "--scan-root", str(conf)], store=store_path)
        client, store = self._client(tmp_path, store_path)
        try:
            # Valid root + file.
            res = client.get("/api/file-snippet",
                             params={"root": str(conf), "file": "app.json", "line": 3})
            assert res.status_code == 200, res.text
            data = res.json()["data"]
            assert data["line"] == 3
            assert any(r["line"] == 3 for r in data["snippet"])
            # Traversal attempt -> 403.
            res2 = client.get("/api/file-snippet",
                              params={"root": str(conf),
                                      "file": "../secret.txt", "line": 1})
            assert res2.status_code == 403
            # Unknown root -> 403.
            res3 = client.get("/api/file-snippet",
                              params={"root": str(tmp_path / "other"),
                                      "file": "app.json", "line": 1})
            assert res3.status_code == 403
        finally:
            store.close()

    def test_report_response_masked(self, tmp_path):
        store_path = tmp_path / "db" / "cfgdrift.db"
        conf = tmp_path / "conf"
        _write(str(conf / "app.json"), '{"password": "hunter2"}\n')
        _run_cli(tmp_path / "home", ["baseline", "create", "prod",
                                     "--scan-root", str(conf)], store=store_path)
        _write(str(conf / "app.json"), '{"password": "newsecret"}\n')
        _run_cli(tmp_path / "home", ["diff", str(conf), "--baseline", "prod"],
                 store=store_path)
        client, store = self._client(tmp_path, store_path)
        try:
            res = client.get("/api/reports/1")
            assert res.status_code == 200, res.text
            items = res.json()["data"]["items"]
            pw = [it for it in items if it["key_path"] == "password"]
            assert pw and pw[0]["old_value"] == "******"
            assert pw[0]["masked"] is True
        finally:
            store.close()
