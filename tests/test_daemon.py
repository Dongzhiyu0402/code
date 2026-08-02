"""Unit tests for cfgdrift v0.3.0 daemon module (PID/status/worker loop).

The POSIX double-fork daemonize and the Windows detached-spawn are exercised
through their helpers and the foreground path; a real background daemon is
environment-dependent (sandbox) and is covered by the end-to-end test in
``test_qa_v030.py`` using ``--foreground``.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

from cfgdrift.alert.config import AlertConfig  # noqa: E402
from cfgdrift.alert.dispatcher import AlertDispatcher  # noqa: E402
from cfgdrift.alert.models import AlertRule  # noqa: E402
from cfgdrift.alert.state import AlertStateStore  # noqa: E402
from cfgdrift.core.model import Severity  # noqa: E402
from cfgdrift.daemon.daemon import DaemonManager  # noqa: E402
from cfgdrift.daemon.worker import DaemonWorker, default_home, setup_logging  # noqa: E402
from cfgdrift.scanner.scanner import Scanner  # noqa: E402
from cfgdrift.storage.store import Store  # noqa: E402


@pytest.fixture()
def manager(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return DaemonManager(str(home))


@pytest.fixture()
def store_and_project(tmp_path):
    """A Store with baseline 'prod' over conf/app.json + the conf dir.

    Uses its own subdirectory so it can be combined with the ``manager``
    fixture (which already creates ``tmp_path/home``).
    """
    home = tmp_path / "sp_home"
    home.mkdir()
    store = Store(str(home / "cfgdrift.db"))
    conf = tmp_path / "conf"
    conf.mkdir()
    (conf / "app.json").write_text(
        json.dumps({"server": {"port": 8080}}), encoding="utf-8"
    )
    snapshot = Scanner().scan_path(str(conf))
    store.create_baseline(
        name="prod",
        description="",
        scan_root=str(conf),
        format="auto",
        data=snapshot,
    )
    yield store, conf, str(home)


class TestDefaultHome:
    def test_uses_env(self, monkeypatch):
        monkeypatch.setenv("CFGDRIFT_HOME", "/tmp/custom")
        assert default_home() == "/tmp/custom"

    def test_falls_back_to_user_home(self, monkeypatch):
        monkeypatch.delenv("CFGDRIFT_HOME", raising=False)
        assert default_home() == os.path.join(os.path.expanduser("~"), ".cfgdrift")


class TestPidManagement:
    def test_read_pid_missing(self, manager):
        assert manager._read_pid() is None

    def test_read_pid_roundtrip(self, manager):
        manager._write_pid(4242)
        assert manager._read_pid() == 4242
        content = open(manager.pid_file, encoding="utf-8").read().strip()
        assert content == "4242"

    def test_read_pid_corrupt_raises(self, manager):
        with open(manager.pid_file, "w", encoding="utf-8") as fh:
            fh.write("not-a-pid")
        with pytest.raises(ValueError):
            manager._read_pid()

    def test_clear_pid(self, manager):
        manager._write_pid(1)
        manager._clear_pid()
        assert not os.path.exists(manager.pid_file)

    def test_stop_file_roundtrip(self, manager):
        manager._write_stop_file(777)
        assert open(manager.stop_file, encoding="utf-8").read().strip() == "777"
        manager._clear_stop_file()
        assert not os.path.exists(manager.stop_file)


class TestProcessExists:
    def test_own_process_exists(self, manager):
        assert manager._process_exists(os.getpid()) is True

    def test_bogus_pid_missing(self, manager):
        # A PID far beyond any real range must not exist on either platform.
        assert manager._process_exists(2**30) is False


class TestStatus:
    def test_no_pid_stopped(self, manager, capsys):
        assert manager.status() == 1
        assert "not running" in capsys.readouterr().out

    def test_running(self, manager, capsys):
        manager._write_pid(os.getpid())
        assert manager.status() == 0
        assert "running (pid=%d)" % os.getpid() in capsys.readouterr().out

    def test_stale_pid_cleared(self, manager, capsys):
        manager._write_pid(2**30)
        assert manager.status() == 1
        assert "stale" in capsys.readouterr().out
        assert not os.path.exists(manager.pid_file)

    def test_corrupt_pid_error(self, manager, capsys):
        with open(manager.pid_file, "w", encoding="utf-8") as fh:
            fh.write("junk")
        assert manager.status() == 2
        assert "error" in capsys.readouterr().out.lower()


class TestStartPreflight:
    def test_missing_baseline_exits_2(self, manager, tmp_path):
        conf = tmp_path / "conf"
        conf.mkdir()
        opts = {
            "targets": [str(conf)],
            "baseline": "nope",
            "store": os.path.join(manager.home, "cfgdrift.db"),
        }
        assert manager.start(opts) == 2

    def test_missing_target_exits_2(self, manager, tmp_path):
        opts = {
            "targets": [str(tmp_path / "does-not-exist")],
            "baseline": "prod",
            "store": os.path.join(manager.home, "cfgdrift.db"),
        }
        assert manager.start(opts) == 2

    def test_already_running_exits_2(self, manager, store_and_project):
        store, conf, home = store_and_project
        manager._write_pid(os.getpid())
        opts = {
            "targets": [str(conf)],
            "baseline": "prod",
            "store": store.db_path,
        }
        assert manager.start(opts) == 2


class TestWorkerCommand:
    def test_builds_expected_argv(self, manager):
        opts = {
            "store": "/tmp/x/cfgdrift.db",
            "baseline": "prod",
            "fmt": "auto",
            "interval": 42,
            "log_level": "INFO",
            "targets": ["/a", "/b"],
            "alerts_config": "/tmp/x/alerts.yaml",
            "alert_state": "/tmp/x/alert_state.json",
        }
        cmd = manager._worker_command(opts)
        assert cmd[0] == sys.executable
        assert cmd[1] == "-m"
        assert "cfgdrift.daemon.worker" in cmd
        assert cmd.count("--path") == 2
        assert "--interval" in cmd and "42" in cmd
        assert "--baseline" in cmd and "prod" in cmd
        assert "--alerts-config" in cmd


class TestSetupLogging:
    def test_file_handler_writes_utc_lines(self, tmp_path):
        log_file = str(tmp_path / "logs" / "daemon.log")
        log = setup_logging(log_file, "INFO", foreground=False)
        log.info("hello daemon")
        assert os.path.exists(log_file)
        content = open(log_file, encoding="utf-8").read()
        assert "hello daemon" in content
        assert " INFO [" in content
        assert "cfgdrift.daemon" in content


class TestWorkerLoop:
    def test_cycle_records_scan_and_dispatches_script(self, tmp_path, store_and_project):
        store, conf, home = store_and_project
        # Introduce a drift: port 8080 -> 9090.
        (conf / "app.json").write_text(
            json.dumps({"server": {"port": 9090}}), encoding="utf-8"
        )
        # Script channel that records CFGDRIFT_DRIFT_COUNT + baseline.
        recorder = tmp_path / "recorder.txt"
        script = tmp_path / "notify.py"
        script.write_text(
            "import os, sys\n"
            "with open(sys.argv[1], 'w', encoding='utf-8') as fh:\n"
            "    fh.write(os.environ['CFGDRIFT_BASELINE'] + '|' +\n"
            "             os.environ['CFGDRIFT_DRIFT_COUNT'])\n",
            encoding="utf-8",
        )
        rule = AlertRule(
            name="script-alert",
            type="script",
            severity=Severity.WARN,
            config={"command": sys.executable, "args": [str(script), str(recorder)], "timeout": 30},
        )
        AlertConfig.save(os.path.join(home, "alerts.yaml"), [rule])
        dispatcher = AlertDispatcher(
            [rule],
            AlertStateStore(os.path.join(home, "alert_state.json"), cooldown_seconds=600),
            retry_attempts=1,
        )
        worker = DaemonWorker(
            store_path=store.db_path,
            paths=[str(conf)],
            fmt="auto",
            baseline_name="prod",
            interval=1,
            dispatcher=dispatcher,
            pid_file=os.path.join(home, "daemon.pid"),
            stop_file=os.path.join(home, "daemon.stop"),
            info_file=os.path.join(home, "daemon.info.json"),
            log_file=os.path.join(home, "logs", "daemon.log"),
        )
        worker._cycle(store)

        scans = store.list_scans(limit=5)
        assert any(
            s["mode"] == "daemon" and s["summary"]["total"] >= 1 for s in scans
        )
        assert recorder.exists()
        assert recorder.read_text(encoding="utf-8") == "prod|1"

    def test_worker_stops_via_sentinel(self, store_and_project):
        store, conf, home = store_and_project
        worker = DaemonWorker(
            store_path=store.db_path,
            paths=[str(conf)],
            fmt="auto",
            baseline_name="prod",
            interval=300,
            dispatcher=None,
            pid_file=os.path.join(home, "daemon.pid"),
            stop_file=os.path.join(home, "daemon.stop"),
            info_file=os.path.join(home, "daemon.info.json"),
            log_file=os.path.join(home, "logs", "daemon.log"),
        )
        thread = threading.Thread(target=worker.run, daemon=True)
        thread.start()
        # The worker runs in this process, so its pid == os.getpid().
        time.sleep(0.3)
        with open(os.path.join(home, "daemon.stop"), "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
        thread.join(timeout=10)
        assert not thread.is_alive()
        assert not os.path.exists(os.path.join(home, "daemon.pid"))
        assert not os.path.exists(os.path.join(home, "daemon.stop"))

    def test_worker_writes_and_clears_info_file(self, store_and_project):
        store, conf, home = store_and_project
        worker = DaemonWorker(
            store_path=store.db_path,
            paths=[str(conf)],
            fmt="auto",
            baseline_name="prod",
            interval=300,
            dispatcher=None,
            pid_file=os.path.join(home, "daemon.pid"),
            stop_file=os.path.join(home, "daemon.stop"),
            info_file=os.path.join(home, "daemon.info.json"),
            log_file=os.path.join(home, "logs", "daemon.log"),
        )
        info_file = os.path.join(home, "daemon.info.json")
        worker._write_info_file()
        assert os.path.exists(info_file)
        data = json.loads(open(info_file, encoding="utf-8").read())
        assert data["baseline"] == "prod"
        assert data["interval"] == 300
        assert data["targets"] == [os.path.abspath(str(conf))]
        worker._clear_info_file()
        assert not os.path.exists(info_file)

    def test_read_info(self, manager):
        assert manager.read_info() is None
        manager._write_pid(1)
        with open(manager.info_file, "w", encoding="utf-8") as fh:
            json.dump({"pid": 1, "interval": 5}, fh)
        info = manager.read_info()
        assert info["interval"] == 5
