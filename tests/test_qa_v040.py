"""Round-4 independent QA verification for cfgdrift v0.3.0 (daemon + alert).

Author: QA (software-qa-engineer-3) — written independently, with a
skeptical eye, *after* reading the v0.3.0 design (system_design.md appendix B)
and the engineer's own tests.  Does not modify any existing test file.

Coverage (T02-T05 acceptance, independently re-verified):

a. alert chain end-to-end: local http.server <- webhook <- baseline <- drift
   <- daemon --foreground --interval 1 -> payload fields -> cooldown -> clear
   cooldown state -> re-trigger.
b. severity threshold filtering (INFO does not trigger a >=WARN rule;
   CRITICAL does).
c. baseline scoping (rule bound to baseline A triggers only for A).
d. script channel: CFGDRIFT_* env contract + non-zero exit -> failure retry.
e. email channel: env-password reference, from/to/subject template,
   SMTPException -> failure.
f. retry backoff (1s/5s) and dedupe/cooldown semantics.
g. daemon lifecycle: PID write/clear, status exit codes, duplicate-start
   exit 2, stop idempotency, sentinel graceful exit, real background
   (DETACHED_PROCESS) start -> status -> stop on win32.
h. security boundaries: payload has no credential fields; alerts.yaml never
   stores SMTP plaintext passwords (only env-var names).
i. alert test connectivity: 200 -> exit 0; unreachable -> exit 2.
j. regression: v0.2.0 semantic diff/CLI still work.
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
PY = sys.executable

sys.path.insert(0, SRC)

from cfgdrift import __version__  # noqa: E402
from cfgdrift.alert.channels import (  # noqa: E402
    ChannelError,
    EmailChannel,
    ScriptChannel,
    WebhookChannel,
    retry_with_backoff,
)
from cfgdrift.alert.config import AlertConfig  # noqa: E402
from cfgdrift.alert.dispatcher import AlertDispatcher  # noqa: E402
from cfgdrift.alert.models import (  # noqa: E402
    AlertRule,
    build_drift_payload,
)
from cfgdrift.alert.state import AlertStateStore  # noqa: E402
from cfgdrift.core.differ import SemanticDiffer  # noqa: E402
from cfgdrift.core.model import (  # noqa: E402
    ChangeType,
    DriftItem,
    Report,
    ScanSummary,
    Severity,
)
from cfgdrift.daemon.daemon import DaemonManager  # noqa: E402
from cfgdrift.scanner.scanner import Scanner  # noqa: E402
from cfgdrift.storage.store import Store, utcnow_iso  # noqa: E402


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def noop_sleep(_seconds):
    """Injected sleep so retry tests never actually wait."""


def run_cli(args, env=None, cwd=None, timeout=90):
    full_env = os.environ.copy()
    full_env["PYTHONPATH"] = SRC + os.pathsep + full_env.get("PYTHONPATH", "")
    if env:
        full_env.update(env)
    return subprocess.run(
        [PY, "-m", "cfgdrift.cli"] + args,
        capture_output=True,
        text=True,
        env=full_env,
        cwd=cwd or ROOT,
        timeout=timeout,
    )


class _Collector:
    """Thread-safe webhook payload collector with optional status override."""

    def __init__(self):
        self.payloads = []
        self.headers = []
        self.lock = threading.Lock()
        self.status = 200

    def add(self, payload, headers):
        with self.lock:
            self.payloads.append(payload)
            self.headers.append(headers)

    def wait_payload(self, timeout=15):
        return self.wait_nth(1, timeout=timeout)

    def wait_nth(self, n, timeout=15):
        """Wait until at least ``n`` payloads arrived; return the n-th."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.lock:
                if len(self.payloads) >= n:
                    return self.payloads[n - 1]
            time.sleep(0.2)
        return None

    def count(self):
        with self.lock:
            return len(self.payloads)


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


@pytest.fixture()
def project(tmp_path):
    conf = tmp_path / "conf"
    conf.mkdir()
    (conf / "app.json").write_text(
        json.dumps({"server": {"host": "localhost", "port": 8080}, "debug": False}),
        encoding="utf-8",
    )
    return str(conf)


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


