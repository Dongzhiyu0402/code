"""Round-6 QA supplementary tests for cfgdrift v0.4.0 (independent, skeptical).

Author: 严过关 (QA).  Written *after* reading the source, with the explicit
goal of not trusting the Engineer's "400 passed / 2 skipped" claim.  Covers:

a. Masking end-to-end across the four display exits (terminal / JSON report /
   Web API / alert payload) + DB raw-value direct query + type change + the
   ``--sensitive-keys`` append semantic + bare-key (``monkey``) non-masking.
b. Line numbers end-to-end for JSON/TOML/INI/YAML + the *key* dual-mode
   assertion (C vs pure line_map per-key equality) + REMOVED old-side
   fallback + compare-without-source ``line=None`` no error.
c. Custom severity: *.tls.enabled -> CRITICAL + summary.max_severity, built-in
   fallback for unmatched items, first-match-wins, invalid severity /
   change_type / regex -> exit 2, ``severity list`` source/enabled.
d. compare: exit codes 0/1/2, three-environment matrix, --json export,
   ``diff --compare`` alias parity.
e. alert_events: sent/failed records, cooldown suppression, alert test, prune.
f. Web: /api/alerts / /api/alert-events (filters) / /api/daemon-status /
   /api/overview -> 200 + structure; /api/file-snippet context + traversal.
g. Regression: v0.3.0 semantic diff / daemon status / alert CRUD / legacy
   parse_* / scan_path / diff_snapshot signatures.
"""

from __future__ import annotations

import json
import os
import sqlite3
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
from cfgdrift.alert.models import AlertRule, build_drift_payload  # noqa: E402
from cfgdrift.alert.state import AlertStateStore  # noqa: E402
from cfgdrift.core.compare import CompareEngine  # noqa: E402
from cfgdrift.core.differ import SemanticDiffer  # noqa: E402
from cfgdrift.core.masker import SensitiveMasker, masking_config_path  # noqa: E402
from cfgdrift.core.model import (  # noqa: E402
    ChangeType,
    DriftItem,
    Report,
    ScanSummary,
    Severity,
)
from cfgdrift.core.parser import parse_file_lines, parse_text, set_backend  # noqa: E402
from cfgdrift.daemon.daemon import DaemonManager  # noqa: E402
from cfgdrift.rules.severity import SeverityConfig  # noqa: E402
from cfgdrift.scanner.scanner import Scanner  # noqa: E402
from cfgdrift.storage.store import Store  # noqa: E402

_MASK = "******"


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def _run_cli(home, args, store=None, backend=None, extra_env=None):
    """Run the CLI in a subprocess with an isolated CFGDRIFT_HOME."""
    env = dict(os.environ)
    env["CFGDRIFT_HOME"] = str(home)
    if backend:
        env["CFGDRIFT_BACKEND"] = backend
    if extra_env:
        env.update(extra_env)
    cmd = [PY, "-m", "cfgdrift.cli"]
    if store:
        cmd += ["--store", str(store)]
    cmd += list(args)
    return subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=180)


def _store_rows(db_path, table, where=""):
    """Direct raw SQL read (bypasses the Store API) — skeptical DB assertions."""
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT * FROM %s %s" % (table, where)
        ).fetchall()
    finally:
        conn.close()


def _make_report(items, severity=Severity.WARN, baseline=None):
    summary = ScanSummary()
    for it in items:
        if it.change_type == ChangeType.ADDED:
            summary.added += 1
        elif it.change_type == ChangeType.REMOVED:
            summary.removed += 1
        elif it.change_type == ChangeType.TYPE_CHANGED:
            summary.type_changed += 1
        else:
            summary.modified += 1
    summary.max_severity = Severity.max_of(*(it.severity for it in items)) or severity
    return Report(None, baseline, "2026-08-03T00:00:00+00:00", "daemon",
                  summary, items)


# ===========================================================================
# a. Masking — four display exits + DB raw values
# ===========================================================================

SENSITIVE_JSON = """{
  "server": {
    "host": "prod",
    "password": "hunter2",
    "token": "tok-123",
    "client_secret": "sec-456"
  },
  "monkey": "banana"
}
"""


