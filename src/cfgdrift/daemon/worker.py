"""Daemon worker: the periodic scan -> diff -> store -> dispatch loop.

``DaemonWorker`` reuses the existing engine (``Scanner.scan_path``,
``SemanticDiffer.diff_snapshot``, ``Store``) and the alert dispatcher.  It
does **not** reuse ``Scanner.watch`` — the daemon builds its own loop so it
can respond to stop requests within 1 second (sliced sleeps) and write
structured logs.

Module-level :func:`main` is the entry point for the Windows child process
(``python -m cfgdrift.daemon.worker``) and for ``daemon start --foreground``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional

from .. import __version__
from ..alert.config import AlertConfig
from ..alert.dispatcher import AlertDispatcher
from ..alert.state import AlertStateStore
from ..core.differ import SemanticDiffer
from ..core.constraints import violations_from_items
from ..core.masker import SensitiveMasker, masking_config_path
from ..core.model import Constraint, Report, ScanSummary
from ..rules.constraints import (
    resolve as resolve_constraints,
)
from ..rules.severity import SeverityConfig, default_path as severity_config_path
from ..scanner.scanner import Scanner
from ..storage.store import Store, utcnow_iso

logger = logging.getLogger("cfgdrift.daemon")

_DEFAULT_INTERVAL = 300
_SLEEP_SLICE = 1.0


def default_home() -> str:
    """Return the cfgdrift data directory (CFGDRIFT_HOME or ~/.cfgdrift)."""
    return os.environ.get("CFGDRIFT_HOME") or os.path.join(
        os.path.expanduser("~"), ".cfgdrift"
    )


def setup_logging(
    log_file: Optional[str],
    log_level: str = "INFO",
    foreground: bool = False,
) -> logging.Logger:
    """Configure the ``cfgdrift.daemon`` logger.

    File handler: ``RotatingFileHandler`` 1MB x 3 backups.  In foreground
    mode (or when no log file is given) a console handler is added too.
    Line format: ``<ISO-8601 UTC> <LEVEL> [<name>] <msg>``.
    """
    root = logging.getLogger("cfgdrift")
    root.setLevel(log_level.upper())
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    formatter.converter = time.gmtime

    # Avoid duplicate handlers across repeated setup_logging calls.
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    if log_file:
        parent = os.path.dirname(log_file)
        if parent:
            os.makedirs(parent, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    if foreground or not log_file:
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        root.addHandler(console)

    root.propagate = False
    return logger


class DaemonWorker:
    """Periodic scanner loop for the cfgdrift daemon."""

    def __init__(
        self,
        store_path: str,
        paths: List[str],
        fmt: str,
        baseline_name: str,
        interval: int,
        dispatcher: Optional[AlertDispatcher],
        pid_file: Optional[str] = None,
        stop_file: Optional[str] = None,
        info_file: Optional[str] = None,
        log_file: Optional[str] = None,
        log: Optional[logging.Logger] = None,
        masker: Optional[SensitiveMasker] = None,
        severity_rules: Optional[List[Any]] = None,
        home: Optional[str] = None,  # v0.6.0: constraints.yaml location
        builtin_enabled: bool = True,  # v0.6.0
        constraint_paths: Optional[List[str]] = None,  # v0.6.0
    ) -> None:
        self.store_path = os.path.abspath(store_path)
        self.paths = [os.path.abspath(p) for p in paths]
        self.fmt = fmt
        self.baseline_name = baseline_name
        self.interval = max(1, int(interval))
        self.dispatcher = dispatcher
        self.pid_file = pid_file
        self.stop_file = stop_file
        self.info_file = info_file
        self.log_file = log_file
        self.log = log or logger
        self.masker = masker
        self.severity_rules = list(severity_rules or [])
        self.home = home or default_home()
        self.builtin_enabled = bool(builtin_enabled)
        self.constraint_paths = list(constraint_paths or [])
        self._constraints: Optional[List[Constraint]] = None
        self._stop_event = threading.Event()
        self._scanner = Scanner()
        self._differ = SemanticDiffer()

    # -- public API -------------------------------------------------------

    def _load_constraints(self) -> List[Constraint]:
        """Resolve the effective constraints (re-read every cycle, D9).

        Delegates to :func:`cfgdrift.rules.constraints.resolve` (D8): built-in
        library (if enabled) + <home>/constraints.yaml (if present) + each
        extra --constraints file.  A corrupt/missing user file raises so the
        failure is logged and the daemon keeps running (previous cycle's
        constraints stay effective in memory).
        """
        return resolve_constraints(
            self.home,
            extra_paths=list(self.constraint_paths),
            builtin_enabled=self.builtin_enabled,
        )

    def run(self, readiness_fd: Optional[int] = None) -> int:
        """Run the scan loop until a stop is requested; returns exit code.

        When ``readiness_fd`` is provided (POSIX daemonize), the worker
        writes ``ok <pid>`` after successful init or ``err:<msg>`` on failure
        so the parent ``daemon start`` can report success synchronously.
        """
        self.log.info("daemon worker started (pid=%d)", os.getpid())
        self._write_pid_file()

        store: Optional[Store] = None
        try:
            store = Store(self.store_path)
        except Exception as exc:  # noqa: BLE001 - init must be reported
            self.log.error("daemon init failed: %s", exc)
            if readiness_fd is not None:
                self._notify_readiness(readiness_fd, ok=False, err=str(exc))
            self._clear_pid_file()
            return 2

        # v0.4.0: wire the alert-event sink + masker into the dispatcher
        # (the dispatcher is built before the Store exists, so the sink is
        # assigned here as a property).
        if self.dispatcher is not None:
            self.dispatcher.event_sink = store
            if self.dispatcher.masker is None:
                self.dispatcher.masker = self.masker

        self._write_info_file()
        if readiness_fd is not None:
            self._notify_readiness(readiness_fd, ok=True, pid=os.getpid())

        try:
            while not self._stop_requested():
                self._cycle(store)
                self._sleep_until_next()
        finally:
            if store is not None:
                store.close()
            self._clear_pid_file()
            self._clear_stop_file()
            self._clear_info_file()
            self.log.info("daemon worker stopped")
        return 0

    def _cycle(self, store: Store) -> int:
        """One scan cycle; returns drift count (or -1 on error)."""
        self.log.info(
            "scan cycle start (baseline=%s interval=%ds)",
            self.baseline_name,
            self.interval,
        )
        try:
            # D9: reload constraints every cycle so `constraint add` takes
            # effect on the next scan (severity_rules stay startup-loaded).
            self._constraints = self._load_constraints()
            for path in self.paths:
                self._scan_one(store, path)
        except Exception as exc:  # noqa: BLE001 - keep daemon alive
            self.log.error("scan cycle failed: %s", exc)
            return -1
        return 0

    def _scan_one(self, store: Store, path: str) -> None:
        """Scan one target path, diff, persist, and dispatch alerts."""
        snapshot, line_maps = self._scanner.scan_path_with_lines(path, self.fmt)
        baseline = store.get_baseline(self.baseline_name)
        rules = store.list_rules(baseline.id)
        items, summary = self._differ.diff_snapshot(
            baseline.data,
            snapshot,
            rules,
            severity_rules=self.severity_rules,
            old_lines=baseline.line_maps,
            new_lines=line_maps,
            constraints=self._constraints,
        )
        report = Report(
            scan_id=None,
            baseline=baseline,
            created_at=utcnow_iso(),
            mode="daemon",
            summary=summary,
            items=items,
        )
        payload = {"code": 0, "data": report.to_dict(), "message": "ok"}
        scan_id = store.add_scan(baseline.id, "daemon", payload)
        report.scan_id = scan_id
        # v0.7.0 (D1): the differ/engine stay pure — drift constraint
        # violations are persisted here, in the calling layer, right after
        # add_scan (daemon does not surface baseline violations, §6.9).
        drift_rows = violations_from_items(items)
        if drift_rows:
            store.add_constraint_violations(scan_id, drift_rows)
        self.log.info(
            "scan cycle done scan_id=%d target=%s total=%d max=%s",
            scan_id,
            path,
            summary.total,
            summary.max_severity.value,
        )
        if summary.total > 0 and self.dispatcher is not None:
            results = self.dispatcher.dispatch_report(
                self.baseline_name, path, report
            )
            for result in results:
                if result.sent:
                    self.log.info(
                        "alert %s dispatched (attempts=%d)",
                        result.rule.name,
                        result.attempts,
                    )
                else:
                    self.log.error(
                        "alert %s dispatch failed: %s",
                        result.rule.name,
                        result.error,
                    )

    # -- stop handling ----------------------------------------------------

    def _stop_requested(self) -> bool:
        if self._stop_event.is_set():
            return True
        if self.stop_file:
            try:
                with open(self.stop_file, "r", encoding="utf-8") as fh:
                    content = fh.read().strip()
                if content == str(os.getpid()):
                    return True
            except OSError:
                pass
        return False

    def request_stop(self) -> None:
        """Programmatic stop (used by the SIGTERM handler and tests)."""
        self._stop_event.set()

    def _handle_sigterm(self, signum: int, frame: Any) -> None:
        self.log.info("received SIGTERM, stopping...")
        self._stop_event.set()

    def _sleep_until_next(self) -> None:
        """Sleep in 1s slices so stop requests are honored quickly."""
        remaining = float(self.interval)
        while remaining > 0 and not self._stop_requested():
            time.sleep(min(_SLEEP_SLICE, remaining))
            remaining -= _SLEEP_SLICE

    # -- pid / info / readiness helpers -----------------------------------

    def _write_pid_file(self) -> None:
        if not self.pid_file:
            return
        try:
            parent = os.path.dirname(self.pid_file)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(self.pid_file, "w", encoding="utf-8") as fh:
                fh.write(str(os.getpid()))
            self.log.info("pid file written: %s (pid=%d)", self.pid_file, os.getpid())
        except OSError as exc:
            self.log.warning("failed to write pid file %s: %s", self.pid_file, exc)

    def _clear_pid_file(self) -> None:
        if not self.pid_file:
            return
        try:
            if os.path.exists(self.pid_file):
                os.remove(self.pid_file)
                self.log.info("pid file cleared: %s", self.pid_file)
        except OSError as exc:
            self.log.warning("failed to clear pid file %s: %s", self.pid_file, exc)

    def _write_info_file(self) -> None:
        if not self.info_file:
            return
        try:
            parent = os.path.dirname(self.info_file)
            if parent:
                os.makedirs(parent, exist_ok=True)
            info = {
                "pid": os.getpid(),
                "started_at": utcnow_iso(),
                "interval": self.interval,
                "targets": self.paths,
                "baseline": self.baseline_name,
                "store": self.store_path,
                "log_file": self.log_file,
            }
            with open(self.info_file, "w", encoding="utf-8") as fh:
                json.dump(info, fh, ensure_ascii=False, indent=2)
        except OSError as exc:
            self.log.warning("failed to write info file %s: %s", self.info_file, exc)

    def _clear_info_file(self) -> None:
        if not self.info_file:
            return
        try:
            if os.path.exists(self.info_file):
                os.remove(self.info_file)
        except OSError as exc:
            self.log.warning("failed to clear info file %s: %s", self.info_file, exc)

    def _clear_stop_file(self) -> None:
        if not self.stop_file:
            return
        try:
            if os.path.exists(self.stop_file):
                os.remove(self.stop_file)
        except OSError as exc:
            self.log.warning("failed to clear stop file %s: %s", self.stop_file, exc)

    def _notify_readiness(
        self, fd: int, ok: bool = True, err: Optional[str] = None, pid: Optional[int] = None
    ) -> None:
        """Write the readiness line to the POSIX daemonize pipe."""
        try:
            if ok:
                os.write(fd, ("ok %d\n" % pid).encode("utf-8"))
            else:
                os.write(fd, ("err:%s\n" % (err or "unknown error")).encode("utf-8"))
        except OSError:
            pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Worker command single source of truth (D9, v0.5.0)
# ---------------------------------------------------------------------------

def build_worker_command(home: str, opts: Dict[str, Any]) -> List[str]:
    """Build the daemon worker argv — the only construction point (D9).

    ``DaemonManager._worker_command`` and ``AutostartManager`` both delegate
    here so autostart units and ``daemon start`` can never drift apart.

    ``opts`` supports the keys used by the daemon CLI: ``store`` /
    ``baseline`` / ``fmt`` / ``interval`` / ``targets`` (or ``paths``) /
    ``log_file`` / ``log_level`` and the optional ``pid_file`` / ``stop_file``
    / ``info_file`` / ``alerts_config`` / ``alert_state``.  PID/sentinel/
    info flags are emitted only when provided (autostart units omit them).
    """
    cmd = [
        sys.executable,
        "-m",
        "cfgdrift.daemon.worker",
        "--home", os.path.abspath(home),
        "--store", opts["store"],
        "--baseline", opts["baseline"],
        "--format", opts.get("fmt", "auto"),
        "--interval", str(opts.get("interval", _DEFAULT_INTERVAL)),
    ]
    pid_file = opts.get("pid_file")
    stop_file = opts.get("stop_file")
    info_file = opts.get("info_file")
    if pid_file:
        cmd += ["--pid-file", pid_file]
    if stop_file:
        cmd += ["--stop-file", stop_file]
    if info_file:
        cmd += ["--info-file", info_file]
    log_file = opts.get("log_file") or os.path.join(home, "logs", "daemon.log")
    cmd += ["--log-file", log_file]
    cmd += ["--log-level", opts.get("log_level", "INFO")]
    alerts_config = opts.get("alerts_config")
    alert_state = opts.get("alert_state")
    if alerts_config:
        cmd += ["--alerts-config", alerts_config]
    if alert_state:
        cmd += ["--alert-state", alert_state]
    # v0.6.0: built-in constraint library + extra constraint files.
    if opts.get("builtin") is False:
        cmd += ["--no-builtin"]
    for extra in opts.get("constraint_paths", []) or []:
        cmd += ["--constraints", extra]
    for target in opts.get("targets", []) or opts.get("paths", []):
        cmd += ["--path", target]
    return cmd


# ---------------------------------------------------------------------------
# CLI entry points
# ---------------------------------------------------------------------------

def _build_dispatcher(opts: Dict[str, Any]) -> Optional[AlertDispatcher]:
    """Load alerts.yaml + alert_state.json and build the dispatcher.

    Returns ``None`` when the rules file does not exist (daemon runs without
    alerts).  Configuration errors raise so startup fails loudly (exit 2).
    """
    alerts_config = opts.get("alerts_config")
    if not alerts_config or not os.path.exists(alerts_config):
        return None
    rules = AlertConfig.load(alerts_config)
    if not rules:
        return None
    state_path = opts.get("alert_state") or os.path.join(
        os.path.dirname(alerts_config) or ".", "alert_state.json"
    )
    state = AlertStateStore(state_path)
    return AlertDispatcher(rules, state)


def _load_masker(opts: Dict[str, Any]) -> SensitiveMasker:
    """Load masking.yaml (missing/corrupt -> default masker).

    Always returns a masker so alert payloads are masked by default at the
    alert display exit (the database keeps raw values).
    """
    home = opts.get("home")
    if not home:
        return SensitiveMasker()
    return SensitiveMasker.from_config(masking_config_path(home))


def _load_severity_rules(opts: Dict[str, Any]) -> List[Any]:
    """Load severity.yaml rules (empty list when absent; corrupt raises)."""
    home = opts.get("home")
    if not home:
        return []
    path = severity_config_path(home)
    if not os.path.exists(path):
        return []
    return SeverityConfig.load(path)


def run_with_opts(opts: Dict[str, Any]) -> int:
    """Build logging + worker from a resolved options dict and run."""
    log = setup_logging(
        opts.get("log_file"),
        opts.get("log_level", "INFO"),
        foreground=bool(opts.get("foreground")),
    )
    dispatcher = _build_dispatcher(opts)
    if dispatcher is not None:
        log.info(
            "alert dispatcher ready (%d rule(s))",
            len(dispatcher.rules),
        )
    masker = _load_masker(opts)
    if masker is not None:
        log.info("sensitive-value masker ready (keywords=%d patterns=%d)",
                 len(masker.keywords), len(masker.patterns))
    severity_rules = _load_severity_rules(opts)
    if severity_rules:
        log.info("custom severity rules loaded (%d rule(s))", len(severity_rules))
    paths = opts.get("paths") or opts.get("targets") or []
    worker = DaemonWorker(
        store_path=opts["store"],
        paths=paths,
        fmt=opts.get("fmt", "auto"),
        baseline_name=opts["baseline"],
        interval=opts.get("interval", _DEFAULT_INTERVAL),
        dispatcher=dispatcher,
        pid_file=opts.get("pid_file"),
        stop_file=opts.get("stop_file"),
        info_file=opts.get("info_file"),
        log_file=opts.get("log_file"),
        log=log,
        masker=masker,
        severity_rules=severity_rules,
        home=opts.get("home"),
        builtin_enabled=opts.get("builtin", True) is not False,
        constraint_paths=opts.get("constraint_paths") or [],
    )
    # POSIX daemonize passes a readiness fd; Windows/foreground do not.
    readiness_fd = opts.get("readiness_fd")
    if readiness_fd is not None:
        try:
            readiness_fd = int(readiness_fd)
        except (TypeError, ValueError):
            readiness_fd = None
    if os.name == "posix":
        signal.signal(signal.SIGTERM, worker._handle_sigterm)
    return worker.run(readiness_fd=readiness_fd)


def main(argv: Optional[List[str]] = None) -> int:
    """argparse entry point for ``python -m cfgdrift.daemon.worker``."""
    parser = argparse.ArgumentParser(
        prog="cfgdrift.daemon.worker",
        description="cfgdrift daemon worker (v%s)" % __version__,
    )
    parser.add_argument("--home", default=None, help="cfgdrift data directory.")
    parser.add_argument("--store", default=None, help="SQLite database file.")
    parser.add_argument(
        "--path", dest="paths", action="append", required=True,
        help="Target path to monitor (repeatable).",
    )
    parser.add_argument("--baseline", required=True, help="Baseline name.")
    # v0.5.0: free string (D8) — the runtime validate_format accepts built-in
    # formats plus registered parser plugins and gives a readable error.
    parser.add_argument("--format", default="auto",
                        help="Config format (auto/json/yaml/toml/ini or a registered plugin name).")
    parser.add_argument("--interval", type=int, default=_DEFAULT_INTERVAL)
    parser.add_argument("--pid-file", default=None)
    parser.add_argument("--stop-file", default=None)
    parser.add_argument("--info-file", default=None)
    parser.add_argument("--log-file", default=None)
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--alerts-config", default=None)
    parser.add_argument("--alert-state", default=None)
    parser.add_argument("--foreground", action="store_true")
    # v0.6.0: consistency constraints.  The built-in toggle is an explicit
    # flag pair (argparse has no --flag/--no-flag shortcut syntax; writing
    # "--builtin/--no-builtin" would be parsed as one value-taking option).
    parser.add_argument("--builtin", dest="builtin", action="store_true",
                        default=True,
                        help="Enable the built-in constraint library (default: on).")
    parser.add_argument("--no-builtin", dest="builtin", action="store_false",
                        help="Disable the built-in constraint library.")
    parser.add_argument("--constraints", dest="constraint_files",
                        action="append", default=[],
                        help="Extra constraints.yaml file (repeatable; v0.6.0).")
    args = parser.parse_args(argv)

    home = args.home or default_home()
    opts: Dict[str, Any] = {
        "home": home,
        "store": args.store or os.path.join(home, "cfgdrift.db"),
        "paths": list(args.paths),
        "baseline": args.baseline,
        "fmt": args.format,
        "interval": args.interval if args.interval and args.interval > 0 else _DEFAULT_INTERVAL,
        "pid_file": args.pid_file or os.path.join(home, "daemon.pid"),
        "stop_file": args.stop_file or os.path.join(home, "daemon.stop"),
        "info_file": args.info_file or os.path.join(home, "daemon.info.json"),
        "log_file": args.log_file or os.path.join(home, "logs", "daemon.log"),
        "log_level": args.log_level,
        "alerts_config": args.alerts_config
        or (os.path.join(home, "alerts.yaml") if home else None),
        "alert_state": args.alert_state
        or (os.path.join(home, "alert_state.json") if home else None),
        "foreground": args.foreground,
        "builtin": args.builtin,
        "constraint_paths": list(args.constraint_files),
    }
    return run_with_opts(opts)


if __name__ == "__main__":  # pragma: no cover - subprocess entry
    sys.exit(main())