def make_dispatcher(rules, state_path, cfg_home, attempts=1, sleep_fn=noop_sleep):
    return AlertDispatcher(
        rules,
        AlertStateStore(state_path),
        retry_attempts=attempts,
        retry_delays=(1, 5, 30),
        sleep_fn=sleep_fn,
    )


# ---------------------------------------------------------------------------
# j. regression: v0.2.0 semantic diff + version
# ---------------------------------------------------------------------------

class TestRegressionV020:
    def test_version_strings(self):
        assert __version__ == "0.5.0"
        import cfgdrift._cfgdrift as c

        assert c.version() == "0.5.0-c"

    def test_semantic_diff_still_works(self, tmp_path):
        """A plain v0.2.0-style scan->baseline->diff->store cycle still works."""
        conf = tmp_path / "conf"
        conf.mkdir()
        (conf / "app.json").write_text(
            json.dumps({"server": {"port": 8080}}), encoding="utf-8"
        )
        store = Store(str(tmp_path / "cfgdrift.db"))
        snapshot = Scanner().scan_path(str(conf))
        store.create_baseline(
            name="prod", description="", scan_root=str(conf),
            format="auto", data=snapshot,
        )
        (conf / "app.json").write_text(
            json.dumps({"server": {"port": 9090}}), encoding="utf-8"
        )
        current = Scanner().scan_path(str(conf))
        baseline = store.get_baseline("prod")
        items, summary = SemanticDiffer().diff_snapshot(
            baseline.data, current, store.list_rules(baseline.id)
        )
        store.close()
        assert summary.total == 1
        assert items[0].key_path == "server.port"
        assert items[0].change_type == ChangeType.MODIFIED
        assert items[0].severity == Severity.WARN

    def test_cli_diff_exit_code_1(self, project, cfg_home):
        """Drift present -> `cfgdrift diff` exits 1 (v0.2.0 CLI contract)."""
        env = {"CFGDRIFT_HOME": cfg_home}
        assert run_cli(["init"], env=env).returncode == 0
        p = run_cli(["scan", project, "--save-as-baseline", "prod"], env=env)
        assert p.returncode == 0, p.stdout + p.stderr
        with open(os.path.join(project, "app.json"), "w", encoding="utf-8") as fh:
            json.dump({"server": {"host": "localhost", "port": 9999}}, fh)
        p = run_cli(["diff", project, "--baseline", "prod"], env=env)
        assert p.returncode == 1, "expected exit 1 for drift, got %d: %s" % (
            p.returncode, p.stdout + p.stderr
        )


# ---------------------------------------------------------------------------
# h. security boundaries
# ---------------------------------------------------------------------------

class TestSecurityBoundaries:
    def test_payload_has_no_credential_fields(self):
        items = [
            DriftItem(
                key_path="server.port",
                change_type=ChangeType.MODIFIED,
                severity=Severity.WARN,
                file="conf/app.json",
                old_value="8080",
                new_value="9090",
            )
        ]
        payload = build_drift_payload(make_report(items, max_sev="WARN"),
                                      "prod", "/etc/app", "0.3.0")
        expected_top = {
            "event", "version", "timestamp", "severity", "baseline",
            "target", "drift_count", "drift_items", "summary",
        }
        assert set(payload.keys()) == expected_top
        # no credential-ish keys anywhere in the payload tree
        blob = json.dumps(payload)
        for secret_key in ("password", "secret", "token", "smtp", "headers"):
            assert secret_key not in blob.lower(), (
                "payload leaked credential key %r" % secret_key
            )

    def test_alerts_yaml_never_stores_smtp_plaintext(self, cfg_home):
        env = {"CFGDRIFT_HOME": cfg_home}
        p = run_cli(
            ["alert", "add", "--name", "mail", "--type", "email",
             "--smtp-host", "smtp.example.com", "--smtp-port", "587",
             "--from", "a@b.c", "--to", "ops@b.c",
             "--smtp-password-env", "CFGDRIFT_SMTP_PASSWORD"],
            env=env,
        )
        assert p.returncode == 0, p.stdout + p.stderr
        path = os.path.join(cfg_home, "alerts.yaml")
        content = open(path, encoding="utf-8").read()
        # the env-var NAME is stored, never a literal password
        assert "smtp_password_env: CFGDRIFT_SMTP_PASSWORD" in content
        assert "smtp_password:" not in content  # no literal password key
        assert "s3cret" not in content
        assert "hunter2" not in content