class TestMaskingFourExits:
    def _setup(self, tmp_path, drifted=False):
        home = tmp_path / "home"
        store_path = tmp_path / "db" / "cfgdrift.db"
        conf = tmp_path / "conf"
        _write(str(conf / "app.json"), SENSITIVE_JSON)
        r = _run_cli(home, ["baseline", "create", "prod",
                            "--scan-root", str(conf)], store=store_path)
        assert r.returncode == 0, r.stderr
        if drifted:
            d = SENSITIVE_JSON.replace('"host": "prod"', '"host": "prod-2"') \
                .replace('"password": "hunter2"', '"password": "newpw"') \
                .replace('"token": "tok-123"', '"token": "tok-999"') \
                .replace('"monkey": "banana"', '"monkey": "apple"')
            _write(str(conf / "app.json"), d)
        return home, store_path, conf

    def test_terminal_diff_masked(self, tmp_path):
        home, store_path, conf = self._setup(tmp_path, drifted=True)
        r = _run_cli(home, ["diff", str(conf), "--baseline", "prod"],
                     store=store_path)
        assert r.returncode == 1, r.stderr
        assert _MASK in r.stdout
        assert "hunter2" not in r.stdout and "newpw" not in r.stdout
        assert "tok-123" not in r.stdout and "tok-999" not in r.stdout
        # monkey changed too; its value must stay visible (not masked).
        assert '"banana" -> "apple"' in r.stdout

    def test_json_report_masked(self, tmp_path):
        home, store_path, conf = self._setup(tmp_path, drifted=True)
        _run_cli(home, ["diff", str(conf), "--baseline", "prod"],
                 store=store_path)
        out = tmp_path / "report.json"
        r = _run_cli(home, ["report", "--json", str(out)], store=store_path)
        # `report --json <path>` is a file export: it always returns 0.
        assert r.returncode == 0, r.stderr
        doc = json.loads(out.read_text(encoding="utf-8"))
        items = doc["data"]["items"]
        pw = [it for it in items if it["key_path"] == "server.password"]
        assert pw and pw[0]["old_value"] == _MASK
        assert pw[0]["new_value"] == _MASK
        assert pw[0]["masked"] is True
        assert pw[0]["old_type"] == "str"  # type preserved in JSON too

    def test_web_api_masked(self, tmp_path):
        home, store_path, conf = self._setup(tmp_path, drifted=True)
        _run_cli(home, ["diff", str(conf), "--baseline", "prod"],
                 store=store_path)
        from fastapi.testclient import TestClient

        from cfgdrift.web.app import create_app

        store = Store(str(store_path))
        try:
            app = create_app(store, home=str(home))
            client = TestClient(app)
            res = client.get("/api/reports/1")
            assert res.status_code == 200, res.text
            items = res.json()["data"]["items"]
            pw = [it for it in items if it["key_path"] == "server.password"]
            assert pw and pw[0]["old_value"] == _MASK
            assert pw[0]["masked"] is True
        finally:
            store.close()

    def test_alert_payload_masked(self, tmp_path):
        home, store_path, conf = self._setup(tmp_path, drifted=False)
        # Build a drift report with sensitive items and mask at the alert exit.
        items = [
            DriftItem("server.password", ChangeType.MODIFIED, Severity.WARN,
                      "app.json", old_value="hunter2", new_value="newpw",
                      old_type="str", new_type="str"),
            DriftItem("monkey", ChangeType.MODIFIED, Severity.WARN,
                      "app.json", old_value="banana", new_value="apple",
                      old_type="str", new_type="str"),
        ]
        report = _make_report(items, Severity.WARN)
        masker = SensitiveMasker.from_config(masking_config_path(str(home)))
        payload = build_drift_payload(report, "prod", str(conf), "0.4.0",
                                      masker=masker)
        pw = [i for i in payload["drift_items"] if i["key"] == "server.password"][0]
        assert pw["baseline"] == _MASK and pw["current"] == _MASK
        assert pw["masked"] is True
        mn = [i for i in payload["drift_items"] if i["key"] == "monkey"][0]
        assert mn["baseline"] == "banana"
        assert mn.get("masked", False) is False

    def test_db_keeps_raw_values(self, tmp_path):
        home, store_path, conf = self._setup(tmp_path, drifted=True)
        _run_cli(home, ["diff", str(conf), "--baseline", "prod"],
                 store=store_path)
        rows = _store_rows(
            store_path, "scan_items",
            "WHERE key_path='server.password' ORDER BY id DESC LIMIT 1",
        )
        assert rows, "no scan_items row for server.password"
        old_v, new_v = json.loads(rows[0][6]), json.loads(rows[0][7])
        assert old_v == "hunter2"
        assert new_v == "newpw"

    def test_type_change_masked_preserved(self, tmp_path):
        home = tmp_path / "home"
        store_path = tmp_path / "db" / "cfgdrift.db"
        conf = tmp_path / "conf"
        _write(str(conf / "app.json"), '{"password": "text"}\n')
        r = _run_cli(home, ["baseline", "create", "prod",
                            "--scan-root", str(conf)], store=store_path)
        assert r.returncode == 0, r.stderr
        _write(str(conf / "app.json"), '{"password": 12345}\n')
        r = _run_cli(home, ["diff", str(conf), "--baseline", "prod"],
                     store=store_path)
        assert r.returncode == 1, r.stderr
        assert _MASK in r.stdout
        assert "12345" not in r.stdout
        assert "类型变化" in r.stdout or "type_changed" in r.stdout

    def test_sensitive_keys_append_semantic(self, tmp_path):
        """--sensitive-keys appends; without it 'monkey' is NOT masked."""
        home = tmp_path / "home"
        store_path = tmp_path / "db" / "cfgdrift.db"
        conf = tmp_path / "conf"
        _write(str(conf / "app.json"),
               '{"monkey": "banana", "password": "p1"}\n')
        _run_cli(home, ["baseline", "create", "prod", "--scan-root", str(conf)],
                 store=store_path)
        _write(str(conf / "app.json"),
               '{"monkey": "coconut", "password": "p2"}\n')
        # Default: monkey visible.
        r1 = _run_cli(home, ["diff", str(conf), "--baseline", "prod"],
                      store=store_path)
        assert "coconut" in r1.stdout
        # With --sensitive-keys monkey: monkey masked, password still masked.
        r2 = _run_cli(home, ["diff", str(conf), "--baseline", "prod",
                             "--sensitive-keys", "monkey"], store=store_path)
        assert "coconut" not in r2.stdout
        assert _MASK in r2.stdout

    def test_masking_yaml_custom_mask(self, tmp_path):
        home = tmp_path / "home"
        _write(str(home / "masking.yaml"),
               "version: 1\nmask: '[[REDACTED]]'\n")
        store_path = tmp_path / "db" / "cfgdrift.db"
        conf = tmp_path / "conf"
        _write(str(conf / "app.json"), '{"password": "x"}\n')
        _run_cli(home, ["baseline", "create", "prod", "--scan-root", str(conf)],
                 store=store_path)
        _write(str(conf / "app.json"), '{"password": "y"}\n')
        r = _run_cli(home, ["diff", str(conf), "--baseline", "prod"],
                     store=store_path)
        assert "[[REDACTED]]" in r.stdout


