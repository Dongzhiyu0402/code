"""Web constraint view tests (v0.7.0, C-09 / C-10 / T05).

Covers the three new endpoints through FastAPI's TestClient:
- ``GET /api/constraints`` — effective view (builtin + user, D6);
- ``PUT /api/constraints/{id}/enabled`` — user rules toggle, builtin -> 400,
  missing -> 404;
- ``GET /api/constraint-events`` — pagination + filters;
plus the SPA static wiring (nav button, section, render functions).
"""

from __future__ import annotations

import json
import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

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

USER_RULE = {
    "id": "user_port_range",
    "type": "range",
    "keys": ["server.port"],
    "min": 1,
    "max": 65535,
    "message": "user port range rule",
    "severity": "WARN",
    "enabled": True,
}


@pytest.fixture()
def client_env(tmp_path):
    from cfgdrift.core.model import Constraint
    from cfgdrift.web.app import create_app

    home = str(tmp_path / "home")
    os.makedirs(home, exist_ok=True)
    store = Store(str(tmp_path / "cfgdrift.db"))
    # Seed one user constraint and a few C-10 events.
    ConstraintConfig.add_rule(
        constraints_path(home), Constraint.from_dict(dict(USER_RULE), source="user")
    )
    store.add_constraint_violations(
        1,
        [
            {"constraint_id": "user_port_range", "kind": "drift",
             "file": "a.yaml", "keys": ["server.port"], "severity": "WARN",
             "detail": "port out of range"},
            {"constraint_id": "http_gzip_enum", "kind": "baseline",
             "file": "b.yaml", "keys": ["gzip"], "severity": "WARN",
             "detail": "gzip bad"},
        ],
    )
    app = create_app(store, home=home)
    client = TestClient(app)
    yield client, store, home
    store.close()


@pytest.mark.skipif(not WEB_OK, reason="fastapi/httpx unavailable")
class TestConstraintsEndpoint:
    def test_list_effective(self, client_env):
        client, _, _ = client_env
        resp = client.get("/api/constraints")
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 0
        constraints = body["data"]["constraints"]
        ids = {c["id"]: c for c in constraints}
        # built-in library present
        assert "http_port_range" in ids
        assert ids["http_port_range"]["source"] == "builtin"
        # user rule present with source user
        assert "user_port_range" in ids
        user = ids["user_port_range"]
        assert user["source"] == "user"
        assert user["enabled"] is True
        assert user["type"] == "range"
        assert user["keys"] == ["server.port"]
        # same view as `constraint list --source all` (D6)
        assert len(constraints) == 21  # 20 builtin + 1 user

    def test_toggle_user_rule(self, client_env):
        client, _, home = client_env
        resp = client.put(
            "/api/constraints/user_port_range/enabled",
            json={"enabled": False},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["id"] == "user_port_range"
        assert data["enabled"] is False
        # constraints.yaml on disk was updated
        loaded = ConstraintConfig.list_rules(constraints_path(home))
        rule = next(r for r in loaded if r.id == "user_port_range")
        assert rule.enabled is False
        # effective view reflects the toggle
        resp2 = client.get("/api/constraints")
        user = next(
            c for c in resp2.json()["data"]["constraints"]
            if c["id"] == "user_port_range"
        )
        assert user["enabled"] is False

    def test_toggle_builtin_400(self, client_env):
        client, _, _ = client_env
        resp = client.put(
            "/api/constraints/http_port_range/enabled", json={"enabled": False}
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == 2
        assert "内置约束不可直接切换" in body["message"]

    def test_toggle_missing_404(self, client_env):
        client, _, _ = client_env
        resp = client.put(
            "/api/constraints/nope/enabled", json={"enabled": True}
        )
        assert resp.status_code == 404


@pytest.mark.skipif(not WEB_OK, reason="fastapi/httpx unavailable")
class TestConstraintEventsEndpoint:
    def test_list_events(self, client_env):
        client, _, _ = client_env
        resp = client.get("/api/constraint-events")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["total"] == 2
        events = data["events"]
        assert len(events) == 2
        first = events[0]
        assert set(first) >= {"id", "constraint_id", "scan_id", "kind", "file",
                              "keys", "severity", "detail", "created_at"}
        assert first["kind"] in ("drift", "baseline")

    def test_filter_and_pagination(self, client_env):
        client, _, _ = client_env
        resp = client.get("/api/constraint-events", params={"kind": "drift"})
        assert resp.json()["data"]["total"] == 1
        resp = client.get("/api/constraint-events",
                          params={"constraint_id": "http_gzip_enum"})
        assert resp.json()["data"]["total"] == 1
        resp = client.get("/api/constraint-events", params={"limit": 1, "offset": 1})
        data = resp.json()["data"]
        assert data["total"] == 2
        assert len(data["events"]) == 1

    def test_limit_clamped(self, client_env):
        client, _, _ = client_env
        resp = client.get("/api/constraint-events", params={"limit": 99999})
        assert resp.status_code == 200
        assert len(resp.json()["data"]["events"]) <= 500


@pytest.mark.skipif(not WEB_OK, reason="fastapi/httpx unavailable")
class TestSpaWiring:
    def test_static_nav_and_section(self, client_env):
        client, _, _ = client_env
        html = client.get("/").text
        assert 'data-view="constraints"' in html
        assert 'id="view-constraints"' in html

    def test_app_js_functions(self, client_env):
        client, _, _ = client_env
        js = client.get("/app.js").text
        assert "renderConstraints" in js
        assert "renderConstraintEvents" in js
        assert "constraints: renderConstraints" in js  # VIEWS registration