# ---------------------------------------------------------------------------
# b. severity threshold filtering
# ---------------------------------------------------------------------------

class TestSeverityThreshold:
    def test_info_drift_does_not_trigger_warn_rule(self, cfg_home, webhook_server):
        server, collector = webhook_server
        rule = AlertRule(
            name="warn-rule", type="webhook",
            severity=Severity.WARN,
            config={"url": "http://127.0.0.1:%d/hook" % server.server_port,
                    "timeout": 5},
        )
        disp = make_dispatcher([rule], os.path.join(cfg_home, "s.json"), cfg_home)
        # max severity INFO < WARN -> filtered
        results = disp.dispatch_report(
            "prod", "/etc/app", make_report([make_item(sev="INFO")], max_sev="INFO")
        )
        assert results == []
        assert collector.count() == 0

    def test_critical_drift_triggers_warn_rule(self, cfg_home, webhook_server):
        server, collector = webhook_server
        rule = AlertRule(
            name="warn-rule", type="webhook",
            severity=Severity.WARN,
            config={"url": "http://127.0.0.1:%d/hook" % server.server_port,
                    "timeout": 5},
        )
        disp = make_dispatcher([rule], os.path.join(cfg_home, "s.json"), cfg_home)
        results = disp.dispatch_report(
            "prod", "/etc/app",
            make_report([make_item(sev="CRITICAL", change="removed")],
                        max_sev="CRITICAL"),
        )
        assert len(results) == 1 and results[0].sent is True
        assert collector.payloads[0]["severity"] == "CRITICAL"

    def test_none_threshold_triggers_everything(self, cfg_home, webhook_server):
        server, collector = webhook_server
        rule = AlertRule(
            name="all", type="webhook", severity=Severity.NONE,
            config={"url": "http://127.0.0.1:%d/hook" % server.server_port,
                    "timeout": 5},
        )
        disp = make_dispatcher([rule], os.path.join(cfg_home, "s.json"), cfg_home)
        results = disp.dispatch_report(
            "prod", "/etc/app", make_report([make_item(sev="INFO")], max_sev="INFO")
        )
        assert len(results) == 1 and results[0].sent is True


# ---------------------------------------------------------------------------
# c. baseline scoping
# ---------------------------------------------------------------------------

class TestBaselineScope:
    def test_rule_scoped_to_a_ignores_b(self, cfg_home, webhook_server):
        server, collector = webhook_server
        url = "http://127.0.0.1:%d/hook" % server.server_port
        rule_a = AlertRule(
            name="a-only", type="webhook", baseline="A",
            config={"url": url, "timeout": 5},
        )
        rule_all = AlertRule(
            name="all-baselines", type="webhook", baseline=None,
            config={"url": url, "timeout": 5},
        )
        disp = make_dispatcher(
            [rule_a, rule_all], os.path.join(cfg_home, "s.json"), cfg_home
        )
        # drift on baseline B: only the un-scoped rule fires
        results = disp.dispatch_report(
            "B", "/etc/b", make_report([make_item()], max_sev="WARN")
        )
        names = [r.rule.name for r in results]
        assert names == ["all-baselines"]
        # drift on baseline A: both fire (one payload each)
        results = disp.dispatch_report(
            "A", "/etc/a", make_report([make_item()], max_sev="WARN")
        )
        names = [r.rule.name for r in results]
        assert set(names) == {"a-only", "all-baselines"}
        assert collector.count() == 3


# ---------------------------------------------------------------------------
# d. script channel
# ---------------------------------------------------------------------------