# ===========================================================================
# b. Line numbers — four formats + dual-mode equality + fallback + None
# ===========================================================================

class TestLineNumbers:
    FORMAT_SAMPLES = {
        "json": ('{\n  "server": {\n    "host": "prod",\n    "port": 8080\n  },\n'
                 '  "monkey": "banana"\n}\n', "port", 4),
        "toml": ('[server]\nhost = "prod"\nport = 8080\n', "port", 3),
        "ini": ("[server]\nhost = prod\nport = 8080\n", "port", 3),
        "yaml": ("server:\n  host: prod\n  port: 8080\nmonkey: banana\n",
                 "port", 3),
    }

    def test_diff_line_numbers_four_formats(self, tmp_path):
        for fmt, (text, key, expected_line) in self.FORMAT_SAMPLES.items():
            home = tmp_path / ("home_" + fmt)
            store_path = tmp_path / ("db_" + fmt) / "cfgdrift.db"
            conf = tmp_path / ("conf_" + fmt)
            fname = "app." + fmt
            _write(str(conf / fname), text)
            r = _run_cli(home, ["baseline", "create", "prod",
                                "--scan-root", str(conf)],
                         store=store_path)
            assert r.returncode == 0, (fmt, r.stderr)
            drifted = text.replace('"port": 8080', '"port": 9090') \
                if fmt == "json" else \
                text.replace("port = 8080", "port = 9090") \
                if fmt in ("toml", "ini") else \
                text.replace("port: 8080", "port: 9090")
            _write(str(conf / fname), drifted)
            r = _run_cli(home, ["diff", str(conf), "--baseline", "prod"],
                         store=store_path)
            assert r.returncode == 1, (fmt, r.stderr)
            loc = "%s:%d" % (fname, expected_line)
            assert loc in r.stdout, (
                "fmt=%s expected %s in:\n%s" % (fmt, loc, r.stdout))

    def test_dual_mode_line_maps_equal_all_formats(self, tmp_path):
        """KEY ASSERTION: C and pure line maps are per-key equal for every fmt."""
        for fmt, (text, _key, _line) in self.FORMAT_SAMPLES.items():
            p = tmp_path / ("dual_%s.%s" % (fmt, fmt))
            _write(str(p), text)
            # C backend first (active by default on this machine).
            _, lm_c = parse_file_lines(str(p), fmt)
            set_backend("pure")
            try:
                _, lm_pure = parse_file_lines(str(p), fmt)
            finally:
                set_backend("auto")
            assert set(lm_c.keys()) == set(lm_pure.keys()), (
                "fmt=%s key sets differ: %s vs %s"
                % (fmt, sorted(lm_c), sorted(lm_pure)))
            for key in lm_c:
                assert lm_c[key] == lm_pure[key], (
                    "fmt=%s key=%r C=%r pure=%r" % (fmt, key, lm_c[key],
                                                    lm_pure[key]))

    def test_removed_item_line_falls_back_old_side(self, tmp_path):
        conf = tmp_path / "conf"
        f = conf / "app.json"
        _write(str(f), '{\n  "keep": 1,\n  "gone": 2\n}\n')
        scanner = Scanner()
        snapshot_old, lm_old = scanner.scan_path_with_lines(str(conf), "json")
        # Simulate a stored baseline (as baseline create would).
        store = Store(str(tmp_path / "db.db"))
        baseline = store.create_baseline(
            "prod", "", str(conf), "json", snapshot_old, line_maps=lm_old)
        _write(str(f), '{\n  "keep": 1\n}\n')
        snapshot_new, lm_new = scanner.scan_path_with_lines(str(conf), "json")
        items, summary = SemanticDiffer().diff_snapshot(
            baseline.data, snapshot_new,
            old_lines=baseline.line_maps, new_lines=lm_new,
        )
        removed = [it for it in items
                   if it.change_type == ChangeType.REMOVED]
        assert removed and removed[0].key_path == "gone"
        assert removed[0].line == 3  # old-side line (gone was on line 3)
        store.close()

    def test_compare_without_source_line_none(self, tmp_path):
        """compare over baselines without line_maps -> line=None, no crash."""
        store = Store(str(tmp_path / "db.db"))
        store.create_baseline("dev", "", str(tmp_path), "json",
                              {"app.json": {"port": 8080}})
        store.create_baseline("prod", "", str(tmp_path), "json",
                              {"app.json": {"port": 9090}})
        engine = CompareEngine(store)
        reports = engine.compare(["dev", "prod"])
        assert reports[0].summary.total == 1
        assert reports[0].items[0].line is None
        store.close()
        # CLI compare must not crash and must render without ":N".
        home = tmp_path / "home"
        r = _run_cli(home, ["compare", "dev", "prod"], store=store.db_path)
        assert r.returncode == 1, r.stderr
        assert "app.json" in r.stdout
        assert ":1" not in r.stdout.split("Summary")[0]


