"""End-to-end CLI tests via subprocess (exit codes 0/1/2)."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
import urllib.request

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
PY = sys.executable

WEB_AVAILABLE = importlib.util.find_spec("fastapi") is not None


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
        timeout=60,
    )
    return proc


@pytest.fixture()
def cfg_home(tmp_path):
    """Isolated CFGDRIFT_HOME per test."""
    home = tmp_path / "home"
    home.mkdir()
    yield str(home)


@pytest.fixture()
def project(tmp_path):
    """A tiny config project: conf/app.json + conf/app.yaml."""
    conf = tmp_path / "conf"
    conf.mkdir()
    (conf / "app.json").write_text(
        json.dumps({"server": {"host": "localhost", "port": 8080}, "debug": False}),
        encoding="utf-8",
    )
    (conf / "app.yaml").write_text("mode: prod\n", encoding="utf-8")
    return str(conf)


def test_init(cfg_home):
    proc = run_cli(["init"], env={"CFGDRIFT_HOME": cfg_home})
    assert proc.returncode == 0
    assert os.path.exists(os.path.join(cfg_home, "cfgdrift.db"))


def test_version_and_help():
    proc = run_cli(["--version"])
    assert proc.returncode == 0
    assert "0.5.0" in proc.stdout
    proc = run_cli(["--help"])
    assert proc.returncode == 0
    for cmd in ("init", "scan", "baseline", "diff", "report", "ignore", "serve"):
        assert cmd in proc.stdout


def test_scan_records_history(cfg_home, project):
    run_cli(["init"], env={"CFGDRIFT_HOME": cfg_home})
    proc = run_cli(["scan", project], env={"CFGDRIFT_HOME": cfg_home})
    assert proc.returncode == 0
    assert "recorded scan #1" in proc.stdout


def test_scan_save_baseline_then_diff_drift(cfg_home, project):
    env = {"CFGDRIFT_HOME": cfg_home}
    run_cli(["init"], env=env)

    # baseline v1
    proc = run_cli(
        ["scan", project, "--save-as-baseline", "prod"], env=env
    )
    assert proc.returncode == 0

    # modify config -> drift (exit 1)
    app = os.path.join(project, "app.json")
    with open(app, "w", encoding="utf-8") as fh:
        json.dump({"server": {"host": "changed", "port": 9090}, "debug": True}, fh)
    proc = run_cli(["diff", project, "--baseline", "prod"], env=env)
    assert proc.returncode == 1
    assert "Summary" in proc.stdout
    assert "server.port" in proc.stdout or "server.host" in proc.stdout

    # revert -> no drift (exit 0)
    with open(app, "w", encoding="utf-8") as fh:
        json.dump({"server": {"host": "localhost", "port": 8080}, "debug": False}, fh)
    proc = run_cli(["diff", project, "--baseline", "prod"], env=env)
    assert proc.returncode == 0

    # save baseline again -> version 2
    proc = run_cli(
        ["scan", project, "--save-as-baseline", "prod"], env=env
    )
    assert proc.returncode == 0
    proc = run_cli(["baseline", "show", "prod"], env=env)
    assert "version: 2" in proc.stdout


def test_baseline_rollback(cfg_home, project):
    env = {"CFGDRIFT_HOME": cfg_home}
    run_cli(["init"], env=env)
    run_cli(["scan", project, "--save-as-baseline", "prod"], env=env)
    run_cli(["scan", project, "--save-as-baseline", "prod"], env=env)
    proc = run_cli(["baseline", "list"], env=env)
    assert "v2" in proc.stdout
    proc = run_cli(["baseline", "rollback", "prod"], env=env)
    assert proc.returncode == 0
    assert "version 1" in proc.stdout
    # only one version left -> rollback errors (exit 2)
    proc = run_cli(["baseline", "rollback", "prod"], env=env)
    assert proc.returncode == 2


def test_invalid_args_exit_2(cfg_home):
    proc = run_cli(["diff", "nonexistent"], env={"CFGDRIFT_HOME": cfg_home})
    assert proc.returncode == 2
    proc = run_cli(["baseline", "show", "missing"], env={"CFGDRIFT_HOME": cfg_home})
    assert proc.returncode == 2


def test_ignore_rules_cli(cfg_home, project):
    env = {"CFGDRIFT_HOME": cfg_home}
    run_cli(["init"], env=env)
    proc = run_cli(
        ["ignore", "add", "noise", "server.", "--match-type", "path_prefix"],
        env=env,
    )
    assert proc.returncode == 0
    assert "added" in proc.stdout

    proc = run_cli(["ignore", "list"], env=env)
    assert proc.returncode == 0
    assert "noise" in proc.stdout

    proc = run_cli(
        ["ignore", "add", "fileonly", "debug", "--match-type", "path_exact",
         "--file-pattern", r"\.yaml$", "--change-type", "added"],
        env=env,
    )
    assert proc.returncode == 0

    proc = run_cli(["ignore", "list"], env=env)
    assert proc.returncode == 0
    assert "fileonly" in proc.stdout

    proc = run_cli(["ignore", "remove", "1"], env=env)
    assert proc.returncode == 0
    proc = run_cli(["ignore", "list"], env=env)
    assert "noise" not in proc.stdout


def test_report_json_export(cfg_home, project):
    env = {"CFGDRIFT_HOME": cfg_home}
    run_cli(["init"], env=env)
    run_cli(["scan", project, "--save-as-baseline", "prod"], env=env)
    app = os.path.join(project, "app.json")
    with open(app, "w", encoding="utf-8") as fh:
        json.dump({"server": {"host": "localhost", "port": 9999}, "debug": False}, fh)
    proc = run_cli(["diff", project, "--baseline", "prod"], env=env)
    assert proc.returncode == 1

    out = os.path.join(project, "report.json")
    proc = run_cli(["report", "--json", out], env=env)
    assert proc.returncode == 0
    with open(out, encoding="utf-8") as fh:
        payload = json.load(fh)
    assert payload["code"] == 0
    assert payload["data"]["scan_id"] is not None
    assert payload["data"]["summary"]["total"] >= 1
    item = payload["data"]["items"][0]
    assert set(item) >= {
        "key_path", "change_type", "severity", "file",
        "old_value", "new_value", "old_type", "new_type", "rule_id",
    }


def test_scan_watch_smoke(cfg_home, project):
    env = {"CFGDRIFT_HOME": cfg_home}
    run_cli(["init"], env=env)
    proc = subprocess.Popen(
        [PY, "-m", "cfgdrift.cli", "scan", project, "--watch", "--interval", "1"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={**os.environ, "PYTHONPATH": SRC, "CFGDRIFT_HOME": cfg_home},
    )
    time.sleep(3)
    proc.terminate()
    out, _ = proc.communicate(timeout=10)
    assert "watching" in out


@pytest.mark.skipif(not WEB_AVAILABLE, reason="fastapi/uvicorn not installed")
def test_serve_smoke(cfg_home):
    env = {"CFGDRIFT_HOME": cfg_home}
    run_cli(["init"], env=env)
    proc = subprocess.Popen(
        [PY, "-m", "cfgdrift.cli", "serve", "--port", "8123"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={**os.environ, "PYTHONPATH": SRC, "CFGDRIFT_HOME": cfg_home},
    )
    try:
        deadline = time.time() + 15
        payload = None
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                    "http://127.0.0.1:8123/api/health", timeout=2
                ) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                break
            except Exception:
                time.sleep(0.5)
        assert payload is not None, "server did not respond"
        assert payload["code"] == 0
    finally:
        proc.terminate()
        proc.communicate(timeout=10)


def test_serve_friendly_error_when_web_missing(cfg_home):
    if WEB_AVAILABLE:
        pytest.skip("web extra is installed; error path not applicable")
    proc = run_cli(["serve"], env={"CFGDRIFT_HOME": cfg_home})
    assert proc.returncode == 2
    assert "web extra" in proc.stdout + proc.stderr