class TestScriptChannel:
    def test_cfgdrift_env_contract(self, tmp_path, cfg_home):
        recorder = tmp_path / "env.txt"
        script = tmp_path / "dump.py"
        script.write_text(
            "import json, os, sys\n"
            "data = {k: os.environ.get(k, '') for k in sorted(os.environ)\n"
            "        if k.startswith('CFGDRIFT_')}\n"
            "with open(sys.argv[1], 'w', encoding='utf-8') as fh:\n"
            "    json.dump(data, fh, ensure_ascii=False)\n",
            encoding="utf-8",
        )
        rule = AlertRule(
            name="script", type="script",
            config={"command": PY, "args": [str(script), str(recorder)],
                    "timeout": 30},
        )
        disp = make_dispatcher([rule], os.path.join(cfg_home, "s.json"), cfg_home)
        results = disp.dispatch_report(
            "prod", "/etc/app",
            make_report([make_item(change="removed", sev="CRITICAL")],
                        max_sev="CRITICAL"),
        )
        assert len(results) == 1 and results[0].sent is True
        data = json.loads(open(recorder, encoding="utf-8").read())
        assert data["CFGDRIFT_EVENT"] == "cfgdrift.drift"
        assert data["CFGDRIFT_VERSION"] == "0.5.0"
        assert data["CFGDRIFT_SEVERITY"] == "CRITICAL"
        assert data["CFGDRIFT_BASELINE"] == "prod"
        assert data["CFGDRIFT_TARGET"] == "/etc/app"
        assert data["CFGDRIFT_DRIFT_COUNT"] == "1"
        assert "1 CRITICAL drift(s)" in data["CFGDRIFT_SUMMARY"]
        items = json.loads(data["CFGDRIFT_DRIFT_ITEMS_JSON"])
        assert items[0]["key"] == "server.port"
        assert items[0]["file"] == "conf/app.json"
        assert items[0]["change_type"] == "removed"

    def test_nonzero_exit_counts_as_failure_and_retries(self, tmp_path, cfg_home):
        bad = tmp_path / "bad.py"
        bad.write_text("import sys\nsys.exit(3)\n", encoding="utf-8")
        sleeps = []
        rule = AlertRule(
            name="fail", type="script",
            config={"command": PY, "args": [str(bad)], "timeout": 30},
        )
        disp = make_dispatcher(
            [rule], os.path.join(cfg_home, "s.json"), cfg_home,
            attempts=3, sleep_fn=sleeps.append,
        )
        results = disp.dispatch_report(
            "prod", "/etc/app", make_report([make_item()], max_sev="WARN")
        )
        assert len(results) == 1
        assert results[0].sent is False
        assert results[0].attempts == 3
        assert sleeps == [1.0, 5.0]  # retry backoff 1s then 5s
        entry = next(iter(disp.state.entries().values()))
        assert entry["last_status"] == "failed"
        assert disp.state.is_suppressed(next(iter(disp.state.entries().keys())))


# ---------------------------------------------------------------------------
# e. email channel
# ---------------------------------------------------------------------------