# ===========================================================================
# c. Custom severity
# ===========================================================================

TLS_JSON = """{
  "tls": {
    "enabled": true
  },
  "server": {
    "port": 8080
  }
}
"""


class TestCustomSeverity:
    def test_tls_rule_upgrades_to_critical(self, tmp_path):
        home = tmp_path / "home"
        store_path = tmp_path / "db" / "cfgdrift.db"
        conf = tmp_path / "conf"
        _write(str(conf / "app.json"), TLS_JSON)
        r = _run_cli(home, ["baseline", "create", "prod",
                            "--scan-root", str(conf)], store=store_path)
        assert r.returncode == 0, r.stderr
        r = _run_cli(home, ["severity", "add", "--name", "tls-crit",
                            "--severity", "CRITICAL",
                            "--key-pattern", r".*tls\.enabled"],
                     store=store_path)
        assert r.returncode == 0, r.stderr
        drifted = TLS_JSON.replace('"enabled": true', '"enabled": false')
        _write(str(conf / "app.json"), drifted)
        r = _run_cli(home, ["diff", str(conf), "--baseline", "prod"],
                     store=store_path)
        assert r.returncode == 1, r.stderr
        assert "[CRITICAL]" in r.stdout
        assert "max=CRITICAL" in r.stdout

    def test_unmatched_items_keep_builtin(self, tmp_path):
        home = tmp_path / "home"
        store_path = tmp_path / "db" / "cfgdrift.db"
        conf = tmp_path / "conf"
        _write(str(conf / "app.json"), TLS_JSON)
        _run_cli(home, ["baseline", "create", "prod", "--scan-root", str(conf)],
                 store=store_path)
        _run_cli(home, ["severity", "add", "--name", "tls-crit",
                        "--severity", "CRITICAL",
                        "--key-pattern", r".*tls\.enabled"], store=store_path)
        drifted = TLS_JSON.replace('"enabled": true', '"enabled": false') \
            .replace('"port": 8080', '"port": 9090')
        _write(str(conf / "app.json"), drifted)
        r = _run_cli(home, ["diff", str(conf), "--baseline", "prod"],
                     store=store_path)
        assert "[CRITICAL]" in r.stdout   # tls.enabled upgraded
        assert "[WARN]" in r.stdout       # server.port stays built-in WARN
        assert "max=CRITICAL" in r.stdout

    def test_first_match_wins(self, tmp_path):
        home = tmp_path / "home"
        store_path = tmp_path / "db" / "cfgdrift.db"
        conf = tmp_path / "conf"
        _write(str(conf / "app.json"), TLS_JSON)
        _run_cli(home, ["baseline", "create", "prod", "--scan-root", str(conf)],
                 store=store_path)
        # Two rules on the same key: the FIRST added must win (file order).
        _run_cli(home, ["severity", "add", "--name", "r1", "--severity",
                        "CRITICAL", "--key-pattern", r".*tls\.enabled"],
                 store=store_path)
        _run_cli(home, ["severity", "add", "--name", "r2", "--severity",
                        "INFO", "--key-pattern", r".*tls\.enabled"],
                 store=store_path)
        drifted = TLS_JSON.replace('"enabled": true', '"enabled": false')
        _write(str(conf / "app.json"), drifted)
        r = _run_cli(home, ["diff", str(conf), "--baseline", "prod"],
                     store=store_path)
        assert "[CRITICAL]" in r.stdout
        assert "max=CRITICAL" in r.stdout

    def test_severity_list_source_enabled(self, tmp_path):
        home = tmp_path / "home"
        store_path = tmp_path / "db" / "cfgdrift.db"
        _run_cli(home, ["severity", "add", "--name", "a", "--severity",
                        "WARN", "--key-pattern", "x.*"], store=store_path)
        _run_cli(home, ["severity", "add", "--name", "b", "--severity",
                        "CRITICAL", "--key-pattern", "y.*", "--disable"],
                 store=store_path)
        r = _run_cli(home, ["severity", "list"], store=store_path)
        assert r.returncode == 0
        assert "source=severity.yaml" in r.stdout
        assert "enabled=yes" in r.stdout and "enabled=no" in r.stdout
        # enable/disable round-trip
        _run_cli(home, ["severity", "enable", "b"], store=store_path)
        rules = SeverityConfig.list_rules(str(home / "severity.yaml"))
        assert {x.name: x.enabled for x in rules} == {"a": True, "b": True}
        _run_cli(home, ["severity", "disable", "b"], store=store_path)
        assert {x.name: x.enabled for x in
                SeverityConfig.list_rules(str(home / "severity.yaml"))} == \
            {"a": True, "b": False}

    def test_invalid_severity_exit2(self, tmp_path):
        home = tmp_path / "home"
        store_path = tmp_path / "db" / "cfgdrift.db"
        r = _run_cli(home, ["severity", "add", "--name", "x",
                            "--severity", "BOGUS"], store=store_path)
        assert r.returncode == 2

    def test_invalid_change_type_exit2(self, tmp_path):
        home = tmp_path / "home"
        store_path = tmp_path / "db" / "cfgdrift.db"
        r = _run_cli(home, ["severity", "add", "--name", "x",
                            "--severity", "CRITICAL", "--change-type", "nope"],
                     store=store_path)
        assert r.returncode == 2

    def test_invalid_regex_exit2(self, tmp_path):
        """Spec (Round-5 brief): an invalid regex must be rejected with exit 2.

        Currently the CLI accepts it silently (exit 0) — this test documents
        the gap.  See the Round-5 report; expected behavior per the brief is
        exit 2.
        """
        home = tmp_path / "home"
        store_path = tmp_path / "db" / "cfgdrift.db"
        r = _run_cli(home, ["severity", "add", "--name", "x",
                            "--severity", "CRITICAL", "--key-pattern", "["],
                     store=store_path)
        assert r.returncode == 2, (
            "invalid regex accepted (exit %d): %s" % (r.returncode, r.stdout))


