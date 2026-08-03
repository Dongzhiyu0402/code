"""Engineer unit tests for cfgdrift v0.5.0 — daemon autostart (T05).

Covers the ``AutostartManager`` on all three platforms.  The Linux/macOS
generators are asserted as *text* only (no real systemctl / launchctl on this
Windows sandbox); the Windows scheduled-task path is exercised through
``--dry-run`` so no real schtasks registration pollutes the system.

Key behaviors verified: platform renderers, validation (target exists /
baseline exists / interval >= 60 / format valid), idempotency (same config
no-op exit 0, different config requires --force), dry-run zero-write,
status exit codes 0/1/2, disable idempotency, and the shared worker command
(D9).
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cfgdrift.daemon.autostart import AutostartManager  # noqa: E402
from cfgdrift.daemon.daemon import DaemonManager  # noqa: E402
from cfgdrift.daemon.worker import build_worker_command  # noqa: E402
from cfgdrift.storage.store import Store  # noqa: E402


@pytest.fixture()
def store_with_baseline(tmp_path):
    """A Store with one baseline named 'prod' (scan root = tmp_path)."""
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


def _opts(home, store_path, **overrides):
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


def _cfg_for(home, store_path, **overrides):
    """Canonical config dict used by the idempotency tests."""
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


# ---------------------------------------------------------------------------
# Renderers (text assertions, platform-independent)
# ---------------------------------------------------------------------------

class TestRenderers:
    def _manager_cfg(self, tmp_path):
        home = str(tmp_path / "home")
        os.makedirs(home, exist_ok=True)
        return {
            "home": home,
            "store": os.path.join(home, "cfgdrift.db"),
            "baseline": "prod",
            "fmt": "auto",
            "interval": 300,
            "targets": ["/etc/nginx", "/etc/app"],
            "log_file": os.path.join(home, "logs", "daemon.log"),
            "log_level": "INFO",
            "scope": "user",
        }

    def test_render_systemd_user(self, tmp_path):
        m = AutostartManager(str(tmp_path))
        text = m._render_systemd(self._manager_cfg(tmp_path))
        assert "[Unit]" in text
        assert "Description=cfgdrift drift daemon" in text
        assert "After=network.target" in text
        assert "[Service]" in text
        assert "Type=simple" in text
        assert "-m cfgdrift.daemon.worker" in text
        assert "--home" in text and "--store" in text
        assert "--baseline prod" in text
        assert "--interval 300" in text
        assert "--path /etc/nginx" in text and "--path /etc/app" in text
        assert "--alerts-config" in text and "--alert-state" in text
        assert "--log-file" in text and "--log-level" in text
        assert "Restart=on-failure" in text
        assert "RestartSec=10" in text
        assert "WantedBy=default.target" in text  # --user default

    def test_render_systemd_system(self, tmp_path):
        m = AutostartManager(str(tmp_path))
        cfg = self._manager_cfg(tmp_path)
        cfg["scope"] = "system"
        text = m._render_systemd(cfg)
        assert "WantedBy=multi-user.target" in text

    def test_render_launchd(self, tmp_path):
        m = AutostartManager(str(tmp_path))
        text = m._render_launchd(self._manager_cfg(tmp_path))
        assert "<plist" in text
        assert "<key>Label</key>" in text
        assert "<string>com.cfgdrift.daemon</string>" in text
        assert "<key>ProgramArguments</key>" in text
        assert "-m" in text and "cfgdrift.daemon.worker" in text
        assert "<key>RunAtLoad</key>" in text and "<true/>" in text
        assert "<key>KeepAlive</key>" in text
        assert "<key>StandardOutPath</key>" in text
        assert "daemon.log" in text

    def test_render_schtasks(self, tmp_path):
        m = AutostartManager(str(tmp_path))
        text = m._render_schtasks(self._manager_cfg(tmp_path))
        assert text.startswith("schtasks /Create")
        assert '/TN "cfgdrift-daemon"' in text
        assert "/SC ONLOGON" in text
        assert "/RL LIMITED" in text
        assert "/F" in text
        assert "-m cfgdrift.daemon.worker" in text
        assert "--path /etc/nginx" in text


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TestValidation:
    def test_interval_lt_60_rejected(self, tmp_path, store_with_baseline):
        home = str(tmp_path / "home")
        os.makedirs(home, exist_ok=True)
        m = AutostartManager(home, store_with_baseline.db_path)
        with pytest.raises(ValueError) as exc:
            m.validate(_opts(home, store_with_baseline.db_path, interval=30))
        assert ">= 60" in str(exc.value)

    def test_missing_target_rejected(self, tmp_path, store_with_baseline):
        home = str(tmp_path / "home")
        os.makedirs(home, exist_ok=True)
        m = AutostartManager(home, store_with_baseline.db_path)
        with pytest.raises(ValueError) as exc:
            m.validate(
                _opts(home, store_with_baseline.db_path, targets=["/no/such/path"])
            )
        assert "target path does not exist" in str(exc.value)

    def test_missing_baseline_rejected(self, tmp_path, store_with_baseline):
        home = str(tmp_path / "home")
        os.makedirs(home, exist_ok=True)
        m = AutostartManager(home, store_with_baseline.db_path)
        with pytest.raises(ValueError) as exc:
            m.validate(_opts(home, store_with_baseline.db_path, baseline="nope"))
        assert "not found" in str(exc.value)

    def test_invalid_format_rejected(self, tmp_path, store_with_baseline):
        home = str(tmp_path / "home")
        os.makedirs(home, exist_ok=True)
        m = AutostartManager(home, store_with_baseline.db_path)
        with pytest.raises(ValueError) as exc:
            m.validate(_opts(home, store_with_baseline.db_path, fmt="bogus"))
        assert "invalid format" in str(exc.value)


# ---------------------------------------------------------------------------
# enable / dry-run / idempotency / status / disable
# ---------------------------------------------------------------------------

class TestEnableDisableStatus:
    def test_enable_dry_run_prints_and_writes_nothing(self, tmp_path, store_with_baseline, capsys):
        home = str(tmp_path / "home")
        os.makedirs(home, exist_ok=True)
        m = AutostartManager(home, store_with_baseline.db_path)
        code = m.enable(_opts(home, store_with_baseline.db_path), dry_run=True)
        assert code == 0
        out = capsys.readouterr().out
        assert "dry run" in out
        assert "autostart.json" in out
        assert os.path.exists(AutostartManager.autostart_config_path(home)) is False

    def test_enable_same_config_is_noop(self, tmp_path, store_with_baseline, capsys):
        home = str(tmp_path / "home")
        os.makedirs(home, exist_ok=True)
        m = AutostartManager(home, store_with_baseline.db_path)
        cfg = _cfg_for(home, store_with_baseline.db_path)
        doc = {
            "version": 1,
            "enabled": True,
            "scope": "user",
            "created_at": "2026-01-01T00:00:00+00:00",
            "config": cfg,
            "unit": {"type": "schtasks", "path": None, "name": "cfgdrift-daemon"},
        }
        m._write_config(doc)
        code = m.enable(_opts(home, store_with_baseline.db_path), dry_run=True)
        assert code == 0
        assert "no change" in capsys.readouterr().out

    def test_enable_different_config_requires_force(self, tmp_path, store_with_baseline, capsys):
        home = str(tmp_path / "home")
        os.makedirs(home, exist_ok=True)
        m = AutostartManager(home, store_with_baseline.db_path)
        m._write_config(
            {
                "version": 1,
                "enabled": True,
                "scope": "user",
                "created_at": "2026-01-01T00:00:00+00:00",
                "config": _cfg_for(home, store_with_baseline.db_path, interval=600),
                "unit": {"type": "schtasks", "path": None, "name": "cfgdrift-daemon"},
            }
        )
        # Without --force -> exit 2 with a readable message.
        code = m.enable(_opts(home, store_with_baseline.db_path), dry_run=True)
        assert code == 2
        assert "different parameters" in capsys.readouterr().err

        # With --force the write proceeds (dry-run: prints, no disk writes).
        code = m.enable(
            _opts(home, store_with_baseline.db_path, force=True), dry_run=True
        )
        assert code == 0

    def test_enable_force_real_write_writes_json(self, tmp_path, store_with_baseline, monkeypatch):
        home = str(tmp_path / "home")
        os.makedirs(home, exist_ok=True)
        m = AutostartManager(home, store_with_baseline.db_path)
        # Keep the platform artifact a no-op (do not touch the real OS) but
        # let _apply run so autostart.json is written (double-write logic).
        monkeypatch.setattr(
            m, "_unit_metadata", lambda scope: (None, "schtasks", "cfgdrift-daemon")
        )
        monkeypatch.setattr(m, "_create_schtasks", lambda cfg: None)
        code = m.enable(_opts(home, store_with_baseline.db_path))
        assert code == 0
        path = AutostartManager.autostart_config_path(home)
        assert os.path.exists(path)
        doc = json.load(open(path, encoding="utf-8"))
        assert doc["enabled"] is True
        assert doc["scope"] == "user"
        assert doc["config"]["interval"] == 300

    def test_status_disabled_when_no_file(self, tmp_path, capsys):
        home = str(tmp_path / "home")
        os.makedirs(home, exist_ok=True)
        m = AutostartManager(home)
        assert m.status() == 1
        assert "disabled" in capsys.readouterr().out

    def test_status_enabled_when_artifact_present(self, tmp_path, monkeypatch):
        home = str(tmp_path / "home")
        os.makedirs(home, exist_ok=True)
        m = AutostartManager(home)
        m._write_config(
            {
                "version": 1,
                "enabled": True,
                "scope": "user",
                "created_at": "2026-01-01T00:00:00+00:00",
                "config": _cfg_for(home, str(tmp_path / "cfgdrift.db")),
                "unit": {"type": "schtasks", "path": None, "name": "cfgdrift-daemon"},
            }
        )
        monkeypatch.setattr(m, "_artifact_present", lambda doc: True)
        assert m.status() == 0

    def test_status_error_when_artifact_missing(self, tmp_path, monkeypatch):
        home = str(tmp_path / "home")
        os.makedirs(home, exist_ok=True)
        m = AutostartManager(home)
        m._write_config(
            {
                "version": 1,
                "enabled": True,
                "scope": "user",
                "created_at": "2026-01-01T00:00:00+00:00",
                "config": _cfg_for(home, str(tmp_path / "cfgdrift.db")),
                "unit": {"type": "schtasks", "path": None, "name": "cfgdrift-daemon"},
            }
        )
        monkeypatch.setattr(m, "_artifact_present", lambda doc: False)
        assert m.status() == 2

    def test_status_error_on_corrupt_json(self, tmp_path):
        home = str(tmp_path / "home")
        os.makedirs(home, exist_ok=True)
        path = AutostartManager.autostart_config_path(home)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        m = AutostartManager(home)
        assert m.status() == 2

    def test_disable_dry_run_prints_commands(self, tmp_path, capsys):
        home = str(tmp_path / "home")
        os.makedirs(home, exist_ok=True)
        m = AutostartManager(home)
        assert m.disable(dry_run=True) == 0
        out = capsys.readouterr().out
        assert "rm" in out or "schtasks /Delete" in out
        assert os.path.exists(AutostartManager.autostart_config_path(home)) is False

    def test_disable_idempotent_and_clears_json(self, tmp_path, monkeypatch):
        home = str(tmp_path / "home")
        os.makedirs(home, exist_ok=True)
        m = AutostartManager(home)
        m._write_config(
            {
                "version": 1,
                "enabled": True,
                "scope": "user",
                "created_at": "2026-01-01T00:00:00+00:00",
                "config": _cfg_for(home, str(tmp_path / "cfgdrift.db")),
                "unit": {"type": "schtasks", "path": None, "name": "cfgdrift-daemon"},
            }
        )
        monkeypatch.setattr(m, "_remove_artifact", lambda *a, **k: None)
        assert m.disable() == 0
        assert os.path.exists(AutostartManager.autostart_config_path(home)) is False
        # Second disable (nothing to remove) is still exit 0 (idempotent).
        assert m.disable() == 0

    def test_unsupported_platform_raises(self, tmp_path, monkeypatch):
        home = str(tmp_path / "home")
        os.makedirs(home, exist_ok=True)
        m = AutostartManager(home)
        monkeypatch.setattr(sys, "platform", "cygwin")
        with pytest.raises(ValueError):
            m._unit_metadata("user")


# ---------------------------------------------------------------------------
# Shared worker command (D9)
# ---------------------------------------------------------------------------

class TestWorkerCommandShared:
    def test_autostart_argv_matches_daemon_manager(self, tmp_path):
        home = str(tmp_path / "home")
        os.makedirs(home, exist_ok=True)
        store_path = str(tmp_path / "cfgdrift.db")
        m = AutostartManager(home, store_path)
        cfg = {
            "home": home,
            "store": store_path,
            "baseline": "prod",
            "fmt": "auto",
            "interval": 300,
            "targets": ["/a"],
            "log_file": os.path.join(home, "logs", "daemon.log"),
            "log_level": "INFO",
        }
        autostart_argv = m._worker_argv(cfg)
        # Autostart units include alerts config/state and omit pid files.
        assert autostart_argv[0] == sys.executable
        assert "--alerts-config" in autostart_argv
        assert "--alert-state" in autostart_argv
        assert "--pid-file" not in autostart_argv
        assert autostart_argv.count("--path") == 1

    def test_daemon_manager_delegates_to_build_worker_command(self, tmp_path):
        home = str(tmp_path / "home")
        os.makedirs(home, exist_ok=True)
        store_path = str(tmp_path / "cfgdrift.db")
        manager = DaemonManager(home)
        opts = {
            "store": store_path,
            "baseline": "prod",
            "fmt": "auto",
            "interval": 42,
            "log_level": "INFO",
            "targets": ["/a", "/b"],
            "alerts_config": os.path.join(home, "alerts.yaml"),
            "alert_state": os.path.join(home, "alert_state.json"),
        }
        delegated = manager._worker_command(opts)
        merged = dict(opts)
        merged["pid_file"] = manager.pid_file
        merged["stop_file"] = manager.stop_file
        merged["info_file"] = manager.info_file
        merged["log_file"] = manager.log_file
        expected = build_worker_command(home, merged)
        assert delegated == expected
        assert "--pid-file" in delegated and "--stop-file" in delegated
        assert delegated.count("--path") == 2