class TestEmailChannel:
    def test_env_password_and_subject_template(self, monkeypatch, cfg_home):
        sent = []
        login_calls = []

        class FakeSMTP:
            def __init__(self, host, port, timeout=None):
                self.host = host
                self.port = port

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def starttls(self):
                pass

            def login(self, user, password):
                login_calls.append((user, password))

            def send_message(self, msg):
                sent.append(msg)

        monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
        monkeypatch.setenv("CFGDRIFT_SMTP_PASSWORD", "real-pw-123")
        rule = AlertRule(
            name="mail", type="email",
            severity=Severity.CRITICAL,
            config={
                "smtp_host": "smtp.example.com",
                "smtp_port": 587,
                "smtp_user": "alerts@example.com",
                "smtp_from": "alerts@example.com",
                "smtp_to": ["ops@example.com"],
                "smtp_password_env": "CFGDRIFT_SMTP_PASSWORD",
                "use_tls": True,
                "subject_template": "[cfgdrift] {severity} drift in {baseline}",
            },
        )
        disp = make_dispatcher([rule], os.path.join(cfg_home, "s.json"), cfg_home)
        results = disp.dispatch_report(
            "prod", "/etc/app",
            make_report([make_item(change="removed", sev="CRITICAL")],
                        max_sev="CRITICAL"),
        )
        assert len(results) == 1 and results[0].sent is True
        assert login_calls == [("alerts@example.com", "real-pw-123")]
        msg = sent[0]
        assert msg["From"] == "alerts@example.com"
        assert msg["To"] == "ops@example.com"
        assert msg["Subject"] == "[cfgdrift] CRITICAL drift in prod"
        assert "1 CRITICAL drift(s)" in msg.get_content()

    def test_smtp_exception_records_failure(self, monkeypatch, cfg_home):
        def boom(*args, **kwargs):
            raise smtplib.SMTPException("connection refused")

        monkeypatch.setattr(smtplib, "SMTP", boom)
        monkeypatch.setenv("CFGDRIFT_SMTP_PASSWORD", "pw")
        rule = AlertRule(
            name="mail", type="email",
            config={
                "smtp_host": "smtp.example.com", "smtp_port": 587,
                "smtp_from": "a@b.c", "smtp_to": ["ops@b.c"],
                "smtp_password_env": "CFGDRIFT_SMTP_PASSWORD",
            },
        )
        disp = make_dispatcher([rule], os.path.join(cfg_home, "s.json"), cfg_home)
        results = disp.dispatch_report(
            "prod", "/etc/app", make_report([make_item()], max_sev="WARN")
        )
        assert len(results) == 1 and results[0].sent is False
        # source wraps SMTPException into ChannelError preserving the reason
        assert "email send failed" in (results[0].error or "")
        assert "connection refused" in (results[0].error or "")
        entry = next(iter(disp.state.entries().values()))
        assert entry["last_status"] == "failed"

    def test_missing_password_env_raises_channel_error(self, monkeypatch, cfg_home):
        monkeypatch.delenv("CFGDRIFT_SMTP_PASSWORD", raising=False)
        channel = EmailChannel(
            {
                "smtp_host": "smtp.example.com", "smtp_port": 587,
                "smtp_from": "a@b.c", "smtp_to": ["ops@b.c"],
                "smtp_password_env": "CFGDRIFT_SMTP_PASSWORD",
            }
        )
        with pytest.raises(ChannelError):
            channel.send(build_drift_payload(
                make_report([make_item()], max_sev="WARN"), "prod", "/etc/app",
                "0.3.0",
            ))


# ---------------------------------------------------------------------------
# f. retry & dedupe
# ---------------------------------------------------------------------------

class TestRetryAndDedupe:
    def test_retry_sleeps_are_1s_then_5s(self):
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
        assert calls == [1, 1, 1]
        assert sleeps == [1.0, 5.0]

    def test_retry_all_fail_raises_last_error(self):
        def send():
            raise ChannelError("boom")

        with pytest.raises(ChannelError):
            retry_with_backoff(send, attempts=3, delays=(1, 5, 30),
                               sleep_fn=noop_sleep)

    def test_same_rule_same_fingerprint_suppressed_in_cooldown(
        self, cfg_home, webhook_server
    ):
        server, collector = webhook_server
        rule = AlertRule(
            name="hook", type="webhook",
            config={"url": "http://127.0.0.1:%d/hook" % server.server_port,
                    "timeout": 5},
        )
        disp = make_dispatcher([rule], os.path.join(cfg_home, "s.json"), cfg_home)
        report = make_report([make_item()], max_sev="WARN")
        first = disp.dispatch_report("prod", "/etc/app", report)
        assert len(first) == 1 and first[0].sent is True
        assert collector.count() == 1
        second = disp.dispatch_report("prod", "/etc/app", report)
        assert second == []
        assert collector.count() == 1

    def test_different_rule_names_each_trigger_once(self, cfg_home, webhook_server):
        server, collector = webhook_server
        url = "http://127.0.0.1:%d/hook" % server.server_port
        r1 = AlertRule(name="one", type="webhook", config={"url": url, "timeout": 5})
        r2 = AlertRule(name="two", type="webhook", config={"url": url, "timeout": 5})
        disp = make_dispatcher([r1, r2], os.path.join(cfg_home, "s.json"), cfg_home)
        report = make_report([make_item()], max_sev="WARN")
        results = disp.dispatch_report("prod", "/etc/app", report)
        assert {r.rule.name for r in results} == {"one", "two"}
        assert collector.count() == 2