# ===========================================================================
# d. compare
# ===========================================================================

class TestCompare:
    def _env_setup(self, tmp_path, ports=None):
        """dev/prod baselines; return (home, store_path, conf)."""
        home = tmp_path / "home"
        store_path = tmp_path / "db" / "cfgdrift.db"
        conf = tmp_path / "conf"
        ports = ports or {"dev": 8080, "prod": 9090}
        for env, port in ports.items():
            _write(str(conf / "app.json"),
                   '{"server": {"port": %d, "env": "%s"}}\n' % (port, env))
            r = _run_cli(home, ["baseline", "create", env,
                                "--scan-root", str(conf)], store=store_path)
            assert r.returncode == 0, r.stderr
        return home, store_path, conf

    def test_compare_exit_codes_and_labels(self, tmp_path):
        home, store_path, conf = self._env_setup(tmp_path)
        # prod vs dev differ -> 1
        r = _run_cli(home, ["compare", "dev", "prod"], store=store_path)
        assert r.returncode == 1, r.stderr
        assert "compare dev -> prod" in r.stdout  # environment labels
        # same vs same -> 0
        r = _run_cli(home, ["compare", "dev", "dev"], store=store_path)
        assert r.returncode == 0, r.stderr
        assert "no differences" in r.stdout
        # missing baseline -> 2
        r = _run_cli(home, ["compare", "dev", "nope"], store=store_path)
        assert r.returncode == 2

    def test_compare_three_env_matrix(self, tmp_path):
        home, store_path, conf = self._env_setup(
            tmp_path, ports={"dev": 8080, "prod": 9090, "staging": 7070})
        r = _run_cli(home, ["compare", "dev", "prod", "staging"],
                     store=store_path)
        assert r.returncode == 1, r.stderr
        assert "compare dev -> prod" in r.stdout
        assert "compare dev -> staging" in r.stdout
        assert r.stdout.count("Summary:") == 2  # two compared environments

    def test_compare_json_export(self, tmp_path):
        home, store_path, conf = self._env_setup(tmp_path)
        r = _run_cli(home, ["compare", "dev", "prod", "--json"],
                     store=store_path)
        assert r.returncode == 1, r.stderr
        payload = json.loads(r.stdout)
        assert payload["code"] == 0
        assert len(payload["data"]) == 1
        rep = payload["data"][0]
        assert rep["baseline_a"] == "dev"
        assert rep["baseline_b"] == "prod"
        assert rep["summary"]["total"] >= 1

    def test_diff_compare_alias_parity(self, tmp_path):
        home, store_path, conf = self._env_setup(tmp_path)
        r_cmp = _run_cli(home, ["compare", "dev", "prod"], store=store_path)
        r_alias = _run_cli(home, ["diff", "--compare", "--env1", "dev",
                                  "--env2", "prod"], store=store_path)
        assert r_alias.returncode == r_cmp.returncode == 1
        assert "compare dev -> prod" in r_alias.stdout
        # Same diff lines on the compared key.
        assert "server.port" in r_cmp.stdout
        assert "server.port" in r_alias.stdout

    def test_environments_yaml_mapping(self, tmp_path):
        home, store_path, conf = self._env_setup(tmp_path)
        # Map env name prod -> a differently-named baseline.
        _run_cli(home, ["baseline", "create", "prod-baseline",
                        "--scan-root", str(conf)], store=store_path)
        _write(str(home / "environments.yaml"),
               "version: 1\nenvironments:\n  prod: prod-baseline\n")
        r = _run_cli(home, ["compare", "dev", "prod"], store=store_path)
        assert r.returncode == 1, r.stderr
        assert "compare dev -> prod-baseline" in r.stdout


