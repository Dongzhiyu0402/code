"""Engineer integration tests for cfgdrift v0.5.0 features (T03/T04).

Covers:

1. Alert rule retry: ``AlertRule.effective_retry`` semantics (count-only /
   delays-only / neither), ``to_dict`` / ``from_dict`` old-file compatibility,
   the CLI ``alert add --retry-count/--retry-delay`` flattening, and
   dispatcher retry count (5 attempts on failure).
2. HTML report export: ``HtmlReporter.render_html`` single-file offline page
   (colors, masked badge, no external references) and the CLI
   ``report --html`` path (mutually exclusive with ``--json``).
3. compare Web API: ``POST /api/compare`` 200/400/404 contract and the
   ``GET /api/reports/{id}/html`` export endpoint (FastAPI TestClient).
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cfgdrift.alert.dispatcher import AlertDispatcher  # noqa: E402
from cfgdrift.alert.models import AlertRule  # noqa: E402
from cfgdrift.alert.state import AlertStateStore  # noqa: E402
from cfgdrift.core.htmlreport import HtmlReporter  # noqa: E402
from cfgdrift.core.model import ChangeType, DriftItem, Report, ScanSummary, Severity  # noqa: E402
from cfgdrift.storage.store import Store  # noqa: E402

CLI_MAIN_AVAILABLE = True
try:
    from cfgdrift.cli import main as cli_main  # noqa: E402
except Exception:  # pragma: no cover - defensive
    CLI_MAIN_AVAILABLE = False

WEB_AVAILABLE = True
try:
    from fastapi.testclient import TestClient  # noqa: E402
except Exception:  # pragma: no cover - [web] extra may be missing
    WEB_AVAILABLE = False


def _noop_sleep(seconds):
    """Inject into dispatchers so retry tests never wait."""
    return None


def _item(key_path="server.port", change=ChangeType.MODIFIED, old=8080, new=9090,
          file="app.json", severity=Severity.WARN, line=3, masked=False,
          rule_id=None) -> DriftItem:
    return DriftItem(
        key_path=key_path,
        change_type=change,
        severity=severity,
        file=file,
        old_value=old,
        new_value=new,
        old_type="int",
        new_type="int",
        line=line,
        masked=masked,
        rule_id=rule_id,
    )


def _report(items):
    summary = ScanSummary()
    summary.max_severity = Severity.max_of(*(it.severity for it in items))
    return Report(
        scan_id=None,
        baseline=None,
        created_at="2026-08-03T00:00:00+00:00",
        mode="manual",
        summary=summary,
        items=items,
    )


# ---------------------------------------------------------------------------
# 1. Alert retry (T03)
# ---------------------------------------------------------------------------

class TestAlertRetryModel:
    def test_effective_retry_count_only(self):
        rule = AlertRule(name="r", type="webhook", config={}, retry_count=5)
        assert rule.effective_retry() == (5, (1, 5, 30))

    def test_effective_retry_delays_only(self):
        rule = AlertRule(
            name="r", type="webhook", config={}, retry_delays=[2, 10, 60]
        )
        attempts, delays = rule.effective_retry()
        assert attempts == 4  # len(delays) + 1 (D5)
        assert delays == (2.0, 10.0, 60.0)

    def test_effective_retry_both_and_default(self):
        rule = AlertRule(
            name="r",
            type="webhook",
            config={},
            retry_count=2,
            retry_delays=[1, 2],
        )
        assert rule.effective_retry() == (2, (1, 5, 30))  # count wins
        default = AlertRule(name="r", type="webhook", config={})
        assert default.effective_retry(3, (1, 5, 30)) == (3, (1, 5, 30))

    def test_validation(self):
        with pytest.raises(ValueError):
            AlertRule(name="r", type="webhook", config={}, retry_count=0)
        with pytest.raises(ValueError):
            AlertRule(name="r", type="webhook", config={}, retry_delays=[])
        with pytest.raises(ValueError):
            AlertRule(name="r", type="webhook", config={}, retry_delays=[-1])

    def test_to_dict_roundtrip_and_old_file_compat(self):
        rule = AlertRule(
            name="r", type="webhook", config={"url": "u"},
            retry_count=5, retry_delays=[1, 2],
        )
        d = rule.to_dict()
        assert d["retry_count"] == 5
        assert d["retry_delays"] == [1.0, 2.0]
        rebuilt = AlertRule.from_dict(d)
        assert rebuilt.retry_count == 5
        assert rebuilt.retry_delays == [1.0, 2.0]
        # Old alerts.yaml entries (no retry keys) default to None.
        old = AlertRule.from_dict(
            {"name": "old", "type": "webhook", "config": {"url": "u"}}
        )
        assert old.retry_count is None
        assert old.retry_delays is None
        # to_dict omits optional keys when unset (keeps the v1 schema clean).
        assert "retry_count" not in old.to_dict()


class TestAlertDispatcherRetry:
    def _dispatcher(self, rules, state):
        return AlertDispatcher(
            rules,
            state,
            retry_attempts=3,
            retry_delays=(1, 5, 30),
            sleep_fn=_noop_sleep,
        )

    def test_rule_retry_count_used_on_failure(self, tmp_path, monkeypatch):
        rule = AlertRule(
            name="flaky",
            type="webhook",
            config={"url": "http://127.0.0.1:1/unreachable", "timeout": 1},
            retry_count=5,
        )
        state = AlertStateStore(str(tmp_path / "state.json"), cooldown_seconds=600)

        calls = {"n": 0}

        class FailingChannel:
            def send(self, payload):
                calls["n"] += 1
                from cfgdrift.alert.channels import ChannelError

                raise ChannelError("always fails")

        monkeypatch.setattr(
            "cfgdrift.alert.dispatcher.build_channel", lambda rule: FailingChannel()
        )
        disp = self._dispatcher([rule], state)
        results = disp.dispatch_report("prod", "/etc/app", _report([_item()]))
        assert len(results) == 1
        assert results[0].sent is False
        assert results[0].attempts == 5
        assert calls["n"] == 5  # retry_count = total attempts (D5)

    def test_test_rule_uses_rule_retry(self, tmp_path, monkeypatch):
        rule = AlertRule(
            name="flaky2",
            type="webhook",
            config={"url": "http://127.0.0.1:1/unreachable", "timeout": 1},
            retry_delays=[1, 2, 3],  # -> 4 attempts
        )
        state = AlertStateStore(str(tmp_path / "state.json"))
        calls = {"n": 0}

        class FailingChannel:
            def send(self, payload):
                calls["n"] += 1
                from cfgdrift.alert.channels import ChannelError

                raise ChannelError("always fails")

        monkeypatch.setattr(
            "cfgdrift.alert.dispatcher.build_channel", lambda rule: FailingChannel()
        )
        disp = self._dispatcher([rule], state)
        result = disp.test_rule(rule)
        assert result.sent is False
        assert calls["n"] == 4
        assert result.attempts == 4

    def test_dedupe_cooldown_unchanged(self, tmp_path, monkeypatch):
        """The dedupe key/cooldown pipeline is untouched by rule retry."""
        rule = AlertRule(
            name="quiet",
            type="webhook",
            config={"url": "http://127.0.0.1:1/unreachable", "timeout": 1},
            retry_count=2,
        )
        state = AlertStateStore(str(tmp_path / "state.json"), cooldown_seconds=600)

        class FailingChannel:
            def send(self, payload):
                from cfgdrift.alert.channels import ChannelError

                raise ChannelError("fail")

        monkeypatch.setattr(
            "cfgdrift.alert.dispatcher.build_channel", lambda rule: FailingChannel()
        )
        disp = self._dispatcher([rule], state)
        results = disp.dispatch_report("prod", "/etc/app", _report([_item()]))
        assert len(results) == 1
        # A second dispatch of the same fingerprint is suppressed by cooldown.
        results2 = disp.dispatch_report("prod", "/etc/app", _report([_item()]))
        assert len(results2) == 0


@pytest.mark.skipif(not CLI_MAIN_AVAILABLE, reason="cli unavailable")
class TestAlertRetryCli:
    def test_alert_add_retry_options_and_list(self, tmp_path, capsys):
        home = str(tmp_path / "home")
        os.makedirs(home, exist_ok=True)
        old_home = os.environ.get("CFGDRIFT_HOME")
        os.environ["CFGDRIFT_HOME"] = home
        try:
            code = cli_main(
                [
                    "alert", "add", "--name", "web",
                    "--type", "webhook", "--url", "http://127.0.0.1:1/x",
                    "--retry-count", "5",
                ]
            )
            assert code == 0
            code = cli_main(
                [
                    "alert", "add", "--name", "mail",
                    "--type", "webhook", "--url", "http://127.0.0.1:1/y",
                    "--retry-delay", "2,10,60",
                ]
            )
            assert code == 0
            code = cli_main(["alert", "list"])
            assert code == 0
            out = capsys.readouterr().out
            assert "retry=5/1.0,5.0,30.0" in out
            assert "retry=4/2.0,10.0,60.0" in out
            # A rule without retry shows the default.
            code = cli_main(
                [
                    "alert", "add", "--name", "plain",
                    "--type", "webhook", "--url", "http://127.0.0.1:1/z",
                ]
            )
            assert code == 0
            code = cli_main(["alert", "list"])
            out = capsys.readouterr().out
            assert "retry=default" in out
        finally:
            if old_home is None:
                os.environ.pop("CFGDRIFT_HOME", None)
            else:
                os.environ["CFGDRIFT_HOME"] = old_home


# ---------------------------------------------------------------------------
# 2. HTML report export (T04)
# ---------------------------------------------------------------------------

class TestHtmlReporter:
    def _data(self, masked_item=False):
        # A masked item carries the mask value (mask_payload replaces values
        # before render_html in the real data flow, D6).
        password_value = "******" if masked_item else "hunter2"
        items = [
            {
                "key_path": "server.port",
                "change_type": "modified",
                "severity": "WARN",
                "file": "app.json",
                "old_value": 8080,
                "new_value": 9090,
                "old_type": "int",
                "new_type": "int",
                "rule_id": None,
                "line": 3,
                "masked": False,
            },
            {
                "key_path": "db.password",
                "change_type": "added",
                "severity": "CRITICAL",
                "file": "app.json",
                "old_value": None,
                "new_value": password_value,
                "old_type": None,
                "new_type": "str",
                "rule_id": 1,
                "line": 8,
                "masked": masked_item,
            },
        ]
        return {
            "scan_id": 7,
            "mode": "manual",
            "created_at": "2026-08-03T00:00:00+00:00",
            "baseline": {"name": "prod", "version": 2},
            "summary": {
                "added": 1, "removed": 0, "modified": 1,
                "type_changed": 0, "ignored": 0, "total": 2,
                "max_severity": "CRITICAL",
            },
            "items": items,
        }

    def test_render_html_structure(self):
        html = HtmlReporter.render_html(self._data(), title="cfgdrift report #7")
        assert html.startswith("<!DOCTYPE html>")
        assert "cfgdrift report #7" in html
        assert "漂移总数" in html
        assert "CRITICAL" in html
        assert "#ef4444" in html  # severity colors (Q4)
        assert "#f59e0b" in html
        assert "#22c55e" in html
        assert "#64748b" in html
        assert "server.port" in html
        assert "app.json:3" in html
        assert "8080" in html and "9090" in html
        assert "修改" in html

    def test_masked_item_badge(self):
        html = HtmlReporter.render_html(self._data(masked_item=True))
        assert "已脱敏" in html
        # Masked value must not leak.
        assert "hunter2" not in html

    def test_no_external_references(self):
        html = HtmlReporter.render_html(self._data())
        # Single-file offline constraint: no external http(s) references in
        # the emitted markup (no CDN / fonts / scripts).
        assert "http://" not in html
        assert "https://" not in html
        assert 'src="' not in html
        assert 'href="http' not in html

    def test_empty_items(self):
        data = self._data()
        data["items"] = []
        html = HtmlReporter.render_html(data)
        assert "无漂移项" in html


@pytest.mark.skipif(not CLI_MAIN_AVAILABLE, reason="cli unavailable")
class TestReportHtmlCli:
    def _setup_scan(self, home):
        """Create a store with one baseline + one drifting scan."""
        store = Store(os.path.join(home, "cfgdrift.db"))
        store.create_baseline(
            name="prod",
            description="",
            scan_root=os.path.abspath("."),
            format="json",
            data={"app.json": {"server": {"port": 8080}}},
            line_maps=None,
        )
        report = _report([_item()])
        payload = {"code": 0, "data": report.to_dict(), "message": "ok"}
        scan_id = store.add_scan(None, "manual", payload)
        store.close()
        return scan_id

    def test_report_html_cli(self, tmp_path, capsys):
        home = str(tmp_path / "home")
        os.makedirs(home, exist_ok=True)
        scan_id = self._setup_scan(home)
        out_path = str(tmp_path / "out.html")
        old_home = os.environ.get("CFGDRIFT_HOME")
        os.environ["CFGDRIFT_HOME"] = home
        try:
            code = cli_main(["report", "--scan-id", str(scan_id), "--html", out_path])
            assert code == 0
            assert os.path.exists(out_path)
            content = open(out_path, encoding="utf-8").read()
            assert "<!DOCTYPE html>" in content
            assert "server.port" in content
            assert "http://" not in content
            assert "written" in capsys.readouterr().out
        finally:
            if old_home is None:
                os.environ.pop("CFGDRIFT_HOME", None)
            else:
                os.environ["CFGDRIFT_HOME"] = old_home

    def test_report_json_and_html_mutually_exclusive(self, tmp_path):
        home = str(tmp_path / "home")
        os.makedirs(home, exist_ok=True)
        scan_id = self._setup_scan(home)
        old_home = os.environ.get("CFGDRIFT_HOME")
        os.environ["CFGDRIFT_HOME"] = home
        try:
            code = cli_main(
                [
                    "report", "--scan-id", str(scan_id),
                    "--json", str(tmp_path / "a.json"),
                    "--html", str(tmp_path / "a.html"),
                ]
            )
            assert code == 2
        finally:
            if old_home is None:
                os.environ.pop("CFGDRIFT_HOME", None)
            else:
                os.environ["CFGDRIFT_HOME"] = old_home


# ---------------------------------------------------------------------------
# 3. compare Web API (T04)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not WEB_AVAILABLE, reason="fastapi/httpx unavailable")
class TestCompareApi:
    @pytest.fixture()
    def client_and_store(self, tmp_path):
        store = Store(str(tmp_path / "cfgdrift.db"))
        store.create_baseline(
            name="dev",
            description="",
            scan_root=str(tmp_path),
            format="json",
            data={"app.json": {"server": {"port": 8080}}},
            line_maps=None,
        )
        store.create_baseline(
            name="prod",
            description="",
            scan_root=str(tmp_path),
            format="json",
            data={"app.json": {"server": {"port": 9090}}},
            line_maps=None,
        )
        from cfgdrift.web.app import create_app

        app = create_app(store, home=str(tmp_path))
        client = TestClient(app)
        yield client, store
        store.close()

    def test_compare_ok(self, client_and_store):
        client, _ = client_and_store
        resp = client.post("/api/compare", json={"env1": "dev", "env2": "prod"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 0
        data = body["data"]
        assert data["baseline_a"] == "dev"
        assert data["baseline_b"] == "prod"
        assert data["summary"]["total"] >= 1
        assert "snippet_root" in data["items"][0]

    def test_compare_missing_env_400(self, client_and_store):
        client, _ = client_and_store
        resp = client.post("/api/compare", json={"env1": "dev"})
        assert resp.status_code == 400
        assert "required" in resp.json()["message"]
        resp = client.post("/api/compare", json={})
        assert resp.status_code == 400

    def test_compare_same_env_400(self, client_and_store):
        client, _ = client_and_store
        resp = client.post("/api/compare", json={"env1": "dev", "env2": "dev"})
        assert resp.status_code == 400
        assert "different" in resp.json()["message"]

    def test_compare_missing_baseline_404(self, client_and_store):
        client, _ = client_and_store
        resp = client.post(
            "/api/compare", json={"env1": "dev", "env2": "ghost"}
        )
        assert resp.status_code == 404
        message = resp.json()["message"]
        assert "ghost" in message
        assert "未采集基线" in message

    def test_report_html_endpoint(self, client_and_store):
        client, store = client_and_store
        report = _report([_item()])
        payload = {"code": 0, "data": report.to_dict(), "message": "ok"}
        scan_id = store.add_scan(None, "manual", payload)
        resp = client.get("/api/reports/%d/html" % scan_id)
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "<!DOCTYPE html>" in resp.text
        assert "server.port" in resp.text
