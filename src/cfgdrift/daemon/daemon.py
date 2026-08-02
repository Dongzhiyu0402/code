"""DaemonManager: start / stop / status + PID management + dual-platform daemonize.

v0.3.0 platform strategy (see ``docs/system_design.md`` appendix B):

- POSIX: classic double-fork daemonize with a readiness pipe — the parent
  ``daemon start`` reports success only after the grandchild has written its
  PID file and opened the Store successfully.
- Windows: ``subprocess.Popen([sys.executable, "-m",
  "cfgdrift.daemon.worker", ...], creationflags=DETACHED_PROCESS |
  CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW)`` — no pywin32; the parent
  polls the PID file (<=15s).

Process-existence checks are platform-aware: POSIX uses ``os.kill(pid, 0)``;
Windows must **never** use ``os.kill(pid, 0)`` (it TerminateProcess-es the
target) — it uses ``ctypes`` ``OpenProcess(SYNCHRONIZE)`` instead.
"""

from __future__ import annotations

import json
import logging
import os
import select
import signal
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

from . import worker as worker_mod
from .worker import default_home

logger = logging.getLogger("cfgdrift.daemon")

_START_TIMEOUT = 15.0
_PID_POLL_INTERVAL = 0.5
_STOP_TIMEOUT_DEFAULT = 30


