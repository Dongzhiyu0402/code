"""Web v0.9.0 tests: /api/scans pagination + compare constraint alignment.

Covers:
- ``GET /api/scans`` — pagination (limit/offset), total, ``q`` fuzzy search
  (scan id / baseline name / mode), severity/mode exact filters, limit clamp
  to [1,500], invalid limit/offset -> 400, empty state (no error).
- ``POST /api/compare`` — default constraint library is active, user
  constraints.yaml rules apply, extra ``constraints`` file paths pass
  through, no-violation responses stay zero-noise (no ``constraint_violations``
  key), and violations never change the drift statistics.
"""

from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

from cfgdrift.core.model import Constraint  # noqa: E402
from cfgdrift.rules.constraints import (  # noqa: E402
    ConstraintConfig,
    default_path as constraints_path,
)
from cfgdrift.storage.store import Store  # noqa: E402

try:
    from fastapi.testclient import TestClient  # noqa: E402

    WEB_OK = True
except Exception:  # pragma: no cover - optional dependency
    TestClient = None  # type: ignore
    WEB_OK = False


def _scan_report(scan_id_label: str, mode: str, max_severity: str,
                 created: str, total: int) -> dict:
    return {
        "code": 0,
        "data": {
            "scan_id": None,
            "baseline": {"name": "prod", "version": 1},
            "created_at": created,
            "mode": mode,
            "summary": {
                "added": total, "removed": 0, "modified": 0,
                "type_changed": 0, "ignored": 0, "total": total,
                "max_severity": max_severity,
            },
            "items": [],
        },
    }


@pytest.fixture()
def client_env(tmp_path):
    from cfgdrift.web.app import create_app

    home = str(tmp_path / "home")
    os.makedirs(home, exist_ok=True)
    store = Store(str(tmp_path / "cfgdrift.db"))
    bl = store.create_baseline(
        "prod", "prod baseline", str(tmp_path), "json",
        {"cfg.json": {"server": {"port": 8080, "token": "sek"}}}, {},
    )
    # 6 scans: mixed severity + mode so filters have something to chew on.
    seeds = [
        (1, "daemon", "CRITICAL", "2026-08-05T09:00:00+00:00", 3),
        (2, "manual", "WARN", "2026-08-05T08:00:00+00:00", 1),
        (3, "daemon", "WARN", "2026-08-05T07:00:00+00:00", 0),
        (4, "manual", "CRITICAL", "2026-08-05T06:00:00+00:00", 2),
        (5, "watch", "INFO", "2026-08-05T05:00:00+00:00", 0),
        (6, "manual", "NONE", "2026-08-05T04:00:00+00:00", 0),
    ]
    for _, mode, sev, created, total in seeds:
        store.add_scan(bl.id, mode, _scan_report(0, mode, sev, created, total))

    # One user constraint that violates prod's cfg.json (port 8080 < 9000).
    ConstraintConfig.add_rule(
        constraints_path(home),
        Constraint.from_dict(
            {
                "id": "user_port_low",
                "type": "range",
                "keys": ["server.port"],
                "min": 9000,
                "message": "port must be >= 9000",
                "severity": "WARN",
            },
            source="user",
        ),
    )

    app = create_app(store, home=home)
    client = TestClient(app)
    yield client, store, home
    store.close()


@pytest.mark.skipif(not WEB_OK, reason="fastapi/httpx unavailable")
class TestScansEndpoint:
    def test_pagination_and_total(self, client_env):
        client, _, _ = client_env
        r = client.get("/api/scans", params={"limit": 2, "offset": 2})
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["total"] == 6
        assert len(data["scans"]) == 2
        # ORDER BY id DESC: offset 2 -> ids 4, 3
        assert [s["scan_id"] for s in data["scans"]] == [4, 3]
        scan = data["scans"][0]
        assert set(scan.keys()) == {"scan_id", "baseline_id", "mode",
                                    "created_at", "baseline", "summary"}
        assert scan["baseline"] == {"name": "prod", "version": 1}
        assert scan["summary"]["max_severity"] == "CRITICAL"

    def test_search_by_scan_id(self, client_env):
        client, _, _ = client_env
        r = client.get("/api/scans", params={"q": "#3"})
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["total"] == 1
        assert data["scans"][0]["scan_id"] == 3

    def test_search_by_baseline_name(self, client_env):
        client, _, _ = client_env
        r = client.get("/api/scans", params={"q": "prod"})
        assert r.json()["data"]["total"] == 6

    def test_search_by_mode(self, client_env):
        client, _, _ = client_env
        r = client.get("/api/scans", params={"q": "daemon"})
        assert r.json()["data"]["total"] == 2

    def test_severity_filter(self, client_env):
        client, _, _ = client_env
        r = client.get("/api/scans", params={"severity": "CRITICAL"})
        data = r.json()["data"]
        assert data["total"] == 2
        assert all(s["summary"]["max_severity"] == "CRITICAL"
                   for s in data["scans"])

    def test_mode_filter(self, client_env):
        client, _, _ = client_env
        r = client.get("/api/scans", params={"mode": "manual"})
        data = r.json()["data"]
        assert data["total"] == 3
        assert all(s["mode"] == "manual" for s in data["scans"])

    def test_combined_filters(self, client_env):
        client, _, _ = client_env
        r = client.get("/api/scans", params={"q": "prod", "severity": "WARN"})
        data = r.json()["data"]
        assert data["total"] == 2
        assert all(s["summary"]["max_severity"] == "WARN"
                   for s in data["scans"])

    def test_like_metachar_escaped(self, client_env):
        client, _, _ = client_env
        # '%' must be matched literally, not as a wildcard -> no rows.
        r = client.get("/api/scans", params={"q": "%"})
        data = r.json()["data"]
        assert data["total"] == 0
        assert data["scans"] == []

    def test_limit_clamped(self, client_env):
        client, _, _ = client_env
        r = client.get("/api/scans", params={"limit": 99999})
        assert r.status_code == 200
        assert len(r.json()["data"]["scans"]) <= 500
        r = client.get("/api/scans", params={"limit": 0})
        assert r.json()["data"]["total"] == 6  # clamp to 1 -> still works

    def test_invalid_limit_400(self, client_env):
        client, _, _ = client_env
        r = client.get("/api/scans", params={"limit": "abc"})
        assert r.status_code == 400
        assert r.json()["code"] == 2

    def test_no_match_empty_state(self, client_env):
        client, _, _ = client_env
        r = client.get("/api/scans", params={"q": "zzz-no-such-scan"})
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["total"] == 0
        assert data["scans"] == []


