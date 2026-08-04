"""Presentation tests for cfgdrift v0.6.0 — five display exits (T04).

Covers the composite-alert data flow (C-05):

1. terminal: ``Reporter.render_terminal`` appends
   ``constraint <id> [<type>]: <message>`` per violation.
2. json: ``DriftItem.to_dict`` emits ``constraint_violations`` only when
   non-empty (zero-noise D7).
3. html: ``HtmlReporter._items_table`` adds a「约束违反」column (empty -> ``-``).
4. Web: ``/api/reports/{id}`` carries ``constraint_violations`` via
   ``report_json`` (app.py unchanged); SPA table header updated.
5. alert payload: ``build_drift_payload`` adds a ``constraint`` field (first
   violation sorted by ``constraint_id``) only when the item has violations.

Plus Scenario A / B JSON assertions.
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

from cfgdrift.alert.models import build_drift_payload  # noqa: E402
from cfgdrift.core.constraints import BUILTIN_CONSTRAINTS  # noqa: E402
from cfgdrift.core.differ import SemanticDiffer  # noqa: E402
from cfgdrift.core.htmlreport import HtmlReporter  # noqa: E402
from cfgdrift.core.model import (  # noqa: E402
    ChangeType,
    DriftItem,
    Report,
    ScanSummary,
    Severity,
)
from cfgdrift.core.reporter import Reporter  # noqa: E402
from cfgdrift.storage.store import Store  # noqa: E402

WEB_AVAILABLE = True
try:
    from fastapi.testclient import TestClient  # noqa: E402
except Exception:  # pragma: no cover - [web] extra may be missing
    WEB_AVAILABLE = False


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


def _scenario_a_report():
    """tls.enabled false->true without cert_path/key_path."""
    differ = SemanticDiffer()
    items, summary = differ.diff_snapshot(
        {"nginx.conf": {"tls": {"enabled": False}, "server": {"port": 8080}}},
        {"nginx.conf": {"tls": {"enabled": True}, "server": {"port": 8080}}},
        constraints=BUILTIN_CONSTRAINTS,
    )
    return Report(None, None, "2026-08-04T00:00:00+00:00", "manual", summary, items)


def _scenario_b_items():
    """server.port 8080->99999 (out of range)."""
    differ = SemanticDiffer()
    items, summary = differ.diff_snapshot(
        {"app.json": {"server": {"port": 8080}}},
        {"app.json": {"server": {"port": 99999}}},
        constraints=BUILTIN_CONSTRAINTS,
    )
    return items, summary


# ---------------------------------------------------------------------------
# 1. terminal
# ---------------------------------------------------------------------------

class TestTerminal:
    def test_constraint_lines_appended(self):
        report = _scenario_a_report()
        text = Reporter().render_terminal(report, color=False, masker=None)
        assert "constraint http_ssl_cert_required [conditional_required]" in text
        assert "tls.cert_path 缺失（tls.enabled=true 需要该字段）" in text
        assert "tls.key_path 缺失" in text
        assert "CRITICAL tls.enabled" in text  # upgraded severity (plain mode)

    def test_zero_noise_no_extra_lines(self):
        differ = SemanticDiffer()
        items, summary = differ.diff_snapshot(
            {"app.json": {"server": {"port": 8080}}},
            {"app.json": {"server": {"port": 9090}}},
            constraints=BUILTIN_CONSTRAINTS,
        )
        report = Report(None, None, "2026-08-04T00:00:00+00:00", "manual",
                        summary, items)
        text = Reporter().render_terminal(report, color=False, masker=None)
        assert "constraint" not in text


# ---------------------------------------------------------------------------
# 2. json (to_dict conditional output)
# ---------------------------------------------------------------------------

class TestJson:
    def test_scenario_b_json_assertion(self):
        items, summary = _scenario_b_items()
        d = items[0].to_dict()
        assert d["severity"] == "CRITICAL"
        assert d["constraint_violations"][0]["constraint_id"] == "http_port_range"
        assert d["constraint_violations"][0]["type"] == "range"
        assert "involved_keys" in d["constraint_violations"][0]
        assert summary.max_severity == Severity.CRITICAL

    def test_scenario_a_json_assertion(self):
        report = _scenario_a_report()
        doc = json.loads(report.to_json())
        item = doc["data"]["items"][0]
        assert item["key_path"] == "tls.enabled"
        assert item["severity"] == "CRITICAL"
        cvs = item["constraint_violations"]
        assert len(cvs) == 2
        assert all(v["constraint_id"] == "http_ssl_cert_required" for v in cvs)
        assert any("tls.cert_path 缺失" in v["message"] for v in cvs)

    def test_zero_noise_no_field(self):
        differ = SemanticDiffer()
        items, _ = differ.diff_snapshot(
            {"app.json": {"server": {"port": 8080}}},
            {"app.json": {"server": {"port": 9090}}},
            constraints=BUILTIN_CONSTRAINTS,
        )
        assert "constraint_violations" not in items[0].to_dict()


# ---------------------------------------------------------------------------
# 3. html
# ---------------------------------------------------------------------------

class TestHtml:
    def test_items_table_constraint_column(self):
        items, summary = _scenario_b_items()
        data = {"items": [it.to_dict() for it in items]}
        html = HtmlReporter._items_table(data["items"])
        assert "约束违反" in HtmlReporter.render_html(
            data, title="t")  # header column exists
        assert "http_port_range" in html
        assert "[range]" in html
        assert "server.port 必须在 [1, 65535] 范围内" in html

    def test_html_empty_shows_dash(self):
        data = {"items": [{
            "key_path": "server.port",
            "change_type": "modified",
            "severity": "WARN",
            "file": "app.json",
            "old_value": 8080,
            "new_value": 9090,
            "rule_id": None,
            "line": None,
            "masked": False,
        }]}
        html = HtmlReporter._items_table(data["items"])
        # the constraint cell renders "-" for a violation-free item
        assert "<td>-</td>" in html


# ---------------------------------------------------------------------------
# 4. Web
# ---------------------------------------------------------------------------

class TestWeb:
    @pytest.mark.skipif(not WEB_AVAILABLE, reason="[web] extra not installed")
    def test_api_reports_carries_violations(self, tmp_path):
        from cfgdrift.web.app import create_app

        home = tmp_path / "home"
        store_path = tmp_path / "db" / "cfgdrift.db"
        conf = tmp_path / "conf"
        _write(str(conf / "app.json"),
               '{"server": {"port": 8080}, "tls": {"enabled": false}}\n')
        _run_cli(home, ["baseline", "create", "prod", "--scan-root", str(conf)],
                 store=store_path)
        _write(str(conf / "app.json"),
               '{"server": {"port": 99999}, "tls": {"enabled": true}}\n')
        r = _run_cli(home, ["diff", str(conf), "--baseline", "prod"],
                     store=store_path)
        assert r.returncode == 1, r.stderr
        store = Store(str(store_path))
        try:
            app = create_app(store, home=str(home))
            client = TestClient(app)
            res = client.get("/api/reports/1")
            assert res.status_code == 200, res.text
            items = res.json()["data"]["items"]
            by_key = {it["key_path"]: it for it in items}
            assert by_key["server.port"]["constraint_violations"]
            assert by_key["tls.enabled"]["constraint_violations"]
            assert by_key["server.port"]["severity"] == "CRITICAL"
            assert by_key["tls.enabled"]["severity"] == "CRITICAL"
        finally:
            store.close()

    @pytest.mark.skipif(not WEB_AVAILABLE, reason="[web] extra not installed")
    def test_spa_table_header_updated(self):
        from cfgdrift import web as web_pkg
        static_dir = os.path.join(os.path.dirname(web_pkg.__file__), "static")
        js = open(os.path.join(static_dir, "app.js"), encoding="utf-8").read()
        html = open(os.path.join(static_dir, "index.html"), encoding="utf-8").read()
        assert "constraint_violations" in js
        assert "cv-id" in js and "cv-id" in html
        assert "约束违反" in js


# ---------------------------------------------------------------------------
# 5. alert payload
# ---------------------------------------------------------------------------

class TestAlertPayload:
    def test_payload_constraint_field(self):
        report = _scenario_a_report()
        payload = build_drift_payload(report, "prod", "/etc/nginx", "0.6.0")
        items = payload["drift_items"]
        tls = [i for i in items if i["key"] == "tls.enabled"][0]
        assert tls["severity"] == "CRITICAL"
        cons = tls["constraint"]
        assert cons["id"] == "http_ssl_cert_required"
        assert cons["type"] == "conditional_required"
        assert "tls.cert_path 缺失" in cons["message"]
        assert cons["involved_keys"] == ["tls.enabled", "tls.cert_path"]
        # first violation chosen deterministically by constraint_id (both are
        # the same id here, so only one key is involved).

    def test_payload_first_violation_sorted_by_id(self):
        # an item with two different constraint violations -> pick by id
        item = DriftItem(
            key_path="server.port", change_type=ChangeType.MODIFIED,
            severity=Severity.CRITICAL, file="app.json",
            old_value=0, new_value=99999, old_type="int", new_type="int",
        )
        item.constraint_violations = [
            {"constraint_id": "zzz", "type": "range", "message": "zzz",
             "involved_keys": ["server.port"]},
            {"constraint_id": "aaa", "type": "range", "message": "aaa",
             "involved_keys": ["server.port"]},
        ]
        summary = ScanSummary()
        summary.max_severity = Severity.CRITICAL
        report = Report(None, None, "2026-08-04T00:00:00+00:00", "manual",
                        summary, [item])
        payload = build_drift_payload(report, "b", "t", "0.6.0")
        assert payload["drift_items"][0]["constraint"]["id"] == "aaa"

    def test_payload_no_constraint_field_when_clean(self):
        item = DriftItem(
            key_path="server.port", change_type=ChangeType.MODIFIED,
            severity=Severity.WARN, file="app.json",
            old_value=8080, new_value=9090, old_type="int", new_type="int",
        )
        summary = ScanSummary()
        summary.max_severity = Severity.WARN
        report = Report(None, None, "2026-08-04T00:00:00+00:00", "manual",
                        summary, [item])
        payload = build_drift_payload(report, "b", "t", "0.6.0")
        assert "constraint" not in payload["drift_items"][0]