# ===========================================================================
# e. alert_events
# ===========================================================================

class TestAlertEvents:
    def _dispatcher(self, tmp_path, script_exit=0):
        home = tmp_path / "home"
        store_path = tmp_path / "db" / "cfgdrift.db"
        script = tmp_path / "notify.py"
        _write(str(script), "import sys\nsys.exit(%d)\n" % script_exit)
        r = _run_cli(home, ["alert", "add", "--name", "rule1", "--type",
                            "script", "--severity", "INFO",
                            "--command", sys.executable, "--arg", str(script)],
                     store=store_path)
        assert r.returncode == 0, r.stderr
        store = Store(str(store_path))
        rules = AlertConfig.load(str(home / "alerts.yaml"))
        state = AlertStateStore(str(home / "alert_state.json"))
        dispatcher = AlertDispatcher(
            rules, state, event_sink=store,
            retry_attempts=1, sleep_fn=lambda s: None,
        )
        return home, store, dispatcher, rules[0]

    def test_sent_event_recorded(self, tmp_path):
        _, store, dispatcher, _ = self._dispatcher(tmp_path, script_exit=0)
        try:
            report = _make_report([
                DriftItem("server.port", ChangeType.MODIFIED, Severity.WARN,
                          "app.json", 8080, 9090, "int", "int")], Severity.WARN)
            results = dispatcher.dispatch_report("prod", "/etc/app", report)
            assert any(res.sent for res in results)
            page = store.list_alert_events()
            assert page["total"] == 1
            ev = page["events"][0]
            assert ev["status"] == "sent"
            assert ev["rule"] == "rule1"
            assert ev["severity"] == "WARN"   # report max severity
            assert ev["baseline"] == "prod"
            assert ev["target"] == "/etc/app"
            assert ev["attempts"] == 1
            assert ev["error"] is None
            assert ev["fingerprint"]
        finally:
            store.close()

    def test_failed_event_recorded_with_error(self, tmp_path):
        _, store, dispatcher, _ = self._dispatcher(tmp_path, script_exit=3)
        try:
            report = _make_report([
                DriftItem("a", ChangeType.MODIFIED, Severity.WARN,
                          "f.json", 1, 2, "int", "int")], Severity.WARN)
            results = dispatcher.dispatch_report("prod", "/t", report)
            assert not results[0].sent
            page = store.list_alert_events()
            assert page["total"] == 1
            ev = page["events"][0]
            assert ev["status"] == "failed"
            assert ev["error"], "failed event must carry an error message"
            assert ev["attempts"] == 1
        finally:
            store.close()

    def test_cooldown_suppression_no_event(self, tmp_path):
        _, store, dispatcher, _ = self._dispatcher(tmp_path, script_exit=0)
        try:
            report = _make_report([
                DriftItem("a", ChangeType.MODIFIED, Severity.WARN,
                          "f.json", 1, 2, "int", "int")], Severity.WARN)
            dispatcher.dispatch_report("b", "/t", report)
            assert store.count_alert_events() == 1
            # Same fingerprint inside the cooldown -> suppressed, no event.
            dispatcher.dispatch_report("b", "/t", report)
            assert store.count_alert_events() == 1
        finally:
            store.close()

    def test_alert_test_writes_no_event(self, tmp_path):
        _, store, dispatcher, rule = self._dispatcher(tmp_path, script_exit=0)
        try:
            dispatcher.test_rule(rule)
            assert store.count_alert_events() == 0
        finally:
            store.close()

    def test_prune_removes_backdated_rows(self, tmp_path):
        home = tmp_path / "home"
        store_path = tmp_path / "db" / "cfgdrift.db"
        store = Store(str(store_path))
        try:
            store.add_alert_event({"rule": "r", "baseline": "b",
                                   "severity": "WARN", "status": "sent"})
            store.add_alert_event({"rule": "r2", "baseline": "b",
                                   "severity": "WARN", "status": "sent"})
            # Backdate both to 40 days ago -> both pruned by the 30-day rule.
            store._conn.execute(
                "UPDATE alert_events SET created_at = "
                "'2020-01-01T00:00:00+00:00' WHERE 1=1")
            store._conn.commit()
            removed = store.prune_alert_events(days=30, max_rows=5000)
            assert removed == 2
            assert store.count_alert_events() == 0
        finally:
            store.close()


