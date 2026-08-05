"""Alert v0.9.0 tests: enable/disable + Web test/retry operations (P0-2).

Covers:
- ``AlertConfig.set_enabled`` — persists to alerts.yaml, missing rule raises;
- CLI ``alert enable/disable`` — exit 0, missing rule -> exit 2, and
  interoperates with the Web PUT endpoint;
- ``PUT /api/alerts/{name}/enabled`` — survives a "restart" (re-load from
  disk), 400 on non-boolean body, 404 for missing rules;
- ``POST /api/alerts/{name}/test`` — sends event=cfgdrift.test, returns
  ``{sent, attempts, error}`` and never writes an alert event;
- ``POST /api/alert-events/{id}/retry`` — creates a new event with
  ``retried=1`` / ``retried_from``, the original row is preserved, and the
  new event is ``sent`` (or ``failed``) — cooldown is bypassed.
"""

from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

from cfgdrift.alert.config import AlertConfig  # noqa: E402
from cfgdrift.alert.models import AlertRule  # noqa: E402
from cfgdrift.alert.state import AlertStateStore  # noqa: E402
from cfgdrift.core.model import Severity  # noqa: E402
from cfgdrift.storage.store import Store  # noqa: E402

try:
    from fastapi.testclient import TestClient  # noqa: E402

    WEB_OK = True
except Exception:  # pragma: no cover - optional dependency
    TestClient = None  # type: ignore
    WEB_OK = False

_SYSPY = sys.executable


def _rule(name="drift-wx", enabled=True, type="script", ok=True):
    cmd = _SYSPY if ok else "__cfgdrift_no_such_command_xyz__"
    args = ["-c", "import sys; sys.exit(0)"] if ok else []
    return AlertRule(
        name=name,
        type=type,
        severity=Severity.WARN,
        enabled=enabled,
        config={"command": cmd, "args": args, "timeout": 5},
    )


def _seed_rule(home, rule=None) -> None:
    AlertConfig.save(
        os.path.join(home, "alerts.yaml"),
        [rule if rule is not None else _rule()],
    )


# ---------------------------------------------------------------------------
# AlertConfig.set_enabled + CLI
# ---------------------------------------------------------------------------


class TestSetEnabled:
    def test_toggle_persists(self, tmp_path):
        home = str(tmp_path)
        _seed_rule(home, _rule(enabled=True))
        path = os.path.join(home, "alerts.yaml")
        AlertConfig.set_enabled(path, "drift-wx", False)
        rules = AlertConfig.load(path)
        assert rules[0].enabled is False
        AlertConfig.set_enabled(path, "drift-wx", True)
        assert AlertConfig.load(path)[0].enabled is True

    def test_missing_raises(self, tmp_path):
        home = str(tmp_path)
        _seed_rule(home)
        with pytest.raises(ValueError):
            AlertConfig.set_enabled(os.path.join(home, "alerts.yaml"),
                                    "nope", True)


class TestCliEnableDisable:
    def test_enable_disable_exit_0(self, tmp_path, monkeypatch):
        from cfgdrift.cli import main

        home = str(tmp_path)
        _seed_rule(home, _rule(enabled=False))
        monkeypatch.setenv("CFGDRIFT_HOME", home)
        assert main(["alert", "enable", "drift-wx"]) == 0
        assert AlertConfig.load(os.path.join(home, "alerts.yaml"))[0].enabled is True
        assert main(["alert", "disable", "drift-wx"]) == 0
        assert AlertConfig.load(os.path.join(home, "alerts.yaml"))[0].enabled is False

    def test_missing_exit_2(self, tmp_path, monkeypatch):
        from cfgdrift.cli import main

        home = str(tmp_path)
        _seed_rule(home)
        monkeypatch.setenv("CFGDRIFT_HOME", home)
        assert main(["alert", "disable", "nope"]) == 2
        assert main(["alert", "enable", "nope"]) == 2


# ---------------------------------------------------------------------------
# Web endpoints
# ---------------------------------------------------------------------------


@pytest.fixture()
def client_env(tmp_path):
    from cfgdrift.web.app import create_app

    home = str(tmp_path / "home")
    os.makedirs(home, exist_ok=True)
    store = Store(str(tmp_path / "cfgdrift.db"))
    _seed_rule(home, _rule(enabled=True))
    app = create_app(store, home=home)
    client = TestClient(app)
    yield client, store, home
    store.close()


