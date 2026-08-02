"""Unit tests for cfgdrift v0.3.0 alert module.

Covers models (payload / fingerprint / substitution), config CRUD + validation,
alert_state cooldown/prune, the three channels, retry-with-backoff, and the
dispatcher (filters / dedupe / success / failure recording).
"""

from __future__ import annotations

import json
import os
import smtplib
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

from cfgdrift.alert.channels import (  # noqa: E402
    ChannelError,
    EmailChannel,
    ScriptChannel,
    WebhookChannel,
    payload_env,
    retry_with_backoff,
)
from cfgdrift.alert.config import AlertConfig  # noqa: E402
from cfgdrift.alert.dispatcher import AlertDispatcher  # noqa: E402
from cfgdrift.alert.models import (  # noqa: E402
    AlertRule,
    build_drift_payload,
    build_test_payload,
    drift_fingerprint,
    expand_env_vars,
    substitute,
)
from cfgdrift.alert.state import AlertStateStore  # noqa: E402
from cfgdrift.core.model import (  # noqa: E402
    ChangeType,
    DriftItem,
    Report,
    ScanSummary,
    Severity,
)
from cfgdrift.storage.store import utcnow_iso  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_item(key="server.port", change="modified", sev="WARN", file="conf/app.json"):
    return DriftItem(
        key_path=key,
        change_type=ChangeType(change),
        severity=Severity(sev),
        file=file,
        old_value="8080",
        new_value="9090",
        old_type="str",
        new_type="str",
    )


def make_report(items=None, max_sev="WARN"):
    summary = ScanSummary()
    summary.max_severity = Severity(max_sev)
    return Report(
        scan_id=1,
        baseline=None,
        created_at=utcnow_iso(),
        mode="daemon",
        summary=summary,
        items=items or [],
    )


def noop_sleep(_seconds):
    """Injected sleep so retry tests never actually wait."""


class _Collector:
    def __init__(self):
        self.payloads = []
        self.headers = []
        self.lock = threading.Lock()
        self.status = 200

    def add(self, payload, headers):
        with self.lock:
            self.payloads.append(payload)
            self.headers.append(headers)


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            payload = json.loads(body.decode("utf-8"))
        except ValueError:
            payload = {"raw": body.decode("utf-8", "replace")}
        self.server.collector.add(payload, dict(self.headers))
        self.send_response(self.server.collector.status)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


@pytest.fixture()
def webhook_server():
    collector = _Collector()
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    server.collector = collector
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, collector
    server.shutdown()
    server.server_close()