# ===========================================================================
# f. Web API
# ===========================================================================

class TestWebApi:
    def _client(self, tmp_path, home=None):
        from fastapi.testclient import TestClient

        from cfgdrift.web.app import create_app

        home = home or (tmp_path / "home")
        store = Store(str(tmp_path / "db" / "cfgdrift.db"))
        app = create_app(store, home=str(home))
        return TestClient(app), store

    def test_endpoints_200_and_structure(self, tmp_path):
        home = tmp_path / "home"
        store_path = tmp_path / "db" / "cfgdrift.db"
        conf = tmp_path / "conf"
        _write(str(conf / "app.json"), '{"server": {"port": 8080}}\n')
        _run_cli(home, ["baseline", "create", "prod", "--scan-root",
                        str(conf)], store=store_path)
        # Seed one alert event directly.
        store0 = Store(str(store_path))
        store0.add_alert_event({"rule": "r1", "baseline": "prod",
                                "severity": "WARN", "status": "sent",
                                "target": "/t", "drift_count": 1,
                                "attempts": 1})
        store0.close()
        client, store = self._client(tmp_path, home)
        try:
            # /api/alerts
            res = client.get("/api/alerts")
            assert res.status_code == 200 and res.json()["code"] == 0
            assert "alerts" in res.json()["data"]
            # /api/alert-events with filters + pagination
            res = client.get("/api/alert-events",
                             params={"status": "sent", "limit": 1})
            assert res.status_code == 200
            data = res.json()["data"]
            assert data["total"] == 1 and len(data["events"]) == 1
            assert data["events"][0]["rule"] == "r1"
            res = client.get("/api/alert-events", params={"status": "failed"})
            assert res.json()["data"]["total"] == 0
            # /api/daemon-status
            res = client.get("/api/daemon-status")
            assert res.status_code == 200 and res.json()["code"] == 0
            st = res.json()["data"]
            assert "running" in st and "last_scan" in st
            # /api/overview
            res = client.get("/api/overview")
            assert res.status_code == 200 and res.json()["code"] == 0
            ov = res.json()["data"]
            for k in ("latest_scan", "timeline", "severity_distribution",
                      "totals", "baseline_count", "scan_count",
                      "daemon_status"):
                assert k in ov, k
            # /api/health
            assert client.get("/api/health").json()["data"] == {"status": "ok"}
        finally:
            store.close()

    def test_file_snippet_context_and_traversal(self, tmp_path):
        home = tmp_path / "home"
        store_path = tmp_path / "db" / "cfgdrift.db"
        conf = tmp_path / "conf"
        # 21 physical lines of *valid* JSON (line 10 sits mid-file).
        body = "{\n" + "".join('  "a%d": %d,\n' % (i, i) for i in range(18)) \
            + '  "a18": 18\n}\n'
        assert body.count("\n") == 21
        _write(str(conf / "app.json"), body)
        _run_cli(home, ["baseline", "create", "prod", "--scan-root",
                        str(conf)], store=store_path)
        client, store = self._client(tmp_path, home)
        try:
            res = client.get("/api/file-snippet",
                             params={"root": str(conf), "file": "app.json",
                                     "line": 10})
            assert res.status_code == 200, res.text
            data = res.json()["data"]
            assert data["line"] == 10
            assert data["total_lines"] == 21
            lines_around = [s["line"] for s in data["snippet"]]
            assert 10 in lines_around
            assert 9 in lines_around and 11 in lines_around  # ±1 context
            assert all(1 <= n <= 21 for n in lines_around)  # bounded
            # Traversal 1: root is the parent of the real scan root -> 403
            res2 = client.get("/api/file-snippet",
                              params={"root": str(tmp_path),
                                      "file": "conf/app.json", "line": 1})
            assert res2.status_code == 403, res2.text
            # Traversal 2: file escapes root via .. -> 403
            _write(str(tmp_path / "secret.txt"), "top-secret\n")
            res3 = client.get("/api/file-snippet",
                              params={"root": str(conf),
                                      "file": "../secret.txt", "line": 1})
            assert res3.status_code == 403, res3.text
            # Unknown root -> 403
            res4 = client.get("/api/file-snippet",
                              params={"root": str(tmp_path / "other"),
                                      "file": "app.json", "line": 1})
            assert res4.status_code == 403, res4.text
        finally:
            store.close()