def _tail(path: str, lines: int = 20) -> str:
    """Return the last ``lines`` lines of a file (best effort)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read()
        return "\n".join(content.splitlines()[-lines:])
    except OSError:
        return ""


def _win_process_exists(pid: int) -> bool:
    """Windows process-existence check via ctypes OpenProcess(SYNCHRONIZE).

    Never use ``os.kill(pid, 0)`` on Windows — it terminates the process.
    """
    import ctypes

    kernel32 = ctypes.windll.kernel32
    SYNCHRONIZE = 0x00100000
    handle = kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
    if handle:
        kernel32.CloseHandle(handle)
        return True
    return False


class DaemonManager:
    """Owns the daemon lifecycle: PID file, sentinel file, process checks."""

    def __init__(self, home: str) -> None:
        self.home = os.path.abspath(home)
        self.pid_file = os.path.join(self.home, "daemon.pid")
        self.stop_file = os.path.join(self.home, "daemon.stop")
        self.info_file = os.path.join(self.home, "daemon.info.json")
        self.log_dir = os.path.join(self.home, "logs")
        self.log_file = os.path.join(self.log_dir, "daemon.log")

    # ------------------------------------------------------------------
    # PID / info helpers
    # ------------------------------------------------------------------

    def _read_pid(self) -> Optional[int]:
        """Return the PID from the pid file, or None when absent.

        Raises ``ValueError`` when the file exists but its content is not a
        pure decimal integer (corrupt -> status exit 2 / start error).
        """
        if not os.path.exists(self.pid_file):
            return None
        try:
            with open(self.pid_file, "r", encoding="utf-8") as fh:
                content = fh.read().strip()
        except OSError as exc:
            raise ValueError(
                "cannot read pid file %s: %s" % (self.pid_file, exc)
            ) from exc
        if not content or not content.isdigit():
            raise ValueError(
                "pid file %s is corrupt (content=%r)"
                % (self.pid_file, content[:40])
            )
        return int(content)

    def _write_pid(self, pid: int) -> None:
        os.makedirs(self.home, exist_ok=True)
        with open(self.pid_file, "w", encoding="utf-8") as fh:
            fh.write(str(int(pid)))

    def _clear_pid(self) -> None:
        try:
            if os.path.exists(self.pid_file):
                os.remove(self.pid_file)
        except OSError as exc:
            logger.warning("failed to clear pid file %s: %s", self.pid_file, exc)

    def _clear_stop_file(self) -> None:
        try:
            if os.path.exists(self.stop_file):
                os.remove(self.stop_file)
        except OSError as exc:
            logger.warning("failed to clear stop file %s: %s", self.stop_file, exc)

    def _write_stop_file(self, pid: int) -> None:
        os.makedirs(self.home, exist_ok=True)
        with open(self.stop_file, "w", encoding="utf-8") as fh:
            fh.write(str(int(pid)))

    def read_info(self) -> Optional[Dict[str, Any]]:
        """Read the worker's runtime info file (interval/targets/start time)."""
        if not os.path.exists(self.info_file):
            return None
        try:
            with open(self.info_file, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    def _clear_info(self) -> None:
        try:
            if os.path.exists(self.info_file):
                os.remove(self.info_file)
        except OSError:
            pass

    def _process_exists(self, pid: int) -> bool:
        """Return True when a process with ``pid`` exists."""
        if sys.platform == "win32":
            return _win_process_exists(pid)
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------

    def status(self) -> int:
        """Print daemon status; returns 0 running / 1 stopped / 2 error."""
        try:
            pid = self._read_pid()
        except ValueError as exc:
            print("error: %s" % exc)
            return 2
        if pid is None:
            print("daemon not running")
            return 1
        if self._process_exists(pid):
            print("daemon running (pid=%d)" % pid)
            return 0
        self._clear_pid()
        self._clear_info()
        print("daemon not running (stale pid cleared)")
        return 1

    # ------------------------------------------------------------------
    # start
    # ------------------------------------------------------------------

    def start(self, opts: Dict[str, Any]) -> int:
        """Start the daemon; returns 0 on success, 2 on any failure."""
        os.makedirs(self.home, exist_ok=True)
        targets = [os.path.abspath(p) for p in opts.get("targets", [])]
        baseline_name = opts.get("baseline", "")
        store_path = opts.get("store", "")

        # Pre-flight validation (before any fork / spawn).
        if not targets:
            print("error: at least one --target path is required", file=sys.stderr)
            return 2
        for target in targets:
            if not os.path.exists(target):
                print("error: target path does not exist: %s" % target, file=sys.stderr)
                return 2
        if not baseline_name:
            print("error: --baseline is required", file=sys.stderr)
            return 2
        try:
            store = worker_mod.Store(store_path)
            try:
                store.get_baseline(baseline_name)
            finally:
                store.close()
        except Exception as exc:  # noqa: BLE001 - report and exit 2
            print("error: baseline %r not found: %s" % (baseline_name, exc), file=sys.stderr)
            return 2

        try:
            pid = self._read_pid()
        except ValueError as exc:
            print("error: %s" % exc, file=sys.stderr)
            return 2
        if pid is not None and self._process_exists(pid):
            print("error: daemon already running (pid=%d)" % pid, file=sys.stderr)
            return 2
        if pid is not None:
            self._clear_pid()
            self._clear_stop_file()
            self._clear_info()

        opts = dict(opts)
        opts["targets"] = targets
        opts["baseline"] = baseline_name
        opts["store"] = store_path

        if sys.platform == "win32":
            return self._spawn_worker_win32(opts)
        return self._daemonize_posix(opts)

    # ------------------------------------------------------------------
    # POSIX daemonize (double fork + readiness pipe)
    # ------------------------------------------------------------------

    def _daemonize_posix(self, opts: Dict[str, Any]) -> int:
        r_fd, w_fd = os.pipe()
        pid = os.fork()
        if pid > 0:
            # Parent: wait for the readiness line (<=15s).
            os.close(w_fd)
            try:
                ready, _, _ = select.select([r_fd], [], [], _START_TIMEOUT)
            except OSError as exc:
                os.close(r_fd)
                print("error: daemon start failed: %s" % exc, file=sys.stderr)
                return 2
            if not ready:
                os.close(r_fd)
                print("error: daemon start timed out", file=sys.stderr)
                return 2
            try:
                data = os.read(r_fd, 4096).decode("utf-8", "replace")
            finally:
                os.close(r_fd)
            data = data.strip()
            if data.startswith("ok"):
                parts = data.split(None, 1)
                pid_text = parts[1] if len(parts) > 1 else "?"
                print("daemon started (pid=%s)" % pid_text)
                return 0
            msg = data[4:] if data.startswith("err:") else data
            print("error: daemon failed: %s" % msg, file=sys.stderr)
            return 2

        # First child: detach from the terminal session.
        os.close(r_fd)
        try:
            os.setsid()
        except OSError:
            pass
        devnull = os.open(os.devnull, os.O_RDWR)
        os.dup2(devnull, 0)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        if devnull > 2:
            os.close(devnull)
        try:
            os.chdir(self.home)
        except OSError:
            pass
        os.umask(0o022)

        # Second fork: the intermediate exits, the grandchild becomes daemon.
        pid2 = os.fork()
        if pid2 > 0:
            os._exit(0)

        # Grandchild: run the worker; report readiness through the pipe.
        opts = dict(opts)
        opts["paths"] = opts.get("targets", [])
        opts["readiness_fd"] = w_fd
        try:
            code = worker_mod.run_with_opts(opts)
        except Exception as exc:  # noqa: BLE001 - any init failure
            try:
                os.write(w_fd, ("err:%s\n" % exc).encode("utf-8"))
            except OSError:
                pass
            code = 2
        os._exit(code)

    # ------------------------------------------------------------------
    # Windows spawn (detached child process)
    # ------------------------------------------------------------------

    def _worker_command(self, opts: Dict[str, Any]) -> List[str]:
        """Build the worker argv for the Windows child process."""
        cmd = [
            sys.executable,
            "-m",
            "cfgdrift.daemon.worker",
            "--home", self.home,
            "--store", opts["store"],
            "--baseline", opts["baseline"],
            "--format", opts.get("fmt", "auto"),
            "--interval", str(opts.get("interval", 300)),
            "--pid-file", self.pid_file,
            "--stop-file", self.stop_file,
            "--info-file", self.info_file,
            "--log-file", opts.get("log_file") or self.log_file,
            "--log-level", opts.get("log_level", "INFO"),
        ]
        alerts_config = opts.get("alerts_config")
        alert_state = opts.get("alert_state")
        if alerts_config:
            cmd += ["--alerts-config", alerts_config]
        if alert_state:
            cmd += ["--alert-state", alert_state]
        for target in opts.get("targets", []):
            cmd += ["--path", target]
        return cmd

    def _spawn_worker_win32(self, opts: Dict[str, Any]) -> int:
        os.makedirs(self.log_dir, exist_ok=True)
        log_handle = open(self.log_file, "a", encoding="utf-8")
        cmd = self._worker_command(opts)
        flags = (
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
        )
        try:
            proc = subprocess.Popen(
                cmd,
                creationflags=flags,
                stdout=log_handle,
                stderr=log_handle,
                stdin=subprocess.DEVNULL,
                close_fds=True,
            )
        except OSError as exc:
            log_handle.close()
            print("error: failed to spawn daemon: %s" % exc, file=sys.stderr)
            return 2

        deadline = time.time() + _START_TIMEOUT
        while time.time() < deadline:
            if os.path.exists(self.pid_file):
                try:
                    pid = self._read_pid()
                except ValueError:
                    pid = None
                print("daemon started (pid=%s)" % (pid if pid is not None else "?"))
                log_handle.close()
                return 0
            if proc.poll() is not None:
                tail = _tail(self.log_file, 20)
                print(
                    "error: daemon failed: %s" % (tail or "worker exited"),
                    file=sys.stderr,
                )
                log_handle.close()
                return 2
            time.sleep(_PID_POLL_INTERVAL)
        log_handle.close()
        print("error: daemon start timed out", file=sys.stderr)
        return 2

    # ------------------------------------------------------------------
    # stop
    # ------------------------------------------------------------------

    def stop(self, timeout: int = _STOP_TIMEOUT_DEFAULT) -> int:
        """Stop the daemon gracefully; idempotent (not running -> 0).

        Writes the sentinel file, then sends SIGTERM on POSIX (or waits for
        the worker's 1s sentinel poll on Windows).  Falls back to
        SIGKILL / ``taskkill /F`` after ``timeout`` seconds.
        """
        try:
            pid = self._read_pid()
        except ValueError as exc:
            print("error: %s" % exc, file=sys.stderr)
            return 2
        if pid is None:
            self._clear_stop_file()
            print("daemon not running")
            return 0
        if not self._process_exists(pid):
            self._clear_pid()
            self._clear_stop_file()
            self._clear_info()
            print("daemon not running (stale pid cleared)")
            return 0

        self._write_stop_file(pid)
        if sys.platform == "win32":
            self._stop_wait_win32(pid, timeout)
        else:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            self._stop_wait_posix(pid, timeout)

        if self._process_exists(pid):
            print("error: failed to stop daemon pid=%d" % pid, file=sys.stderr)
            return 2
        self._clear_pid()
        self._clear_stop_file()
        self._clear_info()
        print("daemon stopped (pid=%d)" % pid)
        return 0

    def _stop_wait_posix(self, pid: int, timeout: int) -> None:
        deadline = time.time() + max(1, int(timeout))
        while time.time() < deadline:
            if not self._process_exists(pid):
                return
            time.sleep(_PID_POLL_INTERVAL)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        # Give the kill a moment to take effect.
        time.sleep(0.5)

    def _stop_wait_win32(self, pid: int, timeout: int) -> None:
        deadline = time.time() + max(1, int(timeout))
        while time.time() < deadline:
            if not self._process_exists(pid):
                return
            time.sleep(_PID_POLL_INTERVAL)
        # Graceful path timed out -> hard kill via taskkill /F.
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        time.sleep(0.5)