# ---------------------------------------------------------------------------
# g. daemon lifecycle (CLI-level + real background on win32)
# ---------------------------------------------------------------------------

def _write_sentinel(home, pid):
    with open(os.path.join(home, "daemon.stop"), "w", encoding="utf-8") as fh:
        fh.write(str(pid))


def _start_foreground(project, home, extra=None):
    """Start the daemon in --foreground; returns the Popen handle."""
    args = [
        PY, "-m", "cfgdrift.cli", "daemon", "start", "--foreground",
        "--target", project, "--baseline", "prod", "--interval", "1",
        "--log-level", "INFO",
    ]
    if extra:
        args += extra
    return subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={**os.environ, "PYTHONPATH": SRC, "CFGDRIFT_HOME": home},
    )


class TestDaemonLifecycle:
    def test_status_stopped_then_pid_then_corrupt_then_stale(self, cfg_home):
        env = {"CFGDRIFT_HOME": cfg_home}
        # no pid file -> stopped (exit 1)
        p = run_cli(["daemon", "status"], env=env)
        assert p.returncode == 1, p.stdout + p.stderr
        # corrupt pid -> error (exit 2)
        with open(os.path.join(cfg_home, "daemon.pid"), "w", encoding="utf-8") as fh:
            fh.write("not-a-pid")
        p = run_cli(["daemon", "status"], env=env)
        assert p.returncode == 2, p.stdout + p.stderr
        # stale pid -> cleaned, stopped (exit 1), pid file removed
        with open(os.path.join(cfg_home, "daemon.pid"), "w", encoding="utf-8") as fh:
            fh.write(str(2 ** 30))
        p = run_cli(["daemon", "status"], env=env)
        assert p.returncode == 1, p.stdout + p.stderr
        assert not os.path.exists(os.path.join(cfg_home, "daemon.pid"))

    def test_stop_idempotent_when_not_running(self, cfg_home):
        env = {"CFGDRIFT_HOME": cfg_home}
        p = run_cli(["daemon", "stop"], env=env)
        assert p.returncode == 0, p.stdout + p.stderr
        assert "not running" in p.stdout

    def test_foreground_lifecycle_and_duplicate_start(self, cfg_home, project):
        env = {"CFGDRIFT_HOME": cfg_home}
        assert run_cli(["init"], env=env).returncode == 0
        p = run_cli(["scan", project, "--save-as-baseline", "prod"], env=env)
        assert p.returncode == 0, p.stdout + p.stderr

        proc = _start_foreground(project, cfg_home)
        try:
            # wait for pid file
            pid_file = os.path.join(cfg_home, "daemon.pid")
            deadline = time.time() + 10
            while time.time() < deadline and not os.path.exists(pid_file):
                time.sleep(0.2)
            assert os.path.exists(pid_file), "pid file not written"
            pid = int(open(pid_file, encoding="utf-8").read().strip())
            assert pid == proc.pid

            # status from another process -> running (exit 0)
            p = run_cli(["daemon", "status"], env=env)
            assert p.returncode == 0, p.stdout + p.stderr
            assert "running (pid=%d)" % pid in p.stdout

            # duplicate start -> exit 2 (already running)
            p = run_cli(
                ["daemon", "start", "--target", project, "--baseline", "prod",
                 "--interval", "1"],
                env=env,
            )
            assert p.returncode == 2, p.stdout + p.stderr
            assert "already running" in (p.stdout + p.stderr)
        finally:
            # graceful stop via sentinel file
            _write_sentinel(cfg_home, proc.pid)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        assert proc.returncode == 0, "foreground daemon exit %d" % proc.returncode
        assert not os.path.exists(os.path.join(cfg_home, "daemon.pid"))
        assert not os.path.exists(os.path.join(cfg_home, "daemon.stop"))

        # status after stop -> stopped (exit 1)
        p = run_cli(["daemon", "status"], env=env)
        assert p.returncode == 1, p.stdout + p.stderr

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows detached process")
    def test_windows_background_daemon_start_status_stop(self, cfg_home, project):
        """Real DETACHED_PROCESS background daemon on win32."""
        env = {"CFGDRIFT_HOME": cfg_home}
        assert run_cli(["init"], env=env).returncode == 0
        p = run_cli(["scan", project, "--save-as-baseline", "prod"], env=env)
        assert p.returncode == 0, p.stdout + p.stderr

        manager = DaemonManager(cfg_home)
        opts = {
            "targets": [project],
            "baseline": "prod",
            "fmt": "auto",
            "interval": 1,
            "store": os.path.join(cfg_home, "cfgdrift.db"),
            "log_level": "INFO",
            "log_file": os.path.join(cfg_home, "logs", "daemon.log"),
            "alerts_config": os.path.join(cfg_home, "alerts.yaml"),
            "alert_state": os.path.join(cfg_home, "alert_state.json"),
        }
        code = manager.start(opts)
        assert code == 0, "daemon start failed"
        assert os.path.exists(manager.pid_file)
        pid = int(open(manager.pid_file, encoding="utf-8").read().strip())

        try:
            # status via CLI -> running (exit 0)
            p = run_cli(["daemon", "status"], env=env, timeout=30)
            assert p.returncode == 0, p.stdout + p.stderr
            assert "running (pid=%d)" % pid in p.stdout

            # worker actually scans: daemon.log exists and eventually has cycles
            log_file = os.path.join(cfg_home, "logs", "daemon.log")
            deadline = time.time() + 10
            content = ""
            while time.time() < deadline:
                if os.path.exists(log_file):
                    content = open(log_file, encoding="utf-8").read()
                    if "scan cycle done" in content:
                        break
                time.sleep(0.5)
            assert "scan cycle done" in content, "worker did not run cycles"
        finally:
            code = manager.stop(timeout=10)
            assert code == 0, "daemon stop failed (code %d)" % code

        assert not os.path.exists(manager.pid_file)
        p = run_cli(["daemon", "status"], env=env)
        assert p.returncode == 1, p.stdout + p.stderr


