"""File / directory scanning and watch loop."""

from __future__ import annotations

import os
import sys
import time
from typing import Callable, Dict, Optional

from ..core.parser import detect_format, parse_file, validate_format


def _normalize_relpath(root: str, full_path: str) -> str:
    """Return a '/' separated relative path for a file under root."""
    rel = os.path.relpath(full_path, root)
    return rel.replace(os.sep, "/")


def _iter_files(root: str):
    """Yield absolute paths of regular files under root (recursive)."""
    for dirpath, dirnames, filenames in os.walk(root):
        # Skip common VCS/cache directories to keep scans predictable.
        dirnames[:] = [
            d
            for d in dirnames
            if d not in (".git", ".hg", ".svn", "__pycache__", ".venv", "venv")
        ]
        dirnames.sort()
        for name in sorted(filenames):
            yield os.path.join(dirpath, name)


class Scanner:
    """Collects config files into snapshots ``{relpath: tree}``."""

    def scan_path(self, path: str, fmt: str = "auto") -> Dict[str, object]:
        """Scan a single file or a directory into a snapshot."""
        fmt = validate_format(fmt)
        path = os.path.abspath(path)
        if os.path.isfile(path):
            return self._scan_file(path, fmt)
        if os.path.isdir(path):
            return self._scan_dir(path, fmt)
        raise ValueError("path does not exist: %s" % path)

    def _scan_file(self, path: str, fmt: str) -> Dict[str, object]:
        use_fmt = fmt
        if use_fmt == "auto":
            detected = detect_format(path)
            if detected is None:
                raise ValueError(
                    "cannot auto-detect format for %r (use --format "
                    "json|yaml|toml|ini)" % path
                )
            use_fmt = detected
        tree = parse_file(path, fmt=use_fmt)
        relpath = os.path.basename(path)
        return {relpath: tree}

    def _scan_dir(self, root: str, fmt: str) -> Dict[str, object]:
        snapshot: Dict[str, object] = {}
        root_abs = os.path.abspath(root)
        for full in _iter_files(root_abs):
            detected = detect_format(full)
            if fmt != "auto":
                if detected is None or detected != fmt:
                    continue
            if detected is None:
                print(
                    "warning: skipping unknown extension: %s"
                    % _normalize_relpath(root_abs, full),
                    file=sys.stderr,
                )
                continue
            relpath = _normalize_relpath(root_abs, full)
            try:
                snapshot[relpath] = parse_file(full, fmt=detected)
            except ValueError as exc:
                raise ValueError("failed to parse %s: %s" % (relpath, exc)) from exc
        return snapshot

    def watch(
        self,
        root: str,
        fmt: str,
        interval: int,
        on_scan: Callable[[Dict[str, object]], None],
    ) -> None:
        """Poll the path every ``interval`` seconds until Ctrl+C.

        ``on_scan`` receives each snapshot; the loop exits cleanly on
        KeyboardInterrupt.
        """
        print("watching %s every %ds (Ctrl+C to stop)..." % (root, interval))
        while True:
            try:
                snapshot = self.scan_path(root, fmt)
                on_scan(snapshot)
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # keep watching across transient errors
                print("error: %s" % exc, file=sys.stderr)
            try:
                time.sleep(interval)
            except KeyboardInterrupt:
                print("stopped.")
                return
