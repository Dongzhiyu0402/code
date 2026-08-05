"""cfgdrift v0.10.0 Web tests (P0-1/P0-2/P0-3): mute, ack, trend, compare.

Covers the v0.10.0 Web surface:
- ``PUT/DELETE /api/alerts/{name}/mute`` — persisted to alerts.yaml, 400 on
  invalid timestamp, 404 on unknown rule, interoperable with the CLI;
- ``POST /api/alert-events/{id}/ack`` — display-only ack, 404 when missing;
- ``GET /api/alert-trend`` — 14-day continuous series, SVG + days + total,
  ``rule`` filter, ``days`` clamped to [1, 30], non-numeric -> 400;
- ``GET /api/reports/compare`` — same grouping as ``report --diff``;
- ``GET /api/overview.muted_rules`` — 0 when nothing is muted, counts muted
  rules after a mute;
- the legacy ``GET /api/alert-events`` response keeps its ``{events, total}``
  shape (new columns are additive only);
- SPA static wiring: app.js exposes the new handlers / view hooks.
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
from cfgdrift.core.model import Severity  # noqa: E402
from cfgdrift.storage.store import Store  # noqa: E402

try:
    from fastapi.testclient import TestClient  # noqa: E402

    WEB_OK = True
except Exception:  # pragma: no cover - optional dependency
    TestClient = None  # type: ignore
    WEB_OK = False

_STATIC = os.path.join(
    ROOT, "src", "cfgdrift", "web", "static", "app.js"
)


def _seed_rule(home, name="drift-wx"):
    AlertConfig.save(
        os.path.join(home, "alerts.yaml"),
        [AlertRule(name=name, type="webhook", severity=Severity.WARN,
                   config={"url": "https://example.invalid/x"})],
    )


@pytest.fixture()
def web_env(tmp_path):
    from cfgdrift.web.app import create_app

    home = str(tmp_path / "home")
    os.makedirs(home, exist_ok=True)
    store = Store(str(tmp_path / "cfgdrift.db"))
    _seed_rule(home)
    app = create_app(store, home=home)
    client = TestClient(app)
    yield client, store, home
    store.close()


@pytest.mark.skipif(not WEB_OK, reason="fastapi/httpx unavailable")
class TestWebMute:
    def test_put_mute_persists(self, web_env):
        client, _, home = web_env
        r = client.put("/api/alerts/drift-wx/mute",
                       json={"until": "2099-01-01T00:00:00Z"})
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data["name"] == "drift-wx"
        assert data["mute_until"] == "2099-01-01T00:00:00+00:00"
        # persisted on disk + visible via /api/alerts
        assert AlertConfig.load(os.path.join(home, "alerts.yaml"))[0].mute_until \
            == "2099-01-01T00:00:00+00:00"
        rules = client.get("/api/alerts").json()["data"]["alerts"]
        assert rules[0]["mute_until"] == "2099-01-01T00:00:00+00:00"
        # overview counts it
        assert client.get("/api/overview").json()["data"]["muted_rules"] == 1

    def test_delete_mute(self, web_env):
        client, _, home = web_env
        client.put("/api/alerts/drift-wx/mute",
                   json={"until": "2099-01-01T00:00:00Z"})
        r = client.delete("/api/alerts/drift-wx/mute")
        assert r.status_code == 200
        assert r.json()["data"]["mute_until"] is None
        assert AlertConfig.load(os.path.join(home, "alerts.yaml"))[0].mute_until \
            is None
        assert client.get("/api/overview").json()["data"]["muted_rules"] == 0

    def test_invalid_until_400(self, web_env):
        client, _, _ = web_env
        r = client.put("/api/alerts/drift-wx/mute", json={"until": "garbage"})
        assert r.status_code == 400

    def test_unknown_rule_404(self, web_env):
        client, _, _ = web_env
        r = client.put("/api/alerts/nope/mute",
                       json={"until": "2099-01-01T00:00:00Z"})
        assert r.status_code == 404
        assert client.delete("/api/alerts/nope/mute").status_code == 404


@pytest.mark.skipif(not WEB_OK, reason="fastapi/httpx unavailable")
class TestWebAck:
    def test_ack_persists(self, web_env):
        client, store, _ = web_env
        eid = store.add_alert_event(
            {"rule": "drift-wx", "baseline": "prod", "severity": "WARN",
             "status": "sent", "target": "t", "drift_count": 1,
             "attempts": 1}
        )
        r = client.post("/api/alert-events/%d/ack" % eid)
        assert r.status_code == 200, r.text
        event = r.json()["data"]
        assert event["acked"] == 1 and event["acked_at"]
        # persisted: fresh fetch shows acked
        assert store.get_alert_event(eid)["acked"] == 1

    def test_ack_missing_404(self, web_env):
        client, _, _ = web_env
        assert client.post("/api/alert-events/99999/ack").status_code == 404

    def test_events_list_carries_ack_fields(self, web_env):
        client, store, _ = web_env
        store.add_alert_event(
            {"rule": "drift-wx", "baseline": "prod", "severity": "WARN",
             "status": "failed", "target": "t", "drift_count": 1,
             "attempts": 1, "error": "boom"}
        )
        r = client.get("/api/alert-events")
        assert r.status_code == 200
        data = r.json()["data"]
        assert set(data.keys()) == {"events", "total"}  # shape unchanged
        assert "acked" in data["events"][0]
        assert "acked_at" in data["events"][0]


@pytest.mark.skipif(not WEB_OK, reason="fastapi/httpx unavailable")
class TestWebTrend:
    def test_default_14_days_with_svg(self, web_env):
        client, store, _ = web_env
        store.add_alert_event(
            {"rule": "drift-wx", "baseline": "prod", "severity": "WARN",
             "status": "sent", "target": "t", "drift_count": 1, "attempts": 1}
        )
        r = client.get("/api/alert-trend")
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert len(data["days"]) == 14
        assert data["total"] >= 1
        assert data["rule"] == ""
        assert "<svg" in data["svg"]
        assert "sent" in data["svg"]  # legend
        assert all(set(d) >= {"date", "sent", "failed"} for d in data["days"])

    def test_rule_filter_and_empty_state(self, web_env):
        client, store, _ = web_env
        store.add_alert_event(
            {"rule": "drift-wx", "baseline": "prod", "severity": "WARN",
             "status": "sent", "target": "t", "drift_count": 1, "attempts": 1}
        )
        r = client.get("/api/alert-trend", params={"rule": "other"})
        data = r.json()["data"]
        assert data["total"] == 0
        assert "暂无告警事件" in data["svg"]
        assert data["rule"] == "other"
        # filter matches the events table source (same store aggregation)
        r = client.get("/api/alert-trend", params={"rule": "drift-wx"})
        assert r.json()["data"]["total"] == 1

    def test_days_clamp_and_bad_input(self, web_env):
        client, _, _ = web_env
        assert len(client.get("/api/alert-trend",
                              params={"days": "99"}).json()["data"]["days"]) == 30
        assert len(client.get("/api/alert-trend",
                              params={"days": "0"}).json()["data"]["days"]) == 1
        assert client.get("/api/alert-trend",
                          params={"days": "abc"}).status_code == 400


@pytest.mark.skipif(not WEB_OK, reason="fastapi/httpx unavailable")
class TestWebCompareEndpoint:
    def _seed(self, store):
        def scan(items):
            return store.add_scan(None, "manual", {
                "code": 0,
                "data": {"summary": {"modified": len(items),
                                     "total": len(items)},
                         "items": items},
                "message": "ok",
            })

        item = lambda k, sev="WARN", new="9090", f="app.yaml", line=5: {  # noqa: E731
            "key_path": k, "change_type": "modified", "severity": sev,
            "file": f, "old_value": "8080", "new_value": new,
            "old_type": "str", "new_type": "str", "line": line,
            "masked": False,
        }
        a = scan([item("services.web.ports[0]", sev="CRITICAL", f="server.conf",
                       line=12), item("api.timeout", f="api.yml", line=7)])
        b = scan([item("services.web.ports[0]", sev="CRITICAL", f="server.conf",
                       line=12),
                  item("debug.enabled", sev="INFO", new="true", f="app.yaml",
                       line=3)])
        return a, b

    def test_compare_groups(self, web_env):
        client, store, _ = web_env
        a, b = self._seed(store)
        r = client.get("/api/reports/compare",
                       params={"base_id": a, "target_id": b})
        assert r.status_code == 200, r.text
        diff = r.json()["data"]
        assert diff["summary"] == {"added": 1, "removed": 1, "changed": 0,
                                   "total": 2}
        assert diff["added"][0]["key_path"] == "api.timeout"
        assert diff["removed"][0]["key_path"] == "debug.enabled"

    def test_compare_no_difference(self, web_env):
        client, store, _ = web_env
        a, _ = self._seed(store)
        r = client.get("/api/reports/compare",
                       params={"base_id": a, "target_id": a})
        assert r.json()["data"]["summary"]["total"] == 0

    def test_compare_missing_scan_404(self, web_env):
        client, store, _ = web_env
        _, b = self._seed(store)
        r = client.get("/api/reports/compare",
                       params={"base_id": 99999, "target_id": b})
        assert r.status_code == 404


@pytest.mark.skipif(not WEB_OK, reason="fastapi/httpx unavailable")
class TestSpaWiring:
    def test_app_js_exposes_v100_handlers(self):
        with open(_STATIC, encoding="utf-8") as fh:
            js = fh.read()
        # P0-1: mute buttons, unmute, ack, overview muted count
        assert "data-alert-mute" in js
        assert "data-alert-unmute" in js
        assert "data-ack" in js
        assert "当前静默规则" in js
        # P0-2: trend card + rule dropdown + innerHTML embedding
        assert "trendRule" in js
        assert "trendSvg" in js
        assert "innerHTML = data.svg" in js
        assert "暂无告警事件" in js
        # P0-3: report compare panel
        assert "reportCompare" in js
        assert "renderDiffResult" in js
        assert "两次扫描无差异" in js
