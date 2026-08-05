"""End-to-end tests for cfgdrift v0.3.0 (daemon + alert).

Scenario covered (T05 acceptance):

1. init + baseline -> webhook alert -> modify config (drift) ->
   ``daemon start --foreground --interval 1`` detects the drift, delivers a
   webhook payload, respects the 10-minute cooldown (no duplicate), and
   writes daemon.log.
2. ``alert add / list / remove / test`` CLI wiring and exit codes.
3. ``daemon status`` / ``daemon start`` pre-flight exit codes.

The daemon runs in ``--foreground`` mode because the sandbox may not allow a
true background process; the worker loop, PID/sentinel handling, and alert
dispatch are identical in both modes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
PY = sys.executable


def run_cli(args, env=None, cwd=None):
    full_env = os.environ.copy()
    full_env["PYTHONPATH"] = SRC + os.pathsep + full_env.get("PYTHONPATH", "")
    if env:
        full_env.update(env)
    proc = subprocess.run(
        [PY, "-m", "cfgdrift.cli"] + args,
        capture_output=True,
        text=True,
        env=full_env,
        cwd=cwd or ROOT,
        timeout=90,
    )
    return proc


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
        json.dumps(
            {"server": {"host": "localhost", "port": 8080}, "debug": False}
        ),
        encoding="utf-8",
    )
    return str(conf)


class _Collector:
    def __init__(self):
        self.payloads = []
        self.lock = threading.Lock()
        self.status = 200

    def add(self, payload):
        with self.lock:
            self.payloads.append(payload)

    def wait_payload(self, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.lock:
                if self.payloads:
                    return self.payloads[0]
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
        self.server.collector.add(payload)
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


def test_e2e_daemon_foreground_webhook(cfg_home, project, webhook_server):
    server, collector = webhook_server
    env = {"CFGDRIFT_HOME": cfg_home}

    # init + baseline
    p = run_cli(["init"], env=env)
    assert p.returncode == 0, p.stdout + p.stderr
    p = run_cli(["scan", project, "--save-as-baseline", "prod"], env=env)
    assert p.returncode == 0, p.stdout + p.stderr

    # add a webhook alert pointing at the local server
    url = "http://127.0.0.1:%d/hook" % server.server_port
    p = run_cli(
        ["alert", "add", "--name", "hook", "--type", "webhook", "--url", url],
        env=env,
    )
    assert p.returncode == 0, p.stdout + p.stderr
    p = run_cli(["alert", "list"], env=env)
    assert p.returncode == 0
    assert "hook" in p.stdout

    # create a drift
    app = os.path.join(project, "app.json")
    with open(app, "w", encoding="utf-8") as fh:
        json.dump(
            {"server": {"host": "localhost", "port": 9090}, "debug": True}, fh
        )

    # run the daemon in the foreground
    proc = subprocess.Popen(
        [
            PY, "-m", "cfgdrift.cli", "daemon", "start", "--foreground",
            "--target", project, "--baseline", "prod",
            "--interval", "1", "--log-level", "INFO",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={**os.environ, "PYTHONPATH": SRC, "CFGDRIFT_HOME": cfg_home},
    )
    try:
        payload = collector.wait_payload(timeout=15)
        assert payload is not None, "no webhook payload received"
        assert payload["event"] == "cfgdrift.drift"
        assert payload["version"] == "0.8.0"
        assert payload["baseline"] == "prod"
        assert payload["drift_count"] >= 1
        assert payload["summary"].startswith(
            "%d " % payload["drift_count"]
        )
        keys = {item["key"] for item in payload["drift_items"]}
        assert "server.port" in keys or "debug" in keys

        # cooldown: further cycles must not re-send the same drift
        time.sleep(3)
        assert collector.count() == 1, "cooldown did not suppress re-send"

        # daemon.log exists and contains cycle + dispatch records
        log_file = os.path.join(cfg_home, "logs", "daemon.log")
        assert os.path.exists(log_file), "daemon.log not written"
        content = open(log_file, encoding="utf-8").read()
        assert "scan cycle done" in content
        assert "alert hook dispatched" in content
    finally:
        # graceful stop via the sentinel file (content = expected pid)
        with open(os.path.join(cfg_home, "daemon.stop"), "w", encoding="utf-8") as fh:
            fh.write(str(proc.pid))
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    assert proc.returncode == 0, "foreground daemon did not exit 0"

    # pid file should be cleaned up by the worker
    assert not os.path.exists(os.path.join(cfg_home, "daemon.pid"))


def test_alert_test_script_ok_and_fail(cfg_home, tmp_path):
    env = {"CFGDRIFT_HOME": cfg_home}
    # ok script: asserts the connectivity-test event
    ok_script = tmp_path / "ok.py"
    ok_script.write_text(
        "import os\n"
        "assert os.environ['CFGDRIFT_EVENT'] == 'cfgdrift.test', os.environ\n"
        "assert os.environ['CFGDRIFT_VERSION'] == '0.8.0'\n",
        encoding="utf-8",
    )
    p = run_cli(
        ["alert", "add", "--name", "ok", "--type", "script",
         "--command", PY, "--arg", str(ok_script)],
        env=env,
    )
    assert p.returncode == 0, p.stdout + p.stderr
    p = run_cli(["alert", "test", "ok"], env=env)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "ok" in p.stdout

    # failing script -> exit 2 (retry policy applies: 3 attempts, 1s+5s waits)
    bad_script = tmp_path / "bad.py"
    bad_script.write_text("import sys\nsys.exit(3)\n", encoding="utf-8")
    p = run_cli(
        ["alert", "add", "--name", "bad", "--type", "script",
         "--command", PY, "--arg", str(bad_script)],
        env=env,
    )
    assert p.returncode == 0, p.stdout + p.stderr
    p = run_cli(["alert", "test", "bad"], env=env, )
    assert p.returncode == 2, p.stdout + p.stderr
    assert "failed" in p.stderr

    # remove + list
    p = run_cli(["alert", "remove", "ok"], env=env)
    assert p.returncode == 0, p.stdout + p.stderr
    p = run_cli(["alert", "list"], env=env)
    assert "ok" not in p.stdout
    assert "bad" in p.stdout


def test_alert_add_webhook_requires_url(cfg_home):
    env = {"CFGDRIFT_HOME": cfg_home}
    p = run_cli(["alert", "add", "--name", "x", "--type", "webhook"], env=env)
    assert p.returncode == 2
    assert "url" in (p.stdout + p.stderr).lower()


def test_alert_duplicate_name_exits_2(cfg_home, webhook_server):
    server, _ = webhook_server
    env = {"CFGDRIFT_HOME": cfg_home}
    url = "http://127.0.0.1:%d/hook" % server.server_port
    p = run_cli(
        ["alert", "add", "--name", "dup", "--type", "webhook", "--url", url], env=env
    )
    assert p.returncode == 0
    p = run_cli(
        ["alert", "add", "--name", "dup", "--type", "webhook", "--url", url], env=env
    )
    assert p.returncode == 2
    assert "already exists" in (p.stdout + p.stderr)


def test_daemon_status_not_running(cfg_home):
    p = run_cli(["daemon", "status"], env={"CFGDRIFT_HOME": cfg_home})
    assert p.returncode == 1
    assert "not running" in p.stdout


def test_daemon_start_requires_existing_baseline(cfg_home, project):
    env = {"CFGDRIFT_HOME": cfg_home}
    run_cli(["init"], env=env)
    p = run_cli(
        ["daemon", "start", "--target", project, "--baseline", "missing"],
        env=env,
    )
    assert p.returncode == 2
    assert "baseline" in (p.stdout + p.stderr).lower()


def test_daemon_start_rejects_bad_interval(cfg_home, project):
    env = {"CFGDRIFT_HOME": cfg_home}
    run_cli(["init"], env=env)
    run_cli(["scan", project, "--save-as-baseline", "prod"], env=env)
    p = run_cli(
        ["daemon", "start", "--target", project, "--baseline", "prod",
         "--interval", "0"],
        env=env,
    )
    assert p.returncode == 2
