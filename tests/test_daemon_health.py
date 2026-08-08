"""cfgdrift v0.11.0 daemon-health tests (P0-1).

Covers:
- ``DaemonWorker._persist_info`` — atomic write (temp + ``os.replace``), no
  half-written JSON observable, self-heal after a corrupt file;
- ``DaemonWorker._record_cycle`` — ``cycles`` rolling log capped at 20 with
  ``cycles_total`` monotonic inside one worker session;
- the ``_daemon_status_payload`` assembly — ``error_rate`` (0~1 float, 4dp) /
  ``cycles_total`` / ``cycles_failed`` appear exactly when the daemon is
  running with a non-empty cycle log (zero-noise otherwise), for both
  ``/api/overview`` and ``/api/daemon-status``.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

from cfgdrift.daemon.worker import DaemonWorker, _MAX_CYCLE_LOG  # noqa: E402

try:
    from fastapi.testclient import TestClient  # noqa: E402

    WEB_OK = True
except Exception:  # pragma: no cover - optional dependency
    TestClient = None  # type: ignore
    WEB_OK = False


def _make_worker(home, info_file):
    return DaemonWorker(
        store_path=os.path.join(home, "cfgdrift.db"),
        paths=[home],
        fmt="auto",
        baseline_name="prod",
        interval=300,
        dispatcher=None,
        info_file=info_file,
    )


class TestCycleLog:
    def test_rolling_cap_and_total(self, tmp_path):
        info = os.path.join(str(tmp_path), "daemon.info.json")
        w = _make_worker(str(tmp_path), info)
        w._write_info_file()
        for i in range(25):
            w._record_cycle(ok=(i % 5 != 0))
        data = json.load(open(info, encoding="utf-8"))
        assert data["cycles_total"] == 25
        assert len(data["cycles"]) == _MAX_CYCLE_LOG
        failed = sum(1 for c in data["cycles"] if not c["ok"])
        assert failed == 4  # 25 cycles, rolling window keeps 20 -> 4 fails
        assert all("ts" in c and "ok" in c for c in data["cycles"])

    def test_atomic_write_no_tmp_left(self, tmp_path):
        info = os.path.join(str(tmp_path), "daemon.info.json")
        w = _make_worker(str(tmp_path), info)
        w._record_cycle(ok=True)
        assert not os.path.exists(info + ".tmp")
        # file is complete valid JSON
        json.load(open(info, encoding="utf-8"))

    def test_corrupt_file_self_heals(self, tmp_path):
        info = os.path.join(str(tmp_path), "daemon.info.json")
        w = _make_worker(str(tmp_path), info)
        with open(info, "w", encoding="utf-8") as fh:
            fh.write("{broken")
        w._record_cycle(ok=False)
        data = json.load(open(info, encoding="utf-8"))
        assert data["cycles_total"] == 1
        assert data["cycles"][0]["ok"] is False
        # base fields rebuilt
        assert data["baseline"] == "prod"

    def test_base_info_shape(self, tmp_path):
        info = os.path.join(str(tmp_path), "daemon.info.json")
        w = _make_worker(str(tmp_path), info)
        w._write_info_file()
        data = json.load(open(info, encoding="utf-8"))
        for key in ("pid", "started_at", "interval", "targets", "baseline", "store"):
            assert key in data


@pytest.mark.skipif(not WEB_OK, reason="fastapi/httpx unavailable")
class TestDaemonStatusPayload:
    @staticmethod
    def _run_status_payload(home, info_cycles, running):
        from cfgdrift.web.app import create_app
        from cfgdrift.storage.store import Store

        store = Store(os.path.join(str(home), "cfgdrift.db"))
        pid_file = os.path.join(str(home), "daemon.pid")
        info_file = os.path.join(str(home), "daemon.info.json")
        os.makedirs(str(home), exist_ok=True)
        if running:
            # A real process for pid existence checks is impractical in CI;
            # we fake the status by writing a pid that resolves to this
            # process (the daemon-status endpoint is best-effort and the
            # payload builder is what we assert on via overview).
            with open(pid_file, "w", encoding="utf-8") as fh:
                fh.write(str(os.getpid()))
            with open(info_file, "w", encoding="utf-8") as fh:
                json.dump(
                    {"pid": os.getpid(), "interval": 300, "cycles": info_cycles},
                    fh,
                )
        app = create_app(store, home=str(home))
        client = TestClient(app)
        return client, store

    def test_error_rate_appears_in_overview(self, tmp_path):
        cycles = [
            {"ts": "2026-08-05T09:00:00+00:00", "ok": True},
            {"ts": "2026-08-05T09:05:00+00:00", "ok": False},
            {"ts": "2026-08-05T09:10:00+00:00", "ok": True},
        ]
        client, store = self._run_status_payload(tmp_path, cycles, running=True)
        try:
            data = client.get("/api/overview").json()["data"]
            ds = data["daemon_status"]
            assert ds["running"] is True
            assert ds["error_rate"] == round(1 / 3, 4)
            assert ds["cycles_total"] == 3
            assert ds["cycles_failed"] == 1
            # existing overview fields intact
            assert "severity_distribution" in data
            assert "totals" in data
        finally:
            store.close()

    def test_error_rate_omitted_when_no_records(self, tmp_path):
        client, store = self._run_status_payload(tmp_path, [], running=True)
        try:
            data = client.get("/api/daemon-status").json()["data"]
            assert "error_rate" not in data
            assert "cycles_total" not in data
            assert "cycles_failed" not in data
            assert data["running"] is True
        finally:
            store.close()

    def test_error_rate_omitted_when_not_running(self, tmp_path):
        cycles = [{"ts": "t", "ok": False}]
        client, store = self._run_status_payload(tmp_path, cycles, running=False)
        try:
            data = client.get("/api/daemon-status").json()["data"]
            assert data["running"] is False
            assert "error_rate" not in data
        finally:
            store.close()

    def test_existing_shape_preserved(self, tmp_path):
        cycles = [{"ts": "t", "ok": True}]
        client, store = self._run_status_payload(tmp_path, cycles, running=True)
        try:
            data = client.get("/api/daemon-status").json()["data"]
            for key in ("running", "pid", "stale", "info", "error", "last_scan"):
                assert key in data
        finally:
            store.close()