# ---------------------------------------------------------------------------
# i. alert test connectivity
# ---------------------------------------------------------------------------

class TestAlertTestCLI:
    def test_webhook_ok_exit_0(self, cfg_home, webhook_server):
        server, collector = webhook_server
        env = {"CFGDRIFT_HOME": cfg_home}
        url = "http://127.0.0.1:%d/hook" % server.server_port
        p = run_cli(
            ["alert", "add", "--name", "hook", "--type", "webhook", "--url", url],
            env=env,
        )
        assert p.returncode == 0, p.stdout + p.stderr
        p = run_cli(["alert", "test", "hook"], env=env)
        assert p.returncode == 0, p.stdout + p.stderr
        assert "ok" in p.stdout
        # the test payload event must be cfgdrift.test
        assert collector.payloads[0]["event"] == "cfgdrift.test"

    def test_unreachable_exit_2(self, cfg_home):
        env = {"CFGDRIFT_HOME": cfg_home}
        p = run_cli(
            ["alert", "add", "--name", "bad", "--type", "webhook",
             "--url", "http://127.0.0.1:1/unreachable"],
            env=env,
        )
        assert p.returncode == 0, p.stdout + p.stderr
        p = run_cli(["alert", "test", "bad"], env=env)
        assert p.returncode == 2, p.stdout + p.stderr
        assert "failed" in p.stderr


# ---------------------------------------------------------------------------
# a. END-TO-END alert chain (the core scenario)
# ---------------------------------------------------------------------------

