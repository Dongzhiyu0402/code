"""QA round 6 — independent verification tests for cfgdrift v0.5.0.

These tests are authored independently by QA (Edward) and deliberately do
NOT copy the engineer's assertions: expected behavior is re-derived from
``docs/system_design_v050.md``.  They cover the five new features plus the
engineer-claimed POST /api/rules PEP 563 fix and v0.4.0 regression spots:

a. plugin parser dispatch (decorator / entry-point priority / failure
   isolation / unregistered guidance / built-in formats unchanged);
b. alert retry (count / delays / count=1 / old alerts.yaml fallback /
   alert list display / cooldown unaffected);
c. HTML report export (single-file offline, four colors, masked badge,
   CLI/Web consistency, --json/--html mutual exclusion);
d. compare Web API (200/400/404 contract, masking, snippet_root) + SPA
   static wiring;
e. autostart (three-platform dry-run zero-write, interval>=60, idempotency
   D2, status 0/1/2, disable idempotency, shared worker command D9);
f. v0.4.0 regression (masking / line numbers / custom severity / compare /
   alert web) + POST /api/rules fix without regression.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cfgdrift.alert.config import AlertConfig  # noqa: E402
from cfgdrift.alert.dispatcher import AlertDispatcher  # noqa: E402
from cfgdrift.alert.models import AlertRule  # noqa: E402
from cfgdrift.alert.state import AlertStateStore  # noqa: E402
from cfgdrift.core import parser as parser_mod  # noqa: E402
from cfgdrift.core import plugins as plugins_mod  # noqa: E402
from cfgdrift.core.htmlreport import HtmlReporter  # noqa: E402
from cfgdrift.core.masker import SensitiveMasker  # noqa: E402
from cfgdrift.core.model import (  # noqa: E402
    ChangeType,
    DriftItem,
    Report,
    ScanSummary,
    Severity,
)
from cfgdrift.daemon.autostart import AutostartManager  # noqa: E402
from cfgdrift.daemon.worker import build_worker_command  # noqa: E402
from cfgdrift.storage.store import Store  # noqa: E402

CLI_OK = True
try:
    from cfgdrift.cli import main as cli_main  # noqa: E402
except Exception:  # pragma: no cover - defensive
    CLI_OK = False

WEB_OK = True
try:
    from fastapi.testclient import TestClient  # noqa: E402
except Exception:  # pragma: no cover - [web] extra may be missing
    WEB_OK = False


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _restore_plugin_registry():
    """Snapshot/restore the shared parser-plugin registry around each test.

    Other test modules register/clean up plugins on the same singleton; this
    keeps every test in this file deterministic regardless of collection
    order.
    """
    reg = parser_mod._PLUGIN_REGISTRY
    saved_plugins = dict(reg._plugins)
    saved_ext = dict(reg._ext_index)
    yield
    reg._plugins = {k: v for k, v in saved_plugins.items()}
    reg._ext_index = {k: v for k, v in saved_ext.items()}


@pytest.fixture()
def cfg_home(tmp_path):
    """Set CFGDRIFT_HOME to a temp dir and restore afterwards."""
    home = str(tmp_path / "home")
    os.makedirs(home, exist_ok=True)
    old = os.environ.get("CFGDRIFT_HOME")
    os.environ["CFGDRIFT_HOME"] = home
    yield home
    if old is None:
        os.environ.pop("CFGDRIFT_HOME", None)
    else:
        os.environ["CFGDRIFT_HOME"] = old


def _register(name, parse_fn, extensions=(), line_map=None):
    plugin = plugins_mod.ParserPlugin(
        name=name, extensions=extensions, parse=parse_fn, build_line_map=line_map
    )
    parser_mod._PLUGIN_REGISTRY.register(plugin, replace=True)
    return plugin


def _noop_sleep(seconds):
    return None


def _item(
    key_path="server.port",
    change=ChangeType.MODIFIED,
    old=8080,
    new=9090,
    file="app.json",
    severity=Severity.WARN,
    line=3,
    masked=False,
    rule_id=None,
) -> DriftItem:
    return DriftItem(
        key_path=key_path,
        change_type=change,
        severity=severity,
        file=file,
        old_value=old,
        new_value=new,
        old_type="int" if isinstance(old, int) else None,
        new_type="int" if isinstance(new, int) else None,
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


def _store_with_baselines(home, dev_data=None, prod_data=None, scan_root=None):
    """Store with 'dev' and 'prod' baselines (data differs on server.port)."""
    store = Store(os.path.join(home, "cfgdrift.db"))
    root = scan_root or home
    store.create_baseline(
        name="dev",
        description="",
        scan_root=root,
        format="json",
        data=dev_data if dev_data is not None
        else {"app.json": {"server": {"port": 8080}, "db": {"password": "devpass"}}},
        line_maps=None,
    )
    store.create_baseline(
        name="prod",
        description="",
        scan_root=root,
        format="json",
        data=prod_data if prod_data is not None
        else {"app.json": {"server": {"port": 9090}, "db": {"password": "prodpass"}}},
        line_maps=None,
    )
    return store


# ---------------------------------------------------------------------------
# a. Plugin parser dispatch
# ---------------------------------------------------------------------------

class TestPluginDispatch:
    def test_decorator_plugin_parse_semantic_tree(self):
        def parse_kv(text):
            out = {}
            for line in text.splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip()
            return out

        _register("kvdsl", parse_kv, extensions=(".kv",))
        tree = parser_mod.parse_text("a=1\nb=hello", "kvdsl")
        assert tree == {"a": "1", "b": "hello"}
        # validate_format accepts the plugin name
        assert parser_mod.validate_format("kvdsl") == "kvdsl"
        # custom_names lists it (built-ins excluded)
        assert "kvdsl" in parser_mod._PLUGIN_REGISTRY.custom_names()

    def test_plugin_line_map_and_scalar_wrap(self):
        def parse_scalar(text):
            return int(text.strip())

        _register("num", parse_scalar)
        assert parser_mod.parse_text("42", "num") == {"$": 42}

        def parse_lm(text):
            return {
                ln.split("=")[0].strip(): ln.split("=", 1)[1].strip()
                for ln in text.splitlines()
                if "=" in ln
            }

        def lm(text):
            return {
                ln.split("=")[0].strip(): i + 1
                for i, ln in enumerate(text.splitlines())
                if "=" in ln
            }

        _register("lm2", parse_lm, line_map=lm)
        tree, line_map = parser_mod.parse_text_lines("x=1\ny=2", "lm2")
        assert tree == {"x": "1", "y": "2"}
        assert line_map == {"x": 1, "y": 2}

    def test_plugin_detect_format_after_builtins(self):
        _register("pd", lambda t: {}, extensions=(".pd",))
        assert parser_mod.detect_format("a.pd") == "pd"
        # built-in extensions still win
        assert parser_mod.detect_format("a.json") == "json"
        assert parser_mod.detect_format("a.yaml") == "yaml"

    def test_plugin_parse_failure_does_not_break_builtins(self):
        def boom(text):
            raise ValueError("boom syntax")

        _register("boom", boom)
        with pytest.raises(ValueError) as exc:
            parser_mod.parse_text("x", "boom")
        assert "boom syntax" in str(exc.value)
        # Built-in parser unaffected
        assert parser_mod.parse_text("{}", "json") == {}
        assert parser_mod.parse_text("a: 1\n", "yaml") == {"a": 1}

    def test_unregistered_format_error_has_guidance(self):
        with pytest.raises(ValueError) as exc:
            parser_mod.validate_format("not_registered")
        message = str(exc.value)
        assert message.startswith("invalid format 'not_registered'")
        assert "cfgdrift.parsers" in message
        assert "register_plugin" in message
        assert "pyproject" in message

    def test_builtin_four_formats_consistent_with_v040(self):
        # Force a clean registry containing only the four built-ins.
        parser_mod._PLUGIN_REGISTRY._plugins = {}
        parser_mod._PLUGIN_REGISTRY._ext_index = {}
        parser_mod._register_builtin_plugins()
        assert parser_mod._PLUGIN_REGISTRY.custom_names() == []
        # parse all four builtins
        assert parser_mod.parse_text('{"a": 1}', "json") == {"a": 1}
        assert parser_mod.parse_text("a: 1\n", "yaml") == {"a": 1}
        assert parser_mod.parse_text('a = 1\n', "toml") == {"a": 1}
        assert parser_mod.parse_text("[sec]\nk = v\n", "ini") == {"sec": {"k": "v"}}
        # line map still built for built-ins
        tree, lm = parser_mod.parse_text_lines('{"server": {"port": 8080}}', "json")
        assert tree == {"server": {"port": 8080}}
        assert "server.port" in lm
        # error message first line unchanged from v0.4.0 (design §1.5)
        with pytest.raises(ValueError) as exc:
            parser_mod.validate_format("custom")
        first = str(exc.value).splitlines()[0]
        assert first == (
            "invalid format 'custom' (expected one of: auto, json, yaml, toml, ini)"
        )


class _FakeEPCollection(dict):
    def select(self, group=None):
        return list(self.get(group, []))


class _FakeEP:
    def __init__(self, name, value):
        self.name = name
        self._value = value

    def load(self):
        return self._value


class TestEntryPointPriority:
    def test_entry_point_overrides_decorator_in_dispatch(self, monkeypatch):
        def parse_deco(text):
            return {"source": "decorator"}

        _register("wins", parse_deco, extensions=(".w",))
        assert parser_mod.parse_text("x", "wins") == {"source": "decorator"}

        def parse_ep(text):
            return {"source": "entry_point"}

        fake = _FakeEPCollection()
        fake["cfgdrift.parsers"] = [_FakeEP("wins", parse_ep)]
        monkeypatch.setattr("importlib.metadata.entry_points", lambda: fake)
        loaded = plugins_mod.discover_entry_points()
        assert loaded >= 1
        # parser dispatch now uses the entry-point plugin (Q5)
        assert parser_mod.parse_text("x", "wins") == {"source": "entry_point"}

    def test_broken_entry_point_is_isolated(self, monkeypatch):
        class _FailingEP(_FakeEP):
            def load(self):
                raise RuntimeError("boom")

        fake = _FakeEPCollection()
        fake["cfgdrift.parsers"] = [_FailingEP("bad", None)]
        monkeypatch.setattr("importlib.metadata.entry_points", lambda: fake)
        reg = plugins_mod.PluginRegistry()
        assert reg.load_entry_points("cfgdrift.parsers") == 0
        # built-ins still fine
        assert parser_mod.parse_text("{}", "json") == {}


@pytest.mark.skipif(not CLI_OK, reason="cli unavailable")
class TestPluginCli:
    def test_scan_plugin_failure_exit2_failed_to_parse(self, tmp_path, cfg_home, capsys):
        def boom(text):
            raise ValueError("boom syntax")

        _register("boomdsl", boom, extensions=(".boom",))
        d = tmp_path / "cfgdir"
        d.mkdir()
        (d / "x.boom").write_text("k=v\n", encoding="utf-8")
        code = cli_main(["scan", str(d), "--format", "boomdsl"])
        assert code == 2
        err = capsys.readouterr().err
        assert "failed to parse" in err
        assert "boom syntax" in err

    def test_scan_unregistered_format_exit2_with_guidance(self, tmp_path, cfg_home, capsys):
        f = tmp_path / "a.json"
        f.write_text("{}", encoding="utf-8")
        code = cli_main(["scan", str(f), "--format", "not_registered"])
        assert code == 2
        err = capsys.readouterr().err
        assert "invalid format" in err
        assert "cfgdrift.parsers" in err
        assert "register_plugin" in err


# ---------------------------------------------------------------------------
# b. Alert retry
# ---------------------------------------------------------------------------

class TestRetrySemantics:
    def _failing(self, calls, message="always fails"):
        from cfgdrift.alert.channels import ChannelError

        class FailingChannel:
            def send(self, payload):
                calls["n"] += 1
                raise ChannelError(message)

        return FailingChannel()

    def test_retry_count_5_yields_5_attempts(self, tmp_path, monkeypatch):
        rule = AlertRule(
            name="cnt5", type="webhook",
            config={"url": "http://127.0.0.1:1/x", "timeout": 1},
            retry_count=5,
        )
        state = AlertStateStore(str(tmp_path / "s.json"), cooldown_seconds=600)
        calls = {"n": 0}
        monkeypatch.setattr(
            "cfgdrift.alert.dispatcher.build_channel", lambda rule: self._failing(calls)
        )
        disp = AlertDispatcher([rule], state, sleep_fn=_noop_sleep)
        results = disp.dispatch_report("prod", "/etc/app", _report([_item()]))
        assert len(results) == 1
        assert results[0].sent is False
        assert results[0].attempts == 5
        assert calls["n"] == 5

    def test_retry_delays_4_attempts_with_exact_waits(self, tmp_path, monkeypatch):
        rule = AlertRule(
            name="dly", type="webhook",
            config={"url": "http://127.0.0.1:1/x", "timeout": 1},
            retry_delays=[2, 10, 60],
        )
        state = AlertStateStore(str(tmp_path / "s.json"), cooldown_seconds=600)
        calls = {"n": 0}
        sleeps = []
        monkeypatch.setattr(
            "cfgdrift.alert.dispatcher.build_channel", lambda rule: self._failing(calls)
        )
        disp = AlertDispatcher([rule], state, sleep_fn=sleeps.append)
        results = disp.dispatch_report("prod", "/etc/app", _report([_item()]))
        assert results[0].sent is False
        assert results[0].attempts == 4  # len(delays) + 1 (D5)
        assert calls["n"] == 4
        assert sleeps == [2.0, 10.0, 60.0]

    def test_retry_count_1_single_attempt_no_sleep(self, tmp_path, monkeypatch):
        rule = AlertRule(
            name="once", type="webhook",
            config={"url": "http://127.0.0.1:1/x", "timeout": 1},
            retry_count=1,
        )
        state = AlertStateStore(str(tmp_path / "s.json"), cooldown_seconds=600)
        calls = {"n": 0}
        sleeps = []
        monkeypatch.setattr(
            "cfgdrift.alert.dispatcher.build_channel", lambda rule: self._failing(calls)
        )
        disp = AlertDispatcher([rule], state, sleep_fn=sleeps.append)
        results = disp.dispatch_report("prod", "/etc/app", _report([_item()]))
        assert results[0].attempts == 1
        assert calls["n"] == 1
        assert sleeps == []

    def test_retry_count_1_success_returns_quickly(self, tmp_path, monkeypatch):
        rule = AlertRule(
            name="ok1", type="webhook",
            config={"url": "http://127.0.0.1:1/x", "timeout": 1},
            retry_count=1,
        )
        state = AlertStateStore(str(tmp_path / "s.json"), cooldown_seconds=600)
        sleeps = []

        class GoodChannel:
            def send(self, payload):
                pass

        monkeypatch.setattr(
            "cfgdrift.alert.dispatcher.build_channel", lambda rule: GoodChannel()
        )
        disp = AlertDispatcher([rule], state, sleep_fn=sleeps.append)
        results = disp.dispatch_report("prod", "/etc/app", _report([_item()]))
        assert results[0].sent is True
        assert results[0].attempts == 1
        assert sleeps == []

    def test_old_alerts_yaml_no_retry_falls_back(self, tmp_path):
        path = os.path.join(str(tmp_path), "alerts.yaml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(
                "version: 1\n"
                "rules:\n"
                "  - name: legacy\n"
                "    type: webhook\n"
                "    severity: WARN\n"
                "    config: {url: 'http://x'}\n"
            )
        rules = AlertConfig.load(path)
        assert len(rules) == 1
        rule = rules[0]
        assert rule.retry_count is None
        assert rule.retry_delays is None
        assert rule.effective_retry(3, (1, 5, 30)) == (3, (1, 5, 30))

    def test_alerts_yaml_retry_fields_loaded(self, tmp_path):
        path = os.path.join(str(tmp_path), "alerts.yaml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(
                "version: 1\n"
                "rules:\n"
                "  - name: cnt\n"
                "    type: webhook\n"
                "    severity: WARN\n"
                "    config: {url: 'http://x'}\n"
                "    retry_count: 5\n"
                "  - name: dly\n"
                "    type: webhook\n"
                "    severity: WARN\n"
                "    config: {url: 'http://x'}\n"
                "    retry_delays: [2, 10, 60]\n"
            )
        rules = AlertConfig.load(path)
        by_name = {r.name: r for r in rules}
        assert by_name["cnt"].effective_retry() == (5, (1.0, 5.0, 30.0))
        attempts, delays = by_name["dly"].effective_retry()
        assert attempts == 4
        assert delays == (2.0, 10.0, 60.0)

    def test_cooldown_still_suppresses_after_retry_failure(self, tmp_path, monkeypatch):
        rule = AlertRule(
            name="cool", type="webhook",
            config={"url": "http://127.0.0.1:1/x", "timeout": 1},
            retry_count=3,
        )
        state = AlertStateStore(str(tmp_path / "s.json"), cooldown_seconds=600)
        calls = {"n": 0}
        monkeypatch.setattr(
            "cfgdrift.alert.dispatcher.build_channel", lambda rule: self._failing(calls)
        )
        disp = AlertDispatcher([rule], state, sleep_fn=_noop_sleep)
        first = disp.dispatch_report("prod", "/etc/app", _report([_item()]))
        assert first[0].attempted is True
        # same fingerprint immediately after -> suppressed by cooldown
        second = disp.dispatch_report("prod", "/etc/app", _report([_item()]))
        assert second == []


@pytest.mark.skipif(not CLI_OK, reason="cli unavailable")
class TestRetryCli:
    def test_alert_list_retry_display(self, tmp_path, cfg_home, capsys):
        path = os.path.join(cfg_home, "alerts.yaml")
        AlertConfig.add_rule(
            path,
            AlertRule(
                name="cnt", type="webhook",
                config={"url": "http://127.0.0.1:1/x"}, retry_count=5,
            ),
        )
        AlertConfig.add_rule(
            path,
            AlertRule(
                name="dly", type="webhook",
                config={"url": "http://127.0.0.1:1/x"}, retry_delays=[2, 10, 60],
            ),
        )
        AlertConfig.add_rule(
            path,
            AlertRule(
                name="plain", type="webhook",
                config={"url": "http://127.0.0.1:1/x"},
            ),
        )
        code = cli_main(["alert", "list"])
        assert code == 0
        out = capsys.readouterr().out
        assert "retry=5/1.0,5.0,30.0" in out
        assert "retry=4/2.0,10.0,60.0" in out
        assert "retry=default" in out


# ---------------------------------------------------------------------------
# c. HTML report export
# ---------------------------------------------------------------------------

def _html_data(masked_item=False):
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
            "new_value": "******" if masked_item else "hunter2",
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


class TestHtmlReport:
    def test_single_file_offline_four_colors(self):
        html = HtmlReporter.render_html(_html_data(), title="cfgdrift report #7")
        assert html.startswith("<!DOCTYPE html>")
        assert "cfgdrift report #7" in html
        assert "漂移总数" in html
        # four severity colors (Q4 / design §6.3)
        for color in ("#ef4444", "#f59e0b", "#22c55e", "#64748b"):
            assert color in html
        # summary cards + distribution + item table columns
        assert "严重度分布" in html
        assert "变更列表" in html
        assert "server.port" in html
        assert "app.json:3" in html  # file:line rendering (D10)
        assert "8080" in html and "9090" in html
        # zero external references (single-file offline)
        assert "http://" not in html
        assert "https://" not in html
        assert 'src="' not in html
        assert 'href="http' not in html

    def test_masked_item_shows_badge_and_hides_secret(self):
        html = HtmlReporter.render_html(_html_data(masked_item=True))
        assert "已脱敏" in html
        assert "hunter2" not in html

    def test_missing_line_renders_file_only(self):
        data = _html_data()
        for item in data["items"]:
            item["line"] = None
        html = HtmlReporter.render_html(data)
        # no ":3" suffix when line is unavailable (D10)
        assert "app.json:3" not in html
        assert "app.json" in html

    def test_empty_items(self):
        data = _html_data()
        data["items"] = []
        html = HtmlReporter.render_html(data)
        assert "无漂移项" in html


@pytest.mark.skipif(not (CLI_OK and WEB_OK), reason="cli/web unavailable")
class TestHtmlCliWebConsistency:
    def _seed_scan(self, home):
        """Store + baseline + one drifting scan (raw values in DB)."""
        store = Store(os.path.join(home, "cfgdrift.db"))
        store.create_baseline(
            name="prod",
            description="",
            scan_root=home,
            format="json",
            data={"app.json": {"server": {"port": 8080}}},
            line_maps=None,
        )
        items = [
            _item(),  # server.port 8080 -> 9090 (not sensitive)
            DriftItem(
                key_path="db.password",
                change_type=ChangeType.MODIFIED,
                severity=Severity.CRITICAL,
                file="app.json",
                old_value="oldpass",
                new_value="newpass",
                old_type="str",
                new_type="str",
                line=8,
                masked=False,
            ),
        ]
        payload = {"code": 0, "data": _report(items).to_dict(), "message": "ok"}
        scan_id = store.add_scan(None, "manual", payload)
        store.close()
        return scan_id

    def test_cli_html_and_web_html_identical(self, tmp_path, cfg_home, capsys):
        scan_id = self._seed_scan(cfg_home)
        out_path = str(tmp_path / "cli.html")
        code = cli_main(["report", "--scan-id", str(scan_id), "--html", out_path])
        assert code == 0
        cli_html = open(out_path, encoding="utf-8").read()

        from cfgdrift.web.app import create_app

        store = Store(os.path.join(cfg_home, "cfgdrift.db"))
        try:
            app = create_app(store, home=cfg_home)
            resp = TestClient(app).get("/api/reports/%d/html" % scan_id)
        finally:
            store.close()
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        web_html = resp.text

        # Same renderer + same masked payload (D6) -> byte-identical output.
        assert web_html == cli_html
        # Key fields present in both; the raw secret never leaks.
        for key in ("<!DOCTYPE html>", "server.port", "db.password", "已脱敏"):
            assert key in cli_html and key in web_html
        assert "oldpass" not in cli_html and "oldpass" not in web_html
        assert "newpass" not in cli_html and "newpass" not in web_html

    def test_report_json_html_mutually_exclusive(self, tmp_path, cfg_home, capsys):
        scan_id = self._seed_scan(cfg_home)
        code = cli_main(
            [
                "report", "--scan-id", str(scan_id),
                "--json", str(tmp_path / "a.json"),
                "--html", str(tmp_path / "a.html"),
            ]
        )
        assert code == 2
        err = capsys.readouterr().err
        assert "mutually exclusive" in err


# ---------------------------------------------------------------------------
# d. compare Web API
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not WEB_OK, reason="fastapi/httpx unavailable")
class TestCompareWebApi:
    @pytest.fixture()
    def client_and_store(self, tmp_path):
        from cfgdrift.web.app import create_app

        home = str(tmp_path / "home")
        os.makedirs(home, exist_ok=True)
        store = _store_with_baselines(home)
        app = create_app(store, home=home)
        client = TestClient(app)
        yield client, store, home
        store.close()

    def test_compare_ok_masked_and_snippet_root(self, client_and_store):
        client, _, _ = client_and_store
        resp = client.post("/api/compare", json={"env1": "dev", "env2": "prod"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 0
        data = body["data"]
        assert data["baseline_a"] == "dev"
        assert data["baseline_b"] == "prod"
        assert data["summary"]["total"] >= 1
        # masking at the display exit: password values replaced
        pw_items = [it for it in data["items"] if "password" in it["key_path"]]
        if pw_items:
            assert pw_items[0]["masked"] is True
            assert pw_items[0]["old_value"] == "******"
            assert pw_items[0]["new_value"] == "******"
            assert "devpass" not in json.dumps(data)
            assert "prodpass" not in json.dumps(data)
        # snippet_root injected for every item
        for it in data["items"]:
            assert "snippet_root" in it

    def test_snippet_root_source_side(self, client_and_store, tmp_path):
        """removed -> env1 root; added/modified -> env2 root."""
        from cfgdrift.web.app import create_app

        home = str(tmp_path / "home2")
        os.makedirs(home, exist_ok=True)
        root1 = str(tmp_path / "root1")
        root2 = str(tmp_path / "root2")
        os.makedirs(root1, exist_ok=True)
        os.makedirs(root2, exist_ok=True)
        store = _store_with_baselines(
            home,
            dev_data={"a.json": {"x": 1, "gone": 1}},
            prod_data={"a.json": {"x": 2, "newk": 3}},
            scan_root=root1,  # both use root1 for baseline scan_root; we patch below
        )
        # force distinct scan roots by rebuilding with explicit roots
        store.close()
        store = Store(os.path.join(home, "cfgdrift.db"))
        # recreate with per-baseline roots
        store.create_baseline(
            name="dev", description="", scan_root=root1, format="json",
            data={"a.json": {"x": 1, "gone": 1}}, line_maps=None,
        )
        store.create_baseline(
            name="prod", description="", scan_root=root2, format="json",
            data={"a.json": {"x": 2, "newk": 3}}, line_maps=None,
        )
        app = create_app(store, home=home)
        client = TestClient(app)
        try:
            resp = client.post("/api/compare", json={"env1": "dev", "env2": "prod"})
            assert resp.status_code == 200
            data = resp.json()["data"]
            by_type = {}
            for it in data["items"]:
                by_type.setdefault(it["change_type"], []).append(it)
            # removed items come from env1's baseline scan root
            for it in by_type.get("removed", []):
                assert it["snippet_root"] == root1
            # added items come from env2's baseline scan root
            for it in by_type.get("added", []):
                assert it["snippet_root"] == root2
            # modified items also use env2's root
            for it in by_type.get("modified", []):
                assert it["snippet_root"] == root2
        finally:
            store.close()

    def test_compare_missing_env_400(self, client_and_store):
        client, _, _ = client_and_store
        resp = client.post("/api/compare", json={"env1": "dev"})
        assert resp.status_code == 400
        assert "env1 and env2 are required" in resp.json()["message"]
        resp = client.post("/api/compare", json={})
        assert resp.status_code == 400

    def test_compare_same_env_400(self, client_and_store):
        client, _, _ = client_and_store
        resp = client.post("/api/compare", json={"env1": "dev", "env2": "dev"})
        assert resp.status_code == 400
        assert "must be different" in resp.json()["message"]

    def test_compare_uncollected_baseline_404(self, client_and_store):
        client, _, _ = client_and_store
        resp = client.post(
            "/api/compare", json={"env1": "dev", "env2": "ghost"}
        )
        assert resp.status_code == 404
        message = resp.json()["message"]
        assert "ghost" in message
        assert "未采集基线" in message

    def test_spa_static_wiring(self):
        static_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "src", "cfgdrift", "web", "static",
        )
        index = open(os.path.join(static_dir, "index.html"), encoding="utf-8").read()
        appjs = open(os.path.join(static_dir, "app.js"), encoding="utf-8").read()
        assert 'data-view="compare"' in index
        assert 'id="view-compare"' in index
        assert "renderCompare" in appjs
        assert "api/compare" in appjs
        assert "snippet" in appjs  # line-number click-through support present


# ---------------------------------------------------------------------------
# e. autostart
# ---------------------------------------------------------------------------

def _autostart_opts(home, store_path, **overrides):
    opts = {
        "targets": [os.path.abspath(".")],
        "baseline": "prod",
        "fmt": "auto",
        "interval": 300,
        "store": store_path,
        "log_file": os.path.join(home, "logs", "daemon.log"),
        "log_level": "INFO",
        "scope": "user",
    }
    opts.update(overrides)
    return opts


def _autostart_cfg(home, store_path, **overrides):
    cfg = {
        "targets": [os.path.abspath(".")],
        "baseline": "prod",
        "fmt": "auto",
        "interval": 300,
        "store": os.path.abspath(store_path),
        "log_file": os.path.abspath(os.path.join(home, "logs", "daemon.log")),
        "log_level": "INFO",
    }
    cfg.update(overrides)
    return cfg


@pytest.fixture()
def store_with_baseline(tmp_path):
    store = Store(str(tmp_path / "cfgdrift.db"))
    store.create_baseline(
        name="prod",
        description="test",
        scan_root=str(tmp_path),
        format="json",
        data={"app.json": {"server": {"port": 8080}}},
        line_maps=None,
    )
    yield store
    store.close()


class TestAutostart:
    def test_dry_run_zero_write_all_platforms(self, tmp_path, store_with_baseline, monkeypatch, capsys):
        home = str(tmp_path / "home")
        os.makedirs(home, exist_ok=True)
        m = AutostartManager(home, store_with_baseline.db_path)
        for platform, marker in (
            ("linux", "systemd"),
            ("darwin", "launchd"),
            ("win32", "schtasks"),
        ):
            monkeypatch.setattr(sys, "platform", platform)
            capsys.readouterr()  # clear
            code = m.enable(_autostart_opts(home, store_with_baseline.db_path), dry_run=True)
            assert code == 0, platform
            out = capsys.readouterr().out
            assert marker in out
            assert "dry run" in out
            assert "autostart.json" in out
            # zero disk writes: no autostart.json, no artifact file
            assert not os.path.exists(AutostartManager.autostart_config_path(home))
        # restore real platform for other tests in this class
        monkeypatch.undo()

    def test_systemd_renderer_uses_shared_worker_command(self, tmp_path, store_with_baseline):
        home = str(tmp_path / "home")
        os.makedirs(home, exist_ok=True)
        m = AutostartManager(home, store_with_baseline.db_path)
        cfg = _autostart_opts(home, store_with_baseline.db_path)
        cfg["home"] = home
        text = m._render_systemd(cfg)
        assert "ExecStart=" in text
        assert "-m cfgdrift.daemon.worker" in text
        assert "--baseline prod" in text
        assert "--interval 300" in text
        assert "--path " + os.path.abspath(".") in text
        # D9: autostart unit carries alerts-config/alert-state
        assert "--alerts-config" in text
        assert "--alert-state" in text
        # user scope default -> default.target
        assert "WantedBy=default.target" in text

    def test_interval_lt_60_exit2(self, tmp_path, store_with_baseline, capsys):
        home = str(tmp_path / "home")
        os.makedirs(home, exist_ok=True)
        m = AutostartManager(home, store_with_baseline.db_path)
        code = m.enable(_autostart_opts(home, store_with_baseline.db_path, interval=30))
        assert code == 2
        assert ">= 60" in capsys.readouterr().err

    def test_same_config_noop_exit0(self, tmp_path, store_with_baseline, capsys):
        home = str(tmp_path / "home")
        os.makedirs(home, exist_ok=True)
        m = AutostartManager(home, store_with_baseline.db_path)
        m._write_config(
            {
                "version": 1, "enabled": True, "scope": "user",
                "created_at": "2026-01-01T00:00:00+00:00",
                "config": _autostart_cfg(home, store_with_baseline.db_path),
                "unit": {"type": "schtasks", "path": None, "name": "cfgdrift-daemon"},
            }
        )
        code = m.enable(_autostart_opts(home, store_with_baseline.db_path), dry_run=True)
        assert code == 0
        assert "no change" in capsys.readouterr().out

    def test_different_config_requires_force(self, tmp_path, store_with_baseline, capsys):
        home = str(tmp_path / "home")
        os.makedirs(home, exist_ok=True)
        m = AutostartManager(home, store_with_baseline.db_path)
        m._write_config(
            {
                "version": 1, "enabled": True, "scope": "user",
                "created_at": "2026-01-01T00:00:00+00:00",
                "config": _autostart_cfg(home, store_with_baseline.db_path, interval=600),
                "unit": {"type": "schtasks", "path": None, "name": "cfgdrift-daemon"},
            }
        )
        code = m.enable(_autostart_opts(home, store_with_baseline.db_path), dry_run=True)
        assert code == 2
        assert "different parameters" in capsys.readouterr().err
        # with --force the dry-run proceeds (still exit 0)
        code = m.enable(
            _autostart_opts(home, store_with_baseline.db_path, force=True),
            dry_run=True,
        )
        assert code == 0

    def test_status_no_config_exit1(self, tmp_path, capsys):
        home = str(tmp_path / "home")
        os.makedirs(home, exist_ok=True)
        m = AutostartManager(home)
        assert m.status() == 1
        assert "disabled" in capsys.readouterr().out

    def test_status_enabled_exit0_with_fake_json(self, tmp_path, monkeypatch, capsys):
        home = str(tmp_path / "home")
        os.makedirs(home, exist_ok=True)
        m = AutostartManager(home)
        m._write_config(
            {
                "version": 1, "enabled": True, "scope": "user",
                "created_at": "2026-01-01T00:00:00+00:00",
                "config": _autostart_cfg(home, str(tmp_path / "cfgdrift.db")),
                "unit": {"type": "schtasks", "path": None, "name": "cfgdrift-daemon"},
            }
        )
        monkeypatch.setattr(m, "_artifact_present", lambda doc: True)
        assert m.status() == 0
        out = capsys.readouterr().out
        assert "autostart: enabled" in out
        assert "artifact_present: yes" in out

    def test_status_enabled_artifact_missing_exit2(self, tmp_path, monkeypatch):
        home = str(tmp_path / "home")
        os.makedirs(home, exist_ok=True)
        m = AutostartManager(home)
        m._write_config(
            {
                "version": 1, "enabled": True, "scope": "user",
                "created_at": "2026-01-01T00:00:00+00:00",
                "config": _autostart_cfg(home, str(tmp_path / "cfgdrift.db")),
                "unit": {"type": "schtasks", "path": None, "name": "cfgdrift-daemon"},
            }
        )
        monkeypatch.setattr(m, "_artifact_present", lambda doc: False)
        assert m.status() == 2

    def test_disable_idempotent_and_clears_json(self, tmp_path, monkeypatch):
        home = str(tmp_path / "home")
        os.makedirs(home, exist_ok=True)
        m = AutostartManager(home)
        m._write_config(
            {
                "version": 1, "enabled": True, "scope": "user",
                "created_at": "2026-01-01T00:00:00+00:00",
                "config": _autostart_cfg(home, str(tmp_path / "cfgdrift.db")),
                "unit": {"type": "schtasks", "path": None, "name": "cfgdrift-daemon"},
            }
        )
        monkeypatch.setattr(m, "_remove_artifact", lambda *a, **k: None)
        assert m.disable() == 0
        assert not os.path.exists(AutostartManager.autostart_config_path(home))
        # idempotent: second disable still 0
        assert m.disable() == 0

    def test_disable_dry_run_no_disk_change(self, tmp_path, capsys):
        home = str(tmp_path / "home")
        os.makedirs(home, exist_ok=True)
        m = AutostartManager(home)
        assert m.disable(dry_run=True) == 0
        out = capsys.readouterr().out
        assert "schtasks /Delete" in out or "rm" in out
        assert not os.path.exists(AutostartManager.autostart_config_path(home))

    def test_worker_command_single_source(self, tmp_path):
        home = str(tmp_path / "home")
        os.makedirs(home, exist_ok=True)
        store_path = str(tmp_path / "cfgdrift.db")
        cmd = build_worker_command(
            home,
            {
                "store": store_path,
                "baseline": "prod",
                "fmt": "yaml",
                "interval": 120,
                "targets": ["/a", "/b"],
                "log_file": os.path.join(home, "logs", "daemon.log"),
                "log_level": "INFO",
                "alerts_config": os.path.join(home, "alerts.yaml"),
                "alert_state": os.path.join(home, "alert_state.json"),
            },
        )
        assert cmd[0] == sys.executable
        assert cmd[1:3] == ["-m", "cfgdrift.daemon.worker"]
        assert "--baseline" in cmd and cmd[cmd.index("--baseline") + 1] == "prod"
        assert cmd[cmd.index("--format") + 1] == "yaml"
        assert cmd[cmd.index("--interval") + 1] == "120"
        assert cmd.count("--path") == 2
        assert "--alerts-config" in cmd and "--alert-state" in cmd
        # without pid files they are omitted (autostart units)
        assert "--pid-file" not in cmd


@pytest.mark.skipif(not CLI_OK, reason="cli unavailable")
class TestAutostartCli:
    def test_enable_autostart_dry_run_exit0(self, tmp_path, cfg_home, capsys):
        store = Store(os.path.join(cfg_home, "cfgdrift.db"))
        store.create_baseline(
            name="prod", description="", scan_root=cfg_home, format="json",
            data={"app.json": {"server": {"port": 8080}}}, line_maps=None,
        )
        store.close()
        target = cfg_home  # exists
        code = cli_main(
            [
                "daemon", "enable-autostart",
                "--target", target, "--baseline", "prod",
                "--interval", "300", "--dry-run",
            ]
        )
        assert code == 0
        out = capsys.readouterr().out
        assert "dry run" in out
        assert "autostart.json" in out
        assert not os.path.exists(os.path.join(cfg_home, "autostart.json"))

    def test_enable_autostart_interval_lt60_exit2(self, tmp_path, cfg_home, capsys):
        store = Store(os.path.join(cfg_home, "cfgdrift.db"))
        store.create_baseline(
            name="prod", description="", scan_root=cfg_home, format="json",
            data={"app.json": {"server": {"port": 8080}}}, line_maps=None,
        )
        store.close()
        code = cli_main(
            [
                "daemon", "enable-autostart",
                "--target", cfg_home, "--baseline", "prod",
                "--interval", "30", "--dry-run",
            ]
        )
        assert code == 2
        assert ">= 60" in capsys.readouterr().err

    def test_autostart_status_no_config_exit1(self, tmp_path, cfg_home, capsys):
        code = cli_main(["daemon", "autostart-status"])
        assert code == 1
        assert "disabled" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# f. v0.4.0 regression + POST /api/rules fix
# ---------------------------------------------------------------------------

class TestV040Regression:
    def test_masking_still_works(self):
        masker = SensitiveMasker()
        item = {
            "key_path": "db.password",
            "old_value": "plain",
            "new_value": "plain",
            "masked": False,
        }
        masker.mask_item(item)
        assert item["masked"] is True
        assert item["old_value"] == "******"
        # non-sensitive keys untouched
        normal = {"key_path": "server.port", "old_value": 1, "new_value": 2}
        masker.mask_item(normal)
        assert normal["new_value"] == 2

    def test_line_numbers_still_rendered(self):
        tree, line_map = parser_mod.parse_text_lines(
            '{"server": {"port": 8080, "host": "x"}}', "json"
        )
        assert line_map.get("server.port") == 1

    def test_custom_severity_still_applies(self, tmp_path, cfg_home):
        from cfgdrift.core.differ import SemanticDiffer
        from cfgdrift.rules.severity import SeverityConfig, default_path

        path = default_path(cfg_home)
        SeverityConfig.add_rule(
            path,
            __import__("cfgdrift.rules.severity", fromlist=["make_rule"]).make_rule(
                name="crit", severity="CRITICAL",
                key_pattern=r".*server\.port", enabled=True,
            ),
        )
        rules = SeverityConfig.load(path)
        differ = SemanticDiffer()
        items, summary = differ.diff_snapshot(
            {"app.json": {"server": {"port": 8080}}},
            {"app.json": {"server": {"port": 9090}}},
            rules=[],
            severity_rules=rules,
        )
        assert items[0].severity.value == "CRITICAL"

    def test_compare_engine_still_works(self, tmp_path):
        home = str(tmp_path / "home")
        os.makedirs(home, exist_ok=True)
        store = _store_with_baselines(home)
        try:
            from cfgdrift.core.compare import CompareEngine

            engine = CompareEngine(store)
            reports = engine.compare(["dev", "prod"], env_map={}, masker=SensitiveMasker())
            assert reports[0].summary.total >= 1
            # masked output hides the password values
            blob = json.dumps(reports[0].to_dict())
            assert "devpass" not in blob and "prodpass" not in blob
        finally:
            store.close()


@pytest.mark.skipif(not (CLI_OK and WEB_OK), reason="cli/web unavailable")
class TestApiRulesFix:
    """Engineer-claimed POST /api/rules 422 fix (PEP 563 annotation strings)."""

    def _client(self, tmp_path):
        from cfgdrift.web.app import create_app

        home = str(tmp_path / "home")
        os.makedirs(home, exist_ok=True)
        store = Store(os.path.join(home, "cfgdrift.db"))
        app = create_app(store, home=home)
        return TestClient(app), store

    def test_post_api_rules_valid_200_not_422(self, tmp_path):
        client, store = self._client(tmp_path)
        try:
            resp = client.post(
                "/api/rules",
                json={"name": "r1", "key_pattern": "server.port"},
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["code"] == 0
            assert "id" in resp.json()["data"]
        finally:
            store.close()

    def test_post_api_rules_invalid_400(self, tmp_path):
        client, store = self._client(tmp_path)
        try:
            resp = client.post("/api/rules", json={"name": "r2"})
            assert resp.status_code == 400
            assert "required" in resp.json()["message"]
        finally:
            store.close()

    def test_get_api_rules_still_works(self, tmp_path):
        client, store = self._client(tmp_path)
        try:
            resp = client.get("/api/rules")
            assert resp.status_code == 200
            assert resp.json()["code"] == 0
            assert "rules" in resp.json()["data"]
        finally:
            store.close()

    def test_web_alert_api_regression(self, tmp_path, cfg_home):
        from cfgdrift.web.app import create_app

        # write an alerts.yaml so /api/alerts has data
        path = os.path.join(cfg_home, "alerts.yaml")
        AlertConfig.add_rule(
            path,
            AlertRule(
                name="web", type="webhook",
                config={"url": "http://127.0.0.1:1/x", "timeout": 1},
            ),
        )
        store = Store(os.path.join(cfg_home, "cfgdrift.db"))
        try:
            client = TestClient(create_app(store, home=cfg_home))
            r = client.get("/api/alerts")
            assert r.status_code == 200
            assert r.json()["data"]["alerts"][0]["name"] == "web"
            r2 = client.get("/api/alert-events")
            assert r2.status_code == 200
            r3 = client.get("/api/health")
            assert r3.status_code == 200
        finally:
            store.close()