@pytest.mark.skipif(not WEB_OK, reason="fastapi/httpx unavailable")
class TestCompareConstraints:
    def _make_dev(self, store, root, port=0):
        # port 0 violates the built-in http_port_range [1, 65535].
        store.create_baseline(
            "dev", "dev baseline", root, "json",
            {"cfg.json": {"server": {"port": port, "token": "dev"}}}, {},
        )

    def test_default_library_active(self, client_env):
        # dev port 0 violates the built-in http_port_range.
        client, store, home = client_env
        self._make_dev(store, str(tmp := os.path.dirname(home)))
        r = client.post("/api/compare", json={"env1": "prod", "env2": "dev",
                                              "constraints": []})
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert "constraint_violations" in data
        cv = data["constraint_violations"]
        sides = list(cv.keys())
        assert set(sides) <= {"env_a", "env_b"}
        all_violations = cv.get("env_a", []) + cv.get("env_b", [])
        ids = {v["constraint_id"] for v in all_violations}
        assert "http_port_range" in ids

    def test_user_constraint_applies(self, client_env):
        # user_port_low: server.port must be >= 1000.  prod has 8080.
        client, store, home = client_env
        self._make_dev(store, os.path.dirname(home), port=9090)
        r = client.post("/api/compare", json={"env1": "prod", "env2": "dev",
                                              "constraints": []})
        assert r.status_code == 200
        cv = r.json()["data"]["constraint_violations"]
        ids = {v["constraint_id"]
               for v in cv.get("env_a", []) + cv.get("env_b", [])}
        assert "user_port_low" in ids

    def test_extra_constraint_file(self, client_env):
        client, store, home = client_env
        self._make_dev(store, os.path.dirname(home), port=9090)
        extra = os.path.join(os.path.dirname(home), "extra.yaml")
        with open(extra, "w", encoding="utf-8") as fh:
            fh.write(
                "version: 1\nrules:\n"
                "  - id: extra_tag\n    type: enum\n"
                "    keys: [server.token]\n    allowed: ['x-only']\n"
                "    message: token must be x-only\n    severity: CRITICAL\n"
            )
        r = client.post("/api/compare", json={"env1": "prod", "env2": "dev",
                                              "constraints": [extra]})
        assert r.status_code == 200, r.text
        cv = r.json()["data"]["constraint_violations"]
        ids = {v["constraint_id"]
               for v in cv.get("env_a", []) + cv.get("env_b", [])}
        assert "extra_tag" in ids

    def test_missing_constraint_file_400(self, client_env):
        client, store, home = client_env
        self._make_dev(store, os.path.dirname(home), port=9090)
        r = client.post("/api/compare", json={"env1": "prod", "env2": "dev",
                                              "constraints": ["/nope.yaml"]})
        assert r.status_code == 400
        assert "constraints file not found" in r.json()["message"]

    def test_no_violations_zero_noise(self, client_env):
        client, store, home = client_env
        # Both sides fully compliant (port 9000 >= 9000 and inside the
        # built-in range) -> no violations anywhere.
        store.create_baseline(
            "clean-a", "clean a", os.path.dirname(home), "json",
            {"cfg.json": {"server": {"port": 9000, "token": "a"}}}, {},
        )
        store.create_baseline(
            "clean-b", "clean b", os.path.dirname(home), "json",
            {"cfg.json": {"server": {"port": 9000, "token": "b"}}}, {},
        )
        r = client.post("/api/compare", json={"env1": "clean-a",
                                              "env2": "clean-b",
                                              "constraints": []})
        assert r.status_code == 200
        data = r.json()["data"]
        # Zero-noise: no constraint_violations key when both sides are clean.
        assert "constraint_violations" not in data
        assert "summary" in data
        assert "items" in data

    def test_violations_do_not_change_stats(self, client_env):
        client, store, home = client_env
        self._make_dev(store, os.path.dirname(home), port=9090)
        r_clean = client.post("/api/compare", json={"env1": "prod",
                                                    "env2": "dev"})
        r_vio = client.post("/api/compare", json={"env1": "prod", "env2": "dev",
                                                  "constraints": []})
        assert r_clean.status_code == 200
        assert r_vio.status_code == 200
        # drift summary is identical with or without constraint checking.
        assert r_clean.json()["data"]["summary"] == r_vio.json()["data"]["summary"]
        assert len(r_clean.json()["data"]["items"]) == len(
            r_vio.json()["data"]["items"])