@pytest.fixture()
def cfg_home(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return str(home)


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------

class TestModels:
    def test_rule_defaults_and_to_dict(self):
        rule = AlertRule(name="r", type="webhook", config={"url": "http://x"})
        assert rule.severity == Severity.WARN
        assert rule.enabled is True
        assert rule.baseline is None
        d = rule.to_dict()
        assert d["name"] == "r"
        assert d["type"] == "webhook"
        assert d["severity"] == "WARN"

    def test_rule_from_dict_validates_type_and_severity(self):
        with pytest.raises(ValueError):
            AlertRule.from_dict({"name": "r", "type": "slack"})
        with pytest.raises(ValueError):
            AlertRule.from_dict(
                {"name": "r", "type": "webhook", "severity": "URGENT"}
            )
        with pytest.raises(ValueError):
            AlertRule.from_dict({"type": "webhook"})

    def test_build_drift_payload_shape(self):
        items = [make_item(key="server.port", change="modified", sev="WARN")]
        report = make_report(items, max_sev="WARN")
        payload = build_drift_payload(report, "prod", "/etc/app", "0.3.0")
        assert payload["event"] == "cfgdrift.drift"
        assert payload["version"] == "0.3.0"
        assert payload["severity"] == "WARN"
        assert payload["baseline"] == "prod"
        assert payload["target"] == "/etc/app"
        assert payload["drift_count"] == 1
        assert payload["summary"] == "1 WARN drift(s) in baseline prod"
        assert payload["drift_items"][0]["key"] == "server.port"
        assert payload["drift_items"][0]["file"] == "conf/app.json"
        assert payload["drift_items"][0]["change_type"] == "modified"
        assert "old_value" not in payload
        assert "password" not in json.dumps(payload)

    def test_drift_fingerprint_stable_and_value_independent(self):
        items_a = [make_item(key="server.port", change="modified")]
        items_b = [
            DriftItem(
                key_path="server.port",
                change_type=ChangeType.MODIFIED,
                severity=Severity.WARN,
                file="conf/app.json",
                old_value="12345",
                new_value="99999",
            )
        ]
        f1 = drift_fingerprint("prod", "/etc/app", items_a)
        f2 = drift_fingerprint("prod", "/etc/app", items_b)
        assert f1 == f2  # value jitter must not change the fingerprint
        f3 = drift_fingerprint("prod2", "/etc/app", items_a)
        assert f1 != f3  # different baseline -> different fingerprint
        f4 = drift_fingerprint(
            "prod", "/etc/app", [make_item(key="server.host", change="modified")]
        )
        assert f1 != f4  # different key -> different fingerprint
        # dict items (as produced by report.to_dict) give the same signature
        fd = drift_fingerprint("prod", "/etc/app", [items_a[0].to_dict()])
        assert f1 == fd

    def test_expand_env_vars_and_substitute(self, monkeypatch):
        monkeypatch.setenv("CFGDRIFT_TOKEN", "secret-token")
        assert expand_env_vars("a={env:CFGDRIFT_TOKEN}") == "a=secret-token"
        assert expand_env_vars("x={env:NO_SUCH_VAR_XYZ}") == "x="
        out = substitute(
            "{severity} in {baseline} token={env:CFGDRIFT_TOKEN}",
            {"severity": "CRITICAL", "baseline": "prod"},
        )
        assert out == "CRITICAL in prod token=secret-token"

    def test_build_test_payload(self):
        payload = build_test_payload("0.3.0")
        assert payload["event"] == "cfgdrift.test"
        assert payload["drift_count"] == 1
        assert payload["drift_items"][0]["change_type"] == "modified"


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

class TestConfig:
    def test_roundtrip(self, cfg_home):
        path = AlertConfig.default_path(cfg_home)
        rule = AlertRule(
            name="hook",
            type="webhook",
            severity=Severity.CRITICAL,
            baseline="prod",
            config={"url": "http://x", "timeout": 5},
        )
        idx = AlertConfig.add_rule(path, rule)
        assert idx == 0
        rules = AlertConfig.list_rules(path)
        assert len(rules) == 1
        assert rules[0].name == "hook"
        assert rules[0].severity == Severity.CRITICAL
        assert rules[0].config["url"] == "http://x"
        assert os.path.exists(path)

    def test_duplicate_name_raises(self, cfg_home):
        path = AlertConfig.default_path(cfg_home)
        AlertConfig.add_rule(
            path, AlertRule(name="r", type="webhook", config={"url": "http://x"})
        )
        with pytest.raises(ValueError):
            AlertConfig.add_rule(
                path, AlertRule(name="r", type="script", config={"command": "x"})
            )

    def test_remove_rule(self, cfg_home):
        path = AlertConfig.default_path(cfg_home)
        AlertConfig.add_rule(
            path, AlertRule(name="r", type="webhook", config={"url": "http://x"})
        )
        AlertConfig.remove_rule(path, "r")
        assert AlertConfig.list_rules(path) == []
        with pytest.raises(ValueError):
            AlertConfig.remove_rule(path, "missing")

    def test_invalid_type_raises_on_load(self, cfg_home):
        path = AlertConfig.default_path(cfg_home)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("version: 1\nrules:\n  - name: r\n    type: slack\n")
        with pytest.raises(ValueError):
            AlertConfig.load(path)

    def test_missing_required_raises_on_load(self, cfg_home):
        path = AlertConfig.default_path(cfg_home)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("version: 1\nrules:\n  - name: r\n    type: webhook\n")
        with pytest.raises(ValueError):
            AlertConfig.load(path)
        # email requires smtp_to non-empty
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(
                "version: 1\nrules:\n  - name: e\n    type: email\n"
                "    config:\n      smtp_host: h\n      smtp_port: 25\n"
                "      smtp_from: a@b.c\n      smtp_to: []\n"
            )
        with pytest.raises(ValueError):
            AlertConfig.load(path)

    def test_unsupported_version_raises(self, cfg_home):
        path = AlertConfig.default_path(cfg_home)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("version: 99\nrules: []\n")
        with pytest.raises(ValueError):
            AlertConfig.load(path)

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission check")
    def test_save_sets_0600(self, cfg_home):
        path = AlertConfig.default_path(cfg_home)
        AlertConfig.save(path, [])
        assert (os.stat(path).st_mode & 0o777) == 0o600


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------

class TestState:
    def test_record_success_suppresses_within_cooldown(self, cfg_home):
        state = AlertStateStore(os.path.join(cfg_home, "alert_state.json"), cooldown_seconds=600)
        key = state.key_for("hook", "abc123")
        state.record_success(key, {"rule": "hook", "fingerprint": "abc123"})
        assert state.is_suppressed(key) is True
        entry = state.get(key)
        assert entry["last_status"] == "sent"
        assert entry["attempts"] == 1
        assert "suppress_until" in entry
        # a different key is not suppressed
        other = state.key_for("hook", "other")
        assert state.is_suppressed(other) is False

    def test_record_failure_sets_status_and_cooldown(self, cfg_home):
        state = AlertStateStore(os.path.join(cfg_home, "alert_state.json"), cooldown_seconds=600)
        key = state.key_for("hook", "abc")
        state.record_failure(key, {"rule": "hook", "fingerprint": "abc", "attempts": 3})
        entry = state.get(key)
        assert entry["last_status"] == "failed"
        assert entry["attempts"] == 3
        assert state.is_suppressed(key) is True

    def test_state_persists_across_reload(self, cfg_home):
        path = os.path.join(cfg_home, "alert_state.json")
        state = AlertStateStore(path)
        key = state.key_for("r", "f")
        state.record_success(key, {"rule": "r", "fingerprint": "f"})
        state2 = AlertStateStore(path)
        assert state2.is_suppressed(key) is True
        assert state2.get(key)["last_status"] == "sent"

    def test_prune_removes_old_entries(self, cfg_home):
        state = AlertStateStore(os.path.join(cfg_home, "alert_state.json"), cooldown_seconds=600)
        key = state.key_for("r", "f")
        state.record_success(key, {"rule": "r", "fingerprint": "f"})
        # Force an old timestamp and reload -> pruned on load.
        entry = state.get(key)
        entry["last_attempt_at"] = "2000-01-01T00:00:00+00:00"
        state._entries[key] = entry
        state.save()
        state2 = AlertStateStore(os.path.join(cfg_home, "alert_state.json"))
        assert state2.get(key) is None

    def test_corrupt_file_rebuilds(self, cfg_home):
        path = os.path.join(cfg_home, "alert_state.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        state = AlertStateStore(path)
        assert state.entries() == {}
        # and it still works afterwards
        key = state.key_for("r", "f")
        state.record_success(key, {"rule": "r", "fingerprint": "f"})
        assert state.is_suppressed(key) is True


# ---------------------------------------------------------------------------
# channels
# ---------------------------------------------------------------------------

class TestChannels:
    def test_webhook_sends_payload_and_headers(self, webhook_server):
        server, collector = webhook_server
        url = "http://127.0.0.1:%d/hook" % server.server_port
        channel = WebhookChannel(
            {"url": url, "headers": {"X-Cfgdrift-Token": "abc"}, "timeout": 5}
        )
        payload = build_test_payload("0.3.0")
        channel.send(payload)
        assert len(collector.payloads) == 1
        received = collector.payloads[0]
        assert received["event"] == "cfgdrift.test"
        assert received["drift_count"] == 1
        assert collector.headers[0].get("X-Cfgdrift-Token") == "abc"
        assert collector.headers[0].get("Content-Type") == "application/json"

    def test_webhook_header_env_substitution(self, webhook_server, monkeypatch):
        server, collector = webhook_server
        monkeypatch.setenv("CFGDRIFT_WEBHOOK_TOKEN", "tok123")
        channel = WebhookChannel(
            {
                "url": "http://127.0.0.1:%d/hook" % server.server_port,
                "headers": {"X-Token": "{env:CFGDRIFT_WEBHOOK_TOKEN}"},
                "timeout": 5,
            }
        )
        channel.send(build_test_payload())
        assert collector.headers[0]["X-Token"] == "tok123"

    def test_webhook_http_error_raises(self, webhook_server):
        server, collector = webhook_server
        collector.status = 500
        channel = WebhookChannel(
            {"url": "http://127.0.0.1:%d/hook" % server.server_port, "timeout": 5}
        )
        with pytest.raises(ChannelError):
            channel.send(build_test_payload())

    def test_webhook_connection_error_raises(self):
        channel = WebhookChannel(
            {"url": "http://127.0.0.1:1/unreachable", "timeout": 1}
        )
        with pytest.raises(ChannelError):
            channel.send(build_test_payload())

    def test_email_channel_builds_message_and_logs_in(self, monkeypatch):
        sent = []
        login_calls = []

        class FakeSMTP:
            def __init__(self, host, port, timeout=None):
                self.host = host
                self.port = port
                self.tls = False

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def starttls(self):
                self.tls = True

            def login(self, user, password):
                login_calls.append((user, password))

            def send_message(self, msg):
                sent.append(msg)

        monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
        monkeypatch.setenv("CFGDRIFT_SMTP_PASSWORD", "s3cret")
        channel = EmailChannel(
            {
                "smtp_host": "smtp.example.com",
                "smtp_port": 587,
                "smtp_user": "alerts@example.com",
                "smtp_from": "alerts@example.com",
                "smtp_to": ["ops@example.com"],
                "smtp_password_env": "CFGDRIFT_SMTP_PASSWORD",
                "use_tls": True,
                "subject_template": "[cfgdrift] {severity} drift in {baseline}",
            }
        )
        channel.send(build_test_payload("0.3.0"))
        assert len(sent) == 1
        msg = sent[0]
        assert msg["To"] == "ops@example.com"
        assert msg["From"] == "alerts@example.com"
        assert msg["Subject"] == "[cfgdrift] WARN drift in <test>"
        assert "1 WARN drift(s)" in msg.get_content()
        assert login_calls == [("alerts@example.com", "s3cret")]

    def test_email_missing_password_env_raises(self, monkeypatch):
        monkeypatch.delenv("CFGDRIFT_SMTP_PASSWORD", raising=False)
        channel = EmailChannel(
            {
                "smtp_host": "smtp.example.com",
                "smtp_port": 587,
                "smtp_from": "a@b.c",
                "smtp_to": ["ops@example.com"],
                "smtp_password_env": "CFGDRIFT_SMTP_PASSWORD",
            }
        )
        with pytest.raises(ChannelError):
            channel.send(build_test_payload())

    def test_script_channel_env_and_argv(self, tmp_path):
        recorder = tmp_path / "recorder.txt"
        script = tmp_path / "notify.py"
        script.write_text(
            "import os, sys\n"
            "with open(sys.argv[1], 'w', encoding='utf-8') as fh:\n"
            "    fh.write(os.environ['CFGDRIFT_EVENT'] + '|' +\n"
            "             os.environ['CFGDRIFT_SEVERITY'] + '|' +\n"
            "             os.environ['CFGDRIFT_BASELINE'] + '|' +\n"
            "             os.environ['CFGDRIFT_DRIFT_COUNT'] + '|' +\n"
            "             sys.argv[2])\n",
            encoding="utf-8",
        )
        channel = ScriptChannel(
            {
                "command": sys.executable,
                "args": [str(script), str(recorder), "{baseline}"],
                "timeout": 30,
            }
        )
        payload = build_test_payload("0.3.0")
        payload["baseline"] = "prod-web"
        channel.send(payload)
        content = recorder.read_text(encoding="utf-8")
        assert content == "cfgdrift.test|WARN|prod-web|1|prod-web"
        # payload_env contract
        env = payload_env(payload)
        assert env["CFGDRIFT_EVENT"] == "cfgdrift.test"
        assert env["CFGDRIFT_DRIFT_ITEMS_JSON"].startswith("[")

    def test_script_nonzero_exit_raises(self, tmp_path):
        bad = tmp_path / "bad.py"
        bad.write_text("import sys\nsys.exit(3)\n", encoding="utf-8")
        channel = ScriptChannel({"command": sys.executable, "args": [str(bad)], "timeout": 30})
        with pytest.raises(ChannelError):
            channel.send(build_test_payload())

    def test_script_timeout_raises(self, tmp_path):
        slow = tmp_path / "slow.py"
        slow.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
        channel = ScriptChannel({"command": sys.executable, "args": [str(slow)], "timeout": 1})
        with pytest.raises(ChannelError):
            channel.send(build_test_payload())

    def test_retry_success_first_try(self):
        calls = []

        def send():
            calls.append(1)

        attempts = retry_with_backoff(send, attempts=3, sleep_fn=noop_sleep)
        assert attempts == 1
        assert len(calls) == 1

    def test_retry_success_after_retries(self):
        calls = []
        sleeps = []

        def send():
            calls.append(1)
            if len(calls) < 3:
                raise ChannelError("flaky")

        attempts = retry_with_backoff(
            send, attempts=3, delays=(1, 5, 30), sleep_fn=sleeps.append
        )
        assert attempts == 3
        assert len(calls) == 3
        assert sleeps == [1.0, 5.0]

    def test_retry_all_fail_raises(self):
        def send():
            raise ChannelError("nope")

        with pytest.raises(ChannelError):
            retry_with_backoff(send, attempts=3, delays=(1, 5, 30), sleep_fn=noop_sleep)


# ---------------------------------------------------------------------------
# dispatcher
# ---------------------------------------------------------------------------

class TestDispatcher:
    def _dispatcher(self, rules, state, attempts=1):
        return AlertDispatcher(
            rules,
            state,
            retry_attempts=attempts,
            retry_delays=(1, 5, 30),
            sleep_fn=noop_sleep,
        )

    def test_filters_disabled_rule(self, cfg_home):
        rule = AlertRule(
            name="off", type="webhook", enabled=False,
            config={"url": "http://127.0.0.1:1/x"},
        )
        state = AlertStateStore(os.path.join(cfg_home, "state.json"))
        disp = self._dispatcher([rule], state)
        results = disp.dispatch_report("prod", "/etc/app", make_report([make_item()]))
        assert results == []

    def test_filters_baseline_scope(self, cfg_home):
        rule = AlertRule(
            name="scoped", type="webhook", baseline="other",
            config={"url": "http://127.0.0.1:1/x"},
        )
        state = AlertStateStore(os.path.join(cfg_home, "state.json"))
        disp = self._dispatcher([rule], state)
        results = disp.dispatch_report("prod", "/etc/app", make_report([make_item()]))
        assert results == []

    def test_filters_severity_threshold(self, cfg_home):
        rule = AlertRule(
            name="critical-only", type="webhook", severity=Severity.CRITICAL,
            config={"url": "http://127.0.0.1:1/x"},
        )
        state = AlertStateStore(os.path.join(cfg_home, "state.json"))
        disp = self._dispatcher([rule], state)
        # report is WARN -> below CRITICAL threshold -> no dispatch
        results = disp.dispatch_report(
            "prod", "/etc/app", make_report([make_item(sev="WARN")], max_sev="WARN")
        )
        assert results == []
        # report is CRITICAL -> dispatched (will fail fast, attempts=1)
        results = disp.dispatch_report(
            "prod", "/etc/app", make_report([make_item(sev="CRITICAL")], max_sev="CRITICAL")
        )
        assert len(results) == 1
        assert results[0].sent is False

    def test_dispatch_sends_and_records_success(self, cfg_home, webhook_server):
        server, collector = webhook_server
        rule = AlertRule(
            name="hook", type="webhook",
            config={"url": "http://127.0.0.1:%d/hook" % server.server_port, "timeout": 5},
        )
        state = AlertStateStore(os.path.join(cfg_home, "state.json"), cooldown_seconds=600)
        disp = self._dispatcher([rule], state)
        report = make_report([make_item()], max_sev="WARN")
        results = disp.dispatch_report("prod", "/etc/app", report)
        assert len(results) == 1
        assert results[0].sent is True
        assert collector.payloads[0]["event"] == "cfgdrift.drift"
        assert collector.payloads[0]["baseline"] == "prod"
        # state recorded with cooldown
        assert len(state.entries()) == 1
        entry = next(iter(state.entries().values()))
        assert entry["last_status"] == "sent"

    def test_dispatch_dedupes_within_cooldown(self, cfg_home, webhook_server):
        server, collector = webhook_server
        rule = AlertRule(
            name="hook", type="webhook",
            config={"url": "http://127.0.0.1:%d/hook" % server.server_port, "timeout": 5},
        )
        state = AlertStateStore(os.path.join(cfg_home, "state.json"), cooldown_seconds=600)
        disp = self._dispatcher([rule], state)
        report = make_report([make_item()], max_sev="WARN")
        first = disp.dispatch_report("prod", "/etc/app", report)
        assert len(first) == 1 and first[0].sent is True
        assert len(collector.payloads) == 1
        # same drift again -> suppressed (no result, no send)
        second = disp.dispatch_report("prod", "/etc/app", report)
        assert second == []
        assert len(collector.payloads) == 1

    def test_dispatch_failure_records_failed(self, cfg_home):
        rule = AlertRule(
            name="bad", type="webhook",
            config={"url": "http://127.0.0.1:1/unreachable", "timeout": 1},
        )
        state = AlertStateStore(os.path.join(cfg_home, "state.json"), cooldown_seconds=600)
        disp = self._dispatcher([rule], state)
        results = disp.dispatch_report("prod", "/etc/app", make_report([make_item()]))
        assert len(results) == 1
        assert results[0].sent is False
        assert results[0].error is not None
        entry = next(iter(state.entries().values()))
        assert entry["last_status"] == "failed"
        # failed alerts also get a cooldown
        assert state.is_suppressed(next(iter(state.entries().keys()))) is True

    def test_test_rule_success(self, cfg_home, webhook_server):
        server, collector = webhook_server
        rule = AlertRule(
            name="hook", type="webhook",
            config={"url": "http://127.0.0.1:%d/hook" % server.server_port, "timeout": 5},
        )
        state = AlertStateStore(os.path.join(cfg_home, "state.json"))
        disp = self._dispatcher([rule], state)
        result = disp.test_rule(rule)
        assert result.sent is True
        assert collector.payloads[0]["event"] == "cfgdrift.test"

    def test_test_rule_failure(self, cfg_home):
        rule = AlertRule(
            name="bad", type="webhook",
            config={"url": "http://127.0.0.1:1/unreachable", "timeout": 1},
        )
        state = AlertStateStore(os.path.join(cfg_home, "state.json"))
        disp = self._dispatcher([rule], state)
        result = disp.test_rule(rule)
        assert result.sent is False
        assert result.error is not None