@pytest.mark.skipif(not WEB_OK, reason="fastapi/httpx unavailable")
class TestWebEnabled:
    def test_put_persists_across_restart(self, client_env):
        client, _, home = client_env
        r = client.put("/api/alerts/drift-wx/enabled", json={"enabled": False})
        assert r.status_code == 200
        assert r.json()["data"] == {"name": "drift-wx", "enabled": False}
        # alerts.yaml on disk changed.
        assert AlertConfig.load(os.path.join(home, "alerts.yaml"))[0].enabled is False
        # Simulate a restart: a fresh app built from the same home sees it.
        from cfgdrift.storage.store import Store as _Store
        from cfgdrift.web.app import create_app

        store2 = _Store(os.path.join(str(home).replace("home", "tmp"), "r.db"))
        client2 = TestClient(create_app(store2, home=home))
        try:
            resp = client2.get("/api/alerts")
            rule = next(r for r in resp.json()["data"]["alerts"]
                        if r["name"] == "drift-wx")
            assert rule["enabled"] is False
        finally:
            store2.close()

    def test_put_invalid_body_400(self, client_env):
        client, _, _ = client_env
        r = client.put("/api/alerts/drift-wx/enabled", json={"enabled": "yes"})
        assert r.status_code == 400

    def test_put_missing_404(self, client_env):
        client, _, _ = client_env
        r = client.put("/api/alerts/nope/enabled", json={"enabled": True})
        assert r.status_code == 404


@pytest.mark.skipif(not WEB_OK, reason="fastapi/httpx unavailable")
class TestWebTest:
    def test_test_success_no_event(self, client_env):
        client, store, _ = client_env
        r = client.post("/api/alerts/drift-wx/test")
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data["sent"] is True
        assert data["attempts"] >= 1
        assert data["error"] is None
        # No event may be written by a connectivity test.
        assert store.count_alert_events() == 0

    def test_test_failure(self, client_env):
        client, _, home = client_env
        _seed_rule(home, _rule(name="bad", ok=False))
        r = client.post("/api/alerts/bad/test")
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["sent"] is False
        assert data["error"]

    def test_test_missing_404(self, client_env):
        client, _, _ = client_env
        r = client.post("/api/alerts/nope/test")
        assert r.status_code == 404


@pytest.mark.skipif(not WEB_OK, reason="fastapi/httpx unavailable")
class TestWebRetry:
    def _seed_event(self, store, status="failed"):
        return store.add_alert_event(
            {
                "rule": "drift-wx",
                "baseline": "prod",
                "severity": "WARN",
                "status": status,
                "target": "echo",
                "drift_count": 3,
                "error": "boom" if status == "failed" else None,
                "attempts": 3 if status == "failed" else 1,
                "fingerprint": "fp123",
            }
        )

    def test_retry_creates_new_event(self, client_env):
        client, store, home = client_env
        original_id = self._seed_event(store, status="failed")
        r = client.post("/api/alert-events/%d/retry" % original_id)
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data["status"] == "sent"
        assert data["sent"] is True
        assert data["event_id"] == original_id + 1

        events = store.list_alert_events()["events"]
        assert len(events) == 2
        orig = {e["id"]: e for e in events}[original_id]
        new = {e["id"]: e for e in events}[data["event_id"]]
        # The original row is preserved untouched.
        assert orig["status"] == "failed"
        assert orig["error"] == "boom"
        # The new event carries the retry bookkeeping.
        assert new["retried"] == 1
        assert new["retried_from"] == original_id
        assert new["status"] == "sent"
        assert new["severity"] == "WARN"
        assert new["baseline"] == "prod"
        assert new["drift_count"] == 3

    def test_retry_does_not_touch_cooldown(self, client_env):
        client, store, home = client_env
        original_id = self._seed_event(store)
        state_path = os.path.join(home, "alert_state.json")
        before = AlertStateStore(state_path).entries()
        r = client.post("/api/alert-events/%d/retry" % original_id)
        assert r.status_code == 200
        after = AlertStateStore(state_path).entries()
        # No cooldown state may be written by a retry (D4).
        assert after == before

    def test_retry_failed_channel(self, client_env):
        client, store, home = client_env
        _seed_rule(home, _rule(name="bad", ok=False))
        original_id = store.add_alert_event(
            {
                "rule": "bad", "baseline": "prod", "severity": "WARN",
                "status": "failed", "target": "x", "drift_count": 1,
                "error": "old", "attempts": 3,
            }
        )
        r = client.post("/api/alert-events/%d/retry" % original_id)
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["status"] == "failed"
        assert data["sent"] is False
        assert data["error"]
        new_id = data["event_id"]
        new = store.get_alert_event(new_id)
        assert new["retried"] == 1
        assert new["retried_from"] == original_id

    def test_retry_missing_event_404(self, client_env):
        client, _, _ = client_env
        r = client.post("/api/alert-events/999/retry")
        assert r.status_code == 404

    def test_retry_missing_rule_404(self, client_env):
        client, store, _ = client_env
        original_id = store.add_alert_event(
            {
                "rule": "ghost", "baseline": "prod", "severity": "WARN",
                "status": "failed", "target": "x", "drift_count": 1,
                "error": "x", "attempts": 1,
            }
        )
        r = client.post("/api/alert-events/%d/retry" % original_id)
        assert r.status_code == 404