# ===========================================================================
# g. Regression — v0.3.0 semantics + legacy contracts
# ===========================================================================

class TestRegression:
    def test_v030_semantic_diff_categories(self):
        differ = SemanticDiffer()
        old = {"f.json": {"a": 1, "b": 2, "c": "x", "d": 1}}
        new = {"f.json": {"a": 1, "b": 3, "c": 5, "e": 9}}
        items, summary = differ.diff_snapshot(old, new)
        kinds = {it.change_type for it in items}
        assert ChangeType.MODIFIED in kinds       # b
        assert ChangeType.TYPE_CHANGED in kinds   # c str -> int
        assert ChangeType.ADDED in kinds          # e
        assert ChangeType.REMOVED in kinds        # d
        assert summary.total == 4

    def test_v030_daemon_status(self, tmp_path):
        mgr = DaemonManager(str(tmp_path / "home"))
        st = mgr.status_dict()
        assert st["running"] is False
        assert st["pid"] is None

    def test_v030_alert_crud(self, tmp_path):
        home = tmp_path / "home"
        store_path = tmp_path / "db" / "cfgdrift.db"
        r = _run_cli(home, ["alert", "add", "--name", "w", "--type",
                            "webhook", "--severity", "CRITICAL",
                            "--url", "http://127.0.0.1:1/x"], store=store_path)
        assert r.returncode == 0, r.stderr
        r = _run_cli(home, ["alert", "list"], store=store_path)
        assert "w" in r.stdout and "webhook" in r.stdout
        r = _run_cli(home, ["alert", "remove", "w"], store=store_path)
        assert r.returncode == 0, r.stderr
        r = _run_cli(home, ["alert", "list"], store=store_path)
        assert "no alert rules" in r.stdout

    def test_legacy_parse_and_scan_signatures(self, tmp_path):
        # parse_text old signature
        tree = parse_text('{"a": {"b": 1}}', "json")
        assert tree == {"a": {"b": 1}}
        # parse_file old signature
        p = tmp_path / "legacy.json"
        _write(str(p), '{"x": 1}\n')
        from cfgdrift.core.parser import parse_file
        assert parse_file(str(p), "json") == {"x": 1}
        # scan_path old signature
        from cfgdrift.scanner.scanner import Scanner
        snap = Scanner().scan_path(str(p), "json")
        assert snap == {"legacy.json": {"x": 1}}
        # diff_snapshot old signature
        differ = SemanticDiffer()
        items, summary = differ.diff_snapshot({"f": {"a": 1}}, {"f": {"a": 2}})
        assert items[0].key_path == "a" and summary.total == 1

    def test_version_contract(self):
        import cfgdrift
        from cfgdrift import _cfgdrift

        assert cfgdrift.__version__ == "0.4.0"
        assert _cfgdrift.version() == "0.4.0-c"
