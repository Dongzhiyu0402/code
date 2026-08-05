"""cfgdrift v0.10.0 report-diff tests (P0-3): diff_reports + CLI + Web.

Covers V10-P0-3 acceptance:
- ``diff_reports`` groups by the ``(file, key_path)`` fingerprint into
  added/removed/changed, where "changed" means severity OR new_value OR
  change_type differs (old_value is never compared across scans);
- identical inputs -> empty summary ("no difference");
- sensitive values are masked by the caller *before* diffing (masker is
  applied on the payloads, so masked values never re-leak);
- CLI ``report --diff A B``: exit 1 with differences / 0 identical /
  2 for a missing scan or argument conflicts (mutually exclusive with
  ``--scan-id`` / ``--html`` / ``--csv``); ``--json PATH`` writes the diff;
- Web ``GET /api/reports/compare`` returns the same document and 404s on a
  missing scan.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

from cfgdrift.core.comparediff import diff_reports  # noqa: E402
from cfgdrift.storage.store import Store  # noqa: E402

try:
    from fastapi.testclient import TestClient  # noqa: E402

    WEB_OK = True
except Exception:  # pragma: no cover - optional dependency
    TestClient = None  # type: ignore
    WEB_OK = False


def _item(key, sev="WARN", new="9090", change="modified", file="app.yaml",
          old="8080", line=5, masked=False):
    return {
        "key_path": key,
        "change_type": change,
        "severity": sev,
        "file": file,
        "old_value": old,
        "new_value": new,
        "old_type": "str",
        "new_type": "str",
        "line": line,
        "masked": masked,
    }


# ---------------------------------------------------------------------------
# diff_reports grouping semantics
# ---------------------------------------------------------------------------


class TestDiffReports:
    def test_added_removed_changed_groups(self):
        items_a = [
            _item("services.web.ports[0]", sev="CRITICAL", file="server.conf",
                  line=12),  # identical in B -> not a change
            _item("api.timeout", sev="WARN", new="30s", file="api.yml",
                  line=7),   # absent in B -> removed
        ]
        items_b = [
            _item("services.web.ports[0]", sev="CRITICAL", file="server.conf",
                  line=12),
            _item("debug.enabled", sev="INFO", change="added", file="app.yaml",
                  new="true", old=None, line=3),  # absent in A -> added
        ]
        diff = diff_reports(items_a, items_b, base_scan_id=1, target_scan_id=2)
        assert diff["base_scan_id"] == 1
        assert diff["target_scan_id"] == 2
        # A has api.timeout (absent in B) -> added; B has debug.enabled
        # (absent in A) -> removed; ports[0] identical -> not a change.
        assert [i["key_path"] for i in diff["added"]] == ["api.timeout"]
        assert [i["key_path"] for i in diff["removed"]] == ["debug.enabled"]
        assert diff["changed"] == []
        assert diff["summary"] == {"added": 1, "removed": 1, "changed": 0,
                                   "total": 2}

    def test_severity_change_is_changed(self):
        items_a = [_item("logging.level", sev="WARN", new="info")]
        items_b = [_item("logging.level", sev="CRITICAL", new="info")]
        diff = diff_reports(items_a, items_b)
        assert diff["summary"]["changed"] == 1
        entry = diff["changed"][0]
        assert entry["severity_changed"] is True
        assert entry["value_changed"] is False
        assert entry["item_a"]["severity"] == "WARN"
        assert entry["item_b"]["severity"] == "CRITICAL"

    def test_value_change_is_changed(self):
        items_a = [_item("logging.level", new="info")]
        items_b = [_item("logging.level", new="debug")]
        diff = diff_reports(items_a, items_b)
        assert diff["summary"]["changed"] == 1
        entry = diff["changed"][0]
        assert entry["severity_changed"] is False
        assert entry["value_changed"] is True

    def test_change_type_change_is_changed(self):
        # The same key switching kind (modified -> added) is a "change", not
        # add+remove — the fingerprint deliberately excludes change_type.
        items_a = [_item("x.y", change="modified")]
        items_b = [_item("x.y", change="added")]
        diff = diff_reports(items_a, items_b)
        assert diff["added"] == []
        assert diff["removed"] == []
        assert diff["summary"]["changed"] == 1

    def test_identical_is_no_difference(self):
        items = [_item("a"), _item("b", sev="CRITICAL")]
        diff = diff_reports(list(items), list(items))
        assert diff["summary"]["total"] == 0
        assert diff["added"] == [] and diff["removed"] == [] and diff["changed"] == []

    def test_masked_values_compared_as_masked(self):
        # The caller masks first (mask_payload) — a sensitive key that is
        # masked on both sides compares equal and is not flagged as changed.
        items_a = [_item("db.password", old="s3cr3t", new="n3wp4ss",
                         masked=True)]
        items_b = [_item("db.password", old="s3cr3t", new="n3wp4ss",
                         masked=True)]
        # emulate display masking: values already replaced by the mask text
        items_a[0]["new_value"] = "******"
        items_b[0]["new_value"] = "******"
        diff = diff_reports(items_a, items_b)
        assert diff["summary"]["total"] == 0


# ---------------------------------------------------------------------------
# CLI report --diff
# ---------------------------------------------------------------------------


def _run_cli(home, store_path, args):
    import subprocess

    env = dict(os.environ)
    env["CFGDRIFT_HOME"] = home
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    cmd = [sys.executable, "-m", "cfgdrift.cli", "--store", store_path] + args
    return subprocess.run(cmd, capture_output=True, text=True, env=env,
                          timeout=120)


def _seed_store(store):
    def scan(items):
        return store.add_scan(None, "manual", {
            "code": 0,
            "data": {"summary": {"modified": len(items),
                                 "total": len(items)},
                     "items": items},
            "message": "ok",
        })

    a = scan([
        _item("services.web.ports[0]", sev="CRITICAL", file="server.conf",
              line=12),
        _item("api.timeout", file="api.yml", line=7),
    ])
    b = scan([
        _item("services.web.ports[0]", sev="CRITICAL", file="server.conf",
              line=12),
        _item("debug.enabled", sev="INFO", change="added", file="app.yaml",
              old=None, new="true", line=3),
    ])
    return a, b


class TestCliReportDiff:
    def test_diff_exit_codes(self, tmp_path):
        home = str(tmp_path / "home")
        os.makedirs(home, exist_ok=True)
        store = Store(str(tmp_path / "db.sqlite"))
        a, b = _seed_store(store)
        store.close()
        sp = str(tmp_path / "db.sqlite")
        # differences -> 1 with the three groups
        r = _run_cli(home, sp, ["report", "--diff", str(a), str(b)])
        assert r.returncode == 1, (r.stdout, r.stderr)
        assert "新增（A 有 B 无，1 项）" in r.stdout
        assert "消失（B 有 A 无，1 项）" in r.stdout
        assert "debug.enabled" in r.stdout and "api.timeout" in r.stdout
        # identical -> 0
        r = _run_cli(home, sp, ["report", "--diff", str(a), str(a)])
        assert r.returncode == 0
        assert "两次扫描无差异" in r.stdout
        # missing scan -> 2
        r = _run_cli(home, sp, ["report", "--diff", "99999", str(a)])
        assert r.returncode == 2
        # argument conflicts -> 2
        r = _run_cli(home, sp, ["report", "--diff", str(a), str(b),
                                "--html", "x.html"])
        assert r.returncode == 2
        r = _run_cli(home, sp, ["report", "--diff", str(a), str(b),
                                "--scan-id", str(a)])
        assert r.returncode == 2

    def test_diff_json_output(self, tmp_path):
        home = str(tmp_path / "home")
        os.makedirs(home, exist_ok=True)
        store = Store(str(tmp_path / "db.sqlite"))
        a, b = _seed_store(store)
        store.close()
        out = str(tmp_path / "diff.json")
        r = _run_cli(home, str(tmp_path / "db.sqlite"),
                     ["report", "--diff", str(a), str(b), "--json", out])
        assert r.returncode == 1
        with open(out, encoding="utf-8") as fh:
            diff = json.load(fh)
        assert diff["base_scan_id"] == a and diff["target_scan_id"] == b
        assert diff["summary"]["added"] == 1
        assert diff["summary"]["removed"] == 1

    def test_single_report_unchanged(self, tmp_path):
        # Zero-noise: plain `report` (no --diff) still works as before.
        home = str(tmp_path / "home")
        os.makedirs(home, exist_ok=True)
        store = Store(str(tmp_path / "db.sqlite"))
        a, _ = _seed_store(store)
        store.close()
        r = _run_cli(home, str(tmp_path / "db.sqlite"),
                     ["report", "--scan-id", str(a)])
        assert r.returncode == 1  # scan a has 2 drift items
        assert "server.port" in r.stdout or "services.web.ports[0]" in r.stdout


# ---------------------------------------------------------------------------
# Web /api/reports/compare
# ---------------------------------------------------------------------------


@pytest.fixture()
def web_env(tmp_path):
    from cfgdrift.web.app import create_app

    home = str(tmp_path / "home")
    os.makedirs(home, exist_ok=True)
    store = Store(str(tmp_path / "cfgdrift.db"))
    a, b = _seed_store(store)
    app = create_app(store, home=home)
    client = TestClient(app)
    yield client, a, b
    store.close()


@pytest.mark.skipif(not WEB_OK, reason="fastapi/httpx unavailable")
class TestWebReportCompare:
    def test_compare_endpoint_matches_cli(self, web_env):
        client, a, b = web_env
        r = client.get("/api/reports/compare",
                       params={"base_id": a, "target_id": b})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["code"] == 0
        diff = body["data"]
        assert diff["base_scan_id"] == a and diff["target_scan_id"] == b
        # same grouping as the CLI (byte-identical diff_reports call)
        assert diff["summary"] == {"added": 1, "removed": 1, "changed": 0,
                                   "total": 2}
        # A owns api.timeout (absent in B) -> added; B owns debug.enabled.
        assert diff["added"][0]["key_path"] == "api.timeout"
        assert diff["removed"][0]["key_path"] == "debug.enabled"

    def test_identical_scan_no_difference(self, web_env):
        client, a, _ = web_env
        r = client.get("/api/reports/compare",
                       params={"base_id": a, "target_id": a})
        assert r.json()["data"]["summary"]["total"] == 0

    def test_missing_scan_404(self, web_env):
        client, _, b = web_env
        r = client.get("/api/reports/compare",
                       params={"base_id": 99999, "target_id": b})
        assert r.status_code == 404