class TestEndToEndAlertChain:
    def test_full_chain_payload_cooldown_retrigger(self, cfg_home, project,
                                                   webhook_server):
        server, collector = webhook_server
        env = {"CFGDRIFT_HOME": cfg_home}

        # init + baseline
        p = run_cli(["init"], env=env)
        assert p.returncode == 0, p.stdout + p.stderr
        p = run_cli(["scan", project, "--save-as-baseline", "prod"], env=env)
        assert p.returncode == 0, p.stdout + p.stderr

        # add webhook alert
        url = "http://127.0.0.1:%d/hook" % server.server_port
        p = run_cli(
            ["alert", "add", "--name", "hook", "--type", "webhook", "--url", url],
            env=env,
        )
        assert p.returncode == 0, p.stdout + p.stderr

        # create drift: port 8080 -> 9090 (modified, WARN)
        with open(os.path.join(project, "app.json"), "w", encoding="utf-8") as fh:
            json.dump({"server": {"host": "localhost", "port": 9090},
                       "debug": False}, fh)

        # start daemon foreground
        proc = _start_foreground(project, cfg_home)
        try:
            payload = collector.wait_payload(timeout=20)
            assert payload is not None, "no webhook payload received"

            # -- payload field correctness (independent assertions) --
            assert payload["event"] == "cfgdrift.drift"
            assert payload["version"] == "0.5.0"
            assert payload["baseline"] == "prod"
            assert payload["target"] == os.path.abspath(project)
            assert payload["drift_count"] >= 1
            assert payload["severity"] == "WARN"  # modified -> WARN
            assert payload["summary"] == (
                "%d WARN drift(s) in baseline prod" % payload["drift_count"]
            )
            by_key = {item["key"]: item for item in payload["drift_items"]}
            assert "server.port" in by_key
            port_item = by_key["server.port"]
            assert port_item["file"] == "app.json"
            assert port_item["change_type"] == "modified"
            assert port_item["severity"] == "WARN"
            # baseline/current preserve JSON types (int 8080, not "8080")
            assert port_item["baseline"] == 8080
            assert port_item["current"] == 9090
            # security: no credential fields anywhere
            blob = json.dumps(payload)
            for secret_key in ("password", "secret", "token", "smtp"):
                assert secret_key not in blob.lower()

            # -- cooldown: same drift must not re-send within 10 min --
            time.sleep(3)
            assert collector.count() == 1, (
                "cooldown did not suppress re-send; count=%d"
                % collector.count()
            )
        finally:
            _write_sentinel(cfg_home, proc.pid)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        assert proc.returncode == 0, "foreground daemon exit %d" % proc.returncode
        assert not os.path.exists(os.path.join(cfg_home, "daemon.pid"))

        # -- clear cooldown state -> the same drift triggers again --
        state_file = os.path.join(cfg_home, "alert_state.json")
        assert os.path.exists(state_file)
        os.remove(state_file)

        proc2 = _start_foreground(project, cfg_home)
        try:
            # wait for a *second* delivery (payload index 1), not the stale first
            payload2 = collector.wait_nth(2, timeout=20)
            assert payload2 is not None, "no re-trigger after cooldown clear"
            assert collector.count() == 2
            assert payload2["baseline"] == "prod"
            assert payload2["drift_count"] >= 1
            # the same drift (same signature) was re-delivered after state cleared
            keys2 = {item["key"] for item in payload2["drift_items"]}
            assert "server.port" in keys2
            sig1 = sorted(
                (i["key"], i["file"], i["change_type"])
                for i in payload["drift_items"]
            )
            sig2 = sorted(
                (i["key"], i["file"], i["change_type"])
                for i in payload2["drift_items"]
            )
            assert sig1 == sig2
        finally:
            _write_sentinel(cfg_home, proc2.pid)
            try:
                proc2.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc2.kill()
                proc2.wait(timeout=5)
        assert proc2.returncode == 0

        # daemon.log records cycle + dispatch for both runs
        log_file = os.path.join(cfg_home, "logs", "daemon.log")
        content = open(log_file, encoding="utf-8").read()
        assert "scan cycle done" in content
        assert "alert hook dispatched" in content
