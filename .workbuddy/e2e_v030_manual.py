"""Standalone v0.3.0 end-to-end verification (manual, outside pytest).

Pipeline: local http.server webhook <- alert add webhook <- baseline <-
drift <- daemon start --foreground --interval 1 -> payload -> cooldown ->
daemon.log -> graceful stop via sentinel.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SRC = os.path.join(ROOT, "src")
PY = sys.executable


class Collector:
    def __init__(self):
        self.payloads = []
        self.lock = threading.Lock()

    def add(self, payload):
        with self.lock:
            self.payloads.append(payload)

    def wait(self, timeout=15):
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


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        self.server.collector.add(json.loads(body.decode("utf-8")))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


def run_cli(args, env):
    full = os.environ.copy()
    full["PYTHONPATH"] = SRC + os.pathsep + full.get("PYTHONPATH", "")
    full.update(env)
    return subprocess.run([PY, "-m", "cfgdrift.cli"] + args, capture_output=True,
                          text=True, env=full, timeout=90)


def main() -> int:
    work = tempfile.mkdtemp(prefix="cfgdrift-e2e-")
    home = os.path.join(work, "home")
    os.makedirs(home)
    conf = os.path.join(work, "conf")
    os.makedirs(conf)
    env = {"CFGDRIFT_HOME": home}

    collector = Collector()
    server = HTTPServer(("127.0.0.1", 0), Handler)
    server.collector = collector
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    proc = None

    try:
        app = os.path.join(conf, "app.json")
        with open(app, "w", encoding="utf-8") as fh:
            json.dump({"server": {"host": "localhost", "port": 8080}, "debug": False}, fh)

        p = run_cli(["init"], env); assert p.returncode == 0, p.stdout + p.stderr
        p = run_cli(["scan", conf, "--save-as-baseline", "prod"], env)
        assert p.returncode == 0, p.stdout + p.stderr

        url = "http://127.0.0.1:%d/hook" % server.server_port
        p = run_cli(["alert", "add", "--name", "hook", "--type", "webhook", "--url", url], env)
        assert p.returncode == 0, p.stdout + p.stderr

        # create a drift
        with open(app, "w", encoding="utf-8") as fh:
            json.dump({"server": {"host": "localhost", "port": 9090}, "debug": True}, fh)

        proc = subprocess.Popen(
            [PY, "-m", "cfgdrift.cli", "daemon", "start", "--foreground",
             "--target", conf, "--baseline", "prod", "--interval", "1",
             "--log-level", "INFO"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            env={**os.environ, "PYTHONPATH": SRC, "CFGDRIFT_HOME": home},
        )
        payload = collector.wait(timeout=15)
        assert payload is not None, "no webhook payload received"
        print("PAYLOAD:")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        assert payload["event"] == "cfgdrift.drift"
        assert payload["baseline"] == "prod"
        assert payload["drift_count"] >= 1

        time.sleep(3)
        assert collector.count() == 1, "cooldown failed: duplicate payload"

        log_file = os.path.join(home, "logs", "daemon.log")
        assert os.path.exists(log_file)
        log = open(log_file, encoding="utf-8").read()
        assert "scan cycle done" in log and "alert hook dispatched" in log

        with open(os.path.join(home, "daemon.stop"), "w", encoding="utf-8") as fh:
            fh.write(str(proc.pid))
        proc.wait(timeout=10)
        assert proc.returncode == 0, "daemon exit code %d" % proc.returncode
        assert not os.path.exists(os.path.join(home, "daemon.pid"))

        # daemon status after stop -> stopped (exit 1)
        p = run_cli(["daemon", "status"], env)
        print("STATUS AFTER STOP: exit=%d out=%r" % (p.returncode, p.stdout.strip()))

        print("\nE2E OK: payload received, cooldown respected, log written, graceful stop")
        print("LOG TAIL:")
        print("\n".join(log.splitlines()[-8:]))
        return 0
    finally:
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    sys.exit(main())
