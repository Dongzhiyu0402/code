"""Daemon autostart management (v0.5.0) — three platforms.

:class:`AutostartManager` installs / removes / reports the daemon autostart
on Linux (systemd), macOS (launchd) and Windows (schtasks).  The single
source of truth is ``<home>/autostart.json`` (D3) — it records the effective
configuration and is written/removed together with the platform artifact
(double-write / double-clear).

Decisions implemented here (see ``docs/system_design_v050.md``):

- Q1: Windows uses a scheduled task ``schtasks /Create /TN cfgdrift-daemon
  /SC ONLOGON`` (no third-party dependency); the interval is handled by the
  worker loop itself.
- Q2: ``--user`` is the default (user-level unit under
  ``~/.config/systemd/user`` / ``~/Library/LaunchAgents``); ``--system`` is
  explicit.  ``--dry-run`` prints the artifact + autostart.json without
  touching disk.
- D1: autostart enforces ``interval >= 60`` (``daemon start`` itself still
  accepts any positive integer).
- D2: idempotent — identical parameters -> no-op exit 0; different
  parameters -> require ``--force`` (else exit 2).
- D9: the worker argv comes from :func:`cfgdrift.daemon.worker.build_worker_command`.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..core.parser import validate_format
from ..storage.store import Store
from . import worker as worker_mod

logger = logging.getLogger("cfgdrift.daemon.autostart")

_AUTOSTART_CONFIG_VERSION = 1

_SYSTEMD_UNIT_NAME = "cfgdrift.service"
_LAUNCHD_LABEL = "com.cfgdrift.daemon"
_SCHTASKS_TASK_NAME = "cfgdrift-daemon"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _win_quote(arg: str) -> str:
    """Quote a command-line argument for schtasks /TR (best effort)."""
    if arg and not any(ch.isspace() for ch in arg) and '"' not in arg:
        return arg
    return '"' + arg.replace('"', '\\"') + '"'


class AutostartManager:
    """Owns enable / disable / status of the daemon autostart."""

    def __init__(self, home: str, store_path: Optional[str] = None) -> None:
        self.home = os.path.abspath(home)
        self.store_path = store_path or os.path.join(self.home, "cfgdrift.db")

    # ------------------------------------------------------------------
    # Paths / platform metadata
    # ------------------------------------------------------------------

    @staticmethod
    def autostart_config_path(home: str) -> str:
        """Return the autostart.json path under a cfgdrift home directory."""
        return os.path.join(home, "autostart.json")

    def _unit_metadata(self, scope: str) -> Tuple[Optional[str], str, str]:
        """Return ``(artifact_path, unit_type, unit_name)`` for the platform.

        Windows has no artifact file — the scheduled task *is* the artifact
        (``path=None``).  Unsupported platforms raise ``ValueError``.
        """
        if sys.platform == "linux":
            if scope == "system":
                return (
                    "/etc/systemd/system/" + _SYSTEMD_UNIT_NAME,
                    "systemd",
                    _SYSTEMD_UNIT_NAME,
                )
            return (
                os.path.join(
                    os.path.expanduser("~"),
                    ".config",
                    "systemd",
                    "user",
                    _SYSTEMD_UNIT_NAME,
                ),
                "systemd",
                _SYSTEMD_UNIT_NAME,
            )
        if sys.platform == "darwin":
            if scope == "system":
                return (
                    "/Library/LaunchDaemons/" + _LAUNCHD_LABEL + ".plist",
                    "launchd",
                    _LAUNCHD_LABEL,
                )
            return (
                os.path.join(
                    os.path.expanduser("~"),
                    "Library",
                    "LaunchAgents",
                    _LAUNCHD_LABEL + ".plist",
                ),
                "launchd",
                _LAUNCHD_LABEL,
            )
        if sys.platform == "win32":
            return None, "schtasks", _SCHTASKS_TASK_NAME
        raise ValueError(
            "autostart is not supported on platform %r (supported: "
            "linux, darwin, win32)" % sys.platform
        )

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------

    def _canonical_config(self, opts: Dict[str, Any]) -> Dict[str, Any]:
        """Build the canonical config stored in autostart.json."""
        return {
            "targets": [os.path.abspath(p) for p in opts.get("targets", [])],
            "baseline": opts["baseline"],
            "fmt": opts.get("fmt", "auto"),
            "interval": int(opts.get("interval", 300)),
            "store": os.path.abspath(opts.get("store") or self.store_path),
            "log_file": os.path.abspath(
                opts.get("log_file") or os.path.join(self.home, "logs", "daemon.log")
            ),
            "log_level": opts.get("log_level", "INFO"),
        }

    def _read_config(self) -> Optional[Dict[str, Any]]:
        """Read autostart.json; ``None`` when absent; raises on corruption."""
        path = self.autostart_config_path(self.home)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        if not isinstance(doc, dict):
            raise ValueError("autostart.json must be a mapping")
        return doc

    def _write_config(self, doc: Dict[str, Any]) -> None:
        os.makedirs(self.home, exist_ok=True)
        path = self.autostart_config_path(self.home)
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)

    # ------------------------------------------------------------------
    # Validation (D1)
    # ------------------------------------------------------------------

    def validate(self, opts: Dict[str, Any]) -> None:
        """Validate autostart options before any write.

        - every ``--target`` must exist;
        - ``--baseline`` must exist in the store;
        - ``--interval >= 60`` (autostart-specific guard);
        - ``--format`` must pass :func:`validate_format`.
        """
        targets = opts.get("targets", [])
        if not targets:
            raise ValueError("at least one --target path is required")
        for target in targets:
            if not os.path.exists(target):
                raise ValueError("target path does not exist: %s" % target)
        baseline_name = opts.get("baseline", "")
        if not baseline_name:
            raise ValueError("--baseline is required")
        store = Store(opts.get("store") or self.store_path)
        try:
            store.get_baseline(baseline_name)
        except ValueError as exc:
            raise ValueError("baseline %r not found: %s" % (baseline_name, exc)) from exc
        finally:
            store.close()
        interval = int(opts.get("interval", 300))
        if interval < 60:
            raise ValueError(
                "--interval must be >= 60 for autostart (got %d)" % interval
            )
        validate_format(opts.get("fmt", "auto"))

    # ------------------------------------------------------------------
    # enable
    # ------------------------------------------------------------------

    def enable(self, opts: Dict[str, Any], dry_run: bool = False) -> int:
        """Install the autostart artifact + autostart.json; returns exit code.

        ``--dry-run`` validates and prints the artifact + json content
        without writing anything (zero disk writes).  Idempotency follows D2:
        already enabled with identical config -> no-op exit 0; different
        config -> requires ``--force`` else exit 2.
        """
        try:
            self.validate(opts)
        except ValueError as exc:
            print("error: %s" % exc, file=sys.stderr)
            return 2

        scope = "system" if opts.get("scope") == "system" else "user"
        config = self._canonical_config(opts)

        # -- idempotency (D2) -------------------------------------------
        existing: Optional[Dict[str, Any]] = None
        try:
            existing = self._read_config()
        except (OSError, ValueError) as exc:
            print(
                "error: autostart.json is corrupt (%s); use --force to "
                "overwrite" % exc,
                file=sys.stderr,
            )
            return 2
        if existing and existing.get("enabled"):
            same_config = (
                existing.get("scope") == scope and existing.get("config") == config
            )
            if same_config:
                print("autostart already enabled (no change)")
                return 0
            if not opts.get("force"):
                print(
                    "autostart is already enabled with different parameters "
                    "(use --force to overwrite)",
                    file=sys.stderr,
                )
                return 2

        unit_path, unit_type, unit_name = self._unit_metadata(scope)
        cfg = dict(config)
        cfg["home"] = self.home
        cfg["scope"] = scope
        doc = {
            "version": _AUTOSTART_CONFIG_VERSION,
            "enabled": True,
            "scope": scope,
            "created_at": _utcnow_iso(),
            "config": config,
            "unit": {"type": unit_type, "path": unit_path, "name": unit_name},
        }

        if dry_run:
            print("# --- %s unit (dry run, not written) ---" % unit_type)
            if unit_type == "systemd":
                print(self._render_systemd(cfg))
            elif unit_type == "launchd":
                print(self._render_launchd(cfg))
            else:
                print(self._render_schtasks(cfg))
            print("# --- autostart.json (dry run, not written) ---")
            print(json.dumps(doc, ensure_ascii=False, indent=2))
            return 0

        # -- real apply --------------------------------------------------
        try:
            self._apply(cfg, doc)
        except ValueError as exc:
            print("error: %s" % exc, file=sys.stderr)
            return 2
        print("autostart enabled (scope=%s)" % scope)
        return 0

    # ------------------------------------------------------------------
    # disable
    # ------------------------------------------------------------------

    def disable(self, dry_run: bool = False) -> int:
        """Remove the platform artifact + autostart.json (idempotent).

        An already-disabled state still returns 0 (removal of a missing
        artifact is best-effort).  ``--dry-run`` prints the commands that
        would run without touching disk.
        """
        scope = "user"
        try:
            existing = self._read_config()
            if existing:
                scope = existing.get("scope", "user")
        except (OSError, ValueError) as exc:
            print(
                "error: autostart.json is corrupt (%s); removal proceeds "
                "with default scope" % exc,
                file=sys.stderr,
            )
            scope = "user"

        try:
            unit_path, unit_type, unit_name = self._unit_metadata(scope)
        except ValueError as exc:
            print("error: %s" % exc, file=sys.stderr)
            return 2

        if dry_run:
            self._print_disable_commands(unit_type, unit_path)
            print("rm %s" % self.autostart_config_path(self.home))
            return 0

        try:
            self._remove_artifact(unit_type, unit_path, scope)
        except ValueError as exc:
            print("error: %s" % exc, file=sys.stderr)
            return 2
        # Clear the source of truth (idempotent — missing file is a no-op).
        try:
            path = self.autostart_config_path(self.home)
            if os.path.exists(path):
                os.remove(path)
        except OSError as exc:
            print("error: failed to remove autostart.json: %s" % exc, file=sys.stderr)
            return 2
        print("autostart disabled")
        return 0

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------

    def status_dict(self) -> Dict[str, Any]:
        """Return autostart status as a dict without printing.

        ``{"enabled", "scope", "config", "unit", "artifact_present",
        "error"}``.  Artifact presence is best-effort (filesystem / schtasks
        query); a missing artifact while json says enabled yields ``error``.
        """
        try:
            doc = self._read_config()
        except (OSError, ValueError) as exc:
            return {
                "enabled": False,
                "scope": None,
                "config": None,
                "unit": None,
                "artifact_present": False,
                "error": "autostart.json is corrupt: %s" % exc,
            }
        if doc is None:
            return {
                "enabled": False,
                "scope": None,
                "config": None,
                "unit": None,
                "artifact_present": False,
                "error": None,
            }
        enabled = bool(doc.get("enabled", False))
        artifact = self._artifact_present(doc)
        error = None
        if enabled and not artifact:
            error = (
                "autostart.json says enabled but the platform artifact is "
                "missing or unreachable"
            )
        return {
            "enabled": enabled,
            "scope": doc.get("scope"),
            "config": doc.get("config"),
            "unit": doc.get("unit"),
            "artifact_present": artifact,
            "error": error,
        }

    def status(self) -> int:
        """Print autostart status; returns 0 enabled / 1 disabled / 2 error."""
        d = self.status_dict()
        if d["error"]:
            print("error: %s" % d["error"])
        if d["enabled"]:
            unit = d["unit"] or {}
            print("autostart: enabled")
            print("scope: %s" % (d["scope"] or "user"))
            cfg = d["config"] or {}
            print(
                "config: targets=%s baseline=%s interval=%s fmt=%s"
                % (
                    ",".join(cfg.get("targets", [])),
                    cfg.get("baseline", ""),
                    cfg.get("interval", ""),
                    cfg.get("fmt", ""),
                )
            )
            print("unit_type: %s" % unit.get("type", ""))
            print("unit_path: %s" % (unit.get("path") or "(scheduled task)"))
            print("artifact_present: %s" % ("yes" if d["artifact_present"] else "no"))
            return 2 if d["error"] else 0
        if d["error"]:
            return 2
        print("autostart: disabled")
        return 1

    # ------------------------------------------------------------------
    # Renderers
    # ------------------------------------------------------------------

    def _worker_argv(self, cfg: Dict[str, Any]) -> List[str]:
        """Build the worker argv shared by all three platform renderers (D9)."""
        opts: Dict[str, Any] = {
            "store": cfg["store"],
            "baseline": cfg["baseline"],
            "fmt": cfg["fmt"],
            "interval": cfg["interval"],
            "targets": cfg["targets"],
            "log_file": cfg["log_file"],
            "log_level": cfg["log_level"],
            "alerts_config": os.path.join(cfg["home"], "alerts.yaml"),
            "alert_state": os.path.join(cfg["home"], "alert_state.json"),
        }
        return worker_mod.build_worker_command(cfg["home"], opts)

    def _render_systemd(self, cfg: Dict[str, Any]) -> str:
        """Render a systemd unit file (``--user`` default / ``--system``)."""
        argv = self._worker_argv(cfg)
        exec_start = " ".join(argv)
        wanted_by = "multi-user.target" if cfg.get("scope") == "system" else "default.target"
        return (
            "[Unit]\n"
            "Description=cfgdrift drift daemon\n"
            "After=network.target\n"
            "\n"
            "[Service]\n"
            "Type=simple\n"
            "ExecStart=%s\n"
            "Restart=on-failure\n"
            "RestartSec=10\n"
            "\n"
            "[Install]\n"
            "WantedBy=%s\n" % (exec_start, wanted_by)
        )

    def _render_launchd(self, cfg: Dict[str, Any]) -> str:
        """Render a launchd plist (``Label=com.cfgdrift.daemon``)."""
        argv = self._worker_argv(cfg)
        args_xml = "\n".join("    <string>%s</string>" % _xml_escape(a) for a in argv)
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            "<plist version=\"1.0\">\n"
            "<dict>\n"
            "    <key>Label</key>\n"
            "    <string>%s</string>\n"
            "    <key>ProgramArguments</key>\n"
            "    <array>\n%s\n"
            "    </array>\n"
            "    <key>RunAtLoad</key>\n"
            "    <true/>\n"
            "    <key>KeepAlive</key>\n"
            "    <true/>\n"
            "    <key>StandardOutPath</key>\n"
            "    <string>%s</string>\n"
            "    <key>StandardErrorPath</key>\n"
            "    <string>%s</string>\n"
            "</dict>\n"
            "</plist>\n" % (_LAUNCHD_LABEL, args_xml, _xml_escape(cfg["log_file"]),
                            _xml_escape(cfg["log_file"]))
        )

    def _render_schtasks(self, cfg: Dict[str, Any]) -> str:
        """Render the schtasks /Create command preview (Q1)."""
        argv = self._worker_argv(cfg)
        tr = " ".join(_win_quote(a) for a in argv)
        return (
            'schtasks /Create /TN "%s" /TR "%s" /SC ONLOGON /RL LIMITED /F'
            % (_SCHTASKS_TASK_NAME, tr)
        )

    # ------------------------------------------------------------------
    # Apply / remove (real side effects)
    # ------------------------------------------------------------------

    def _apply(self, cfg: Dict[str, Any], doc: Dict[str, Any]) -> None:
        """Write autostart.json + the platform artifact.

        The artifact is created first so a failing artifact never leaves a
        json that claims enabled (D3 double-write consistency).
        """
        unit_path, unit_type, _ = self._unit_metadata(doc["scope"])
        if unit_type == "systemd":
            self._write_systemd(cfg, unit_path, doc["scope"])
        elif unit_type == "launchd":
            self._write_launchd(cfg, unit_path)
        else:  # schtasks
            self._create_schtasks(cfg)
        self._write_config(doc)

    def _write_systemd(self, cfg: Dict[str, Any], unit_path: str, scope: str) -> None:
        content = self._render_systemd(cfg)
        try:
            os.makedirs(os.path.dirname(unit_path), exist_ok=True)
            with open(unit_path, "w", encoding="utf-8") as fh:
                fh.write(content)
        except OSError as exc:
            raise ValueError(
                "failed to write systemd unit %s: %s (system units require "
                "root; use --user)" % (unit_path, exc)
            ) from exc
        # Best-effort enable (failure is only a warning — the unit file is
        # the durable artifact).
        try:
            if scope == "user":
                subprocess.run(
                    ["systemctl", "--user", "daemon-reload"],
                    capture_output=True,
                    timeout=30,
                )
                subprocess.run(
                    ["systemctl", "--user", "enable", _SYSTEMD_UNIT_NAME],
                    capture_output=True,
                    timeout=30,
                )
            else:
                subprocess.run(
                    ["systemctl", "daemon-reload"], capture_output=True, timeout=30
                )
                subprocess.run(
                    ["systemctl", "enable", _SYSTEMD_UNIT_NAME],
                    capture_output=True,
                    timeout=30,
                )
        except Exception as exc:  # noqa: BLE001 - best-effort enable
            logger.warning("systemctl enable failed (best-effort): %s", exc)

    def _write_launchd(self, cfg: Dict[str, Any], plist_path: str) -> None:
        content = self._render_launchd(cfg)
        try:
            os.makedirs(os.path.dirname(plist_path), exist_ok=True)
            with open(plist_path, "w", encoding="utf-8") as fh:
                fh.write(content)
        except OSError as exc:
            raise ValueError(
                "failed to write launchd plist %s: %s (system daemons "
                "require root; use --user)" % (plist_path, exc)
            ) from exc
        try:
            subprocess.run(
                ["launchctl", "load", "-w", plist_path],
                capture_output=True,
                timeout=30,
            )
        except Exception as exc:  # noqa: BLE001 - best-effort load
            logger.warning("launchctl load failed (best-effort): %s", exc)

    def _create_schtasks(self, cfg: Dict[str, Any]) -> None:
        argv = self._worker_argv(cfg)
        tr = " ".join(_win_quote(a) for a in argv)
        cmd = [
            "schtasks",
            "/Create",
            "/TN",
            _SCHTASKS_TASK_NAME,
            "/TR",
            tr,
            "/SC",
            "ONLOGON",
            "/RL",
            "LIMITED",
            "/F",
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30
            )
        except Exception as exc:  # noqa: BLE001 - surface as a hard error
            raise ValueError("failed to create scheduled task: %s" % exc) from exc
        if proc.returncode != 0:
            detail = (proc.stderr or "").strip() or (proc.stdout or "").strip()
            raise ValueError(
                "schtasks /Create failed (%d): %s" % (proc.returncode, detail)
            )

    def _print_disable_commands(self, unit_type: str, unit_path: Optional[str]) -> None:
        """Print the commands ``disable --dry-run`` would execute."""
        if unit_type == "systemd":
            print("systemctl --user disable %s" % _SYSTEMD_UNIT_NAME)
            print("systemctl --user daemon-reload")
            if unit_path:
                print("rm %s" % unit_path)
        elif unit_type == "launchd":
            if unit_path:
                print("launchctl unload %s" % unit_path)
                print("rm %s" % unit_path)
        else:
            print('schtasks /Delete /TN "%s" /F' % _SCHTASKS_TASK_NAME)

    def _remove_artifact(self, unit_type: str, unit_path: Optional[str], scope: str) -> None:
        """Remove the platform artifact (best-effort, idempotent)."""
        if unit_type == "systemd":
            try:
                if scope == "user":
                    subprocess.run(
                        ["systemctl", "--user", "disable", _SYSTEMD_UNIT_NAME],
                        capture_output=True,
                        timeout=30,
                    )
                    subprocess.run(
                        ["systemctl", "--user", "daemon-reload"],
                        capture_output=True,
                        timeout=30,
                    )
                else:
                    subprocess.run(
                        ["systemctl", "disable", _SYSTEMD_UNIT_NAME],
                        capture_output=True,
                        timeout=30,
                    )
            except Exception as exc:  # noqa: BLE001 - best-effort
                logger.warning("systemctl disable failed (best-effort): %s", exc)
            if unit_path:
                try:
                    if os.path.exists(unit_path):
                        os.remove(unit_path)
                except OSError as exc:
                    logger.warning("failed to remove unit file %s: %s", unit_path, exc)
        elif unit_type == "launchd":
            if unit_path:
                try:
                    subprocess.run(
                        ["launchctl", "unload", unit_path],
                        capture_output=True,
                        timeout=30,
                    )
                except Exception as exc:  # noqa: BLE001 - best-effort
                    logger.warning("launchctl unload failed (best-effort): %s", exc)
                try:
                    if os.path.exists(unit_path):
                        os.remove(unit_path)
                except OSError as exc:
                    logger.warning("failed to remove plist %s: %s", unit_path, exc)
        else:  # schtasks
            try:
                subprocess.run(
                    ["schtasks", "/Delete", "/TN", _SCHTASKS_TASK_NAME, "/F"],
                    capture_output=True,
                    timeout=30,
                )
            except Exception as exc:  # noqa: BLE001 - best-effort
                logger.warning("schtasks /Delete failed (best-effort): %s", exc)

    # ------------------------------------------------------------------
    # Artifact presence (best-effort)
    # ------------------------------------------------------------------

    def _artifact_present(self, doc: Dict[str, Any]) -> bool:
        """Best-effort check of platform artifact existence (status exit)."""
        unit = doc.get("unit") or {}
        unit_type = unit.get("type")
        if unit_type in ("systemd", "launchd"):
            path = unit.get("path")
            return bool(path) and os.path.exists(path)
        if unit_type == "schtasks":
            try:
                proc = subprocess.run(
                    ["schtasks", "/Query", "/TN", _SCHTASKS_TASK_NAME],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                return proc.returncode == 0
            except Exception:  # noqa: BLE001 - best-effort
                return False
        return False


def _xml_escape(text: str) -> str:
    """Escape a string for embedding in an XML plist."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
