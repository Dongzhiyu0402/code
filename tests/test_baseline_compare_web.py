"""cfgdrift v0.11.0 baseline version-compare Web tests (P0-2).

Covers:
- ``Store.list_baseline_versions`` — full history ordered by version ASC,
  empty list for an unknown name;
- ``CompareEngine.compare_baseline_versions`` — tree-level grouping
  (added / removed / changed), old/new values + line numbers, sensitive-value
  masking before ``to_dict``, all-empty groups for identical versions,
  ``ValueError`` for a missing version;
- the two new endpoints — ``GET /api/baselines/{name}/versions`` (404 when
  the name has no versions) and ``GET /api/baselines/compare`` (va==vb -> 400,
  missing version -> 404, identical -> empty groups);
- zero-noise: the existing ``/api/baselines`` response is unchanged.
"""

from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

from cfgdrift.core.masker import SensitiveMasker  # noqa: E402
from cfgdrift.storage.store import Store  # noqa: E402

try:
    from fastapi.testclient import TestClient  # noqa: E402

    WEB_OK = True
except Exception:  # pragma: no cover - optional dependency
    TestClient = None  # type: ignore
    WEB_OK = False


def _seed_baselines(store, root):
    """Create 3 versions of ``prod`` with known drift between v1/v2."""
    store.create_baseline(
        "prod", "initial", root, "yaml",
        {
            "app.yaml": {
                "server": {"port": 8080},
                "debug": True,
                "log": {"level": "info"},
                "password": "hunter2",
            }
        },
    )
    store.create_baseline(
        "prod", "second", root, "yaml",
        {
            "app.yaml": {
                "server": {"port": 9090},
                "debug": True,
                "log": {"level": "debug"},
                "password": "newpass",
            }
        },
    )
    store.create_baseline(
        "prod", "third (same as second)", root, "yaml",
        {
            "app.yaml": {
                "server": {"port": 9090},
                "debug": True,
                "log": {"level": "debug"},
                "password": "newpass",
            }
        },
    )


class TestStoreListVersions:
    def test_versions_ordered_asc(self, tmp_path):
        store = Store(str(tmp_path / "t.db"))
        try:
            _seed_baselines(store, str(tmp_path))
            versions = store.list_baseline_versions("prod")
            assert [v.version for v in versions] == [1, 2, 3]
            assert versions[0].description == "initial"
        finally:
            store.close()

    def test_unknown_name_empty(self, tmp_path):
        store = Store(str(tmp_path / "t.db"))
        try:
            assert store.list_baseline_versions("missing") == []
        finally:
            store.close()


class TestCompareBaselineVersions:
    def test_grouping_and_values(self, tmp_path):
        store = Store(str(tmp_path / "t.db"))
        try:
            _seed_baselines(store, str(tmp_path))
            from cfgdrift.core.compare import CompareEngine

            r = CompareEngine(store).compare_baseline_versions("prod", 1, 2)
            by_path = {}
            for item in r["changed"]:
                by_path[item["key_path"]] = item
            assert "server.port" in by_path
            assert by_path["server.port"]["old_value"] == 8080
            assert by_path["server.port"]["new_value"] == 9090
            assert "log.level" in by_path
            # grouping
            assert r["summary"]["added"] == len(r["added"])
            assert r["summary"]["changed"] == len(r["changed"])
            assert all(i["change_type"] == "added" for i in r["added"])
            assert all(i["change_type"] in ("modified", "type_changed") for i in r["changed"])
        finally:
            store.close()

    def test_masking_before_to_dict(self, tmp_path):
        store = Store(str(tmp_path / "t.db"))
        try:
            _seed_baselines(store, str(tmp_path))
            from cfgdrift.core.compare import CompareEngine

            masker = SensitiveMasker(keywords=["password"])
            r = CompareEngine(store).compare_baseline_versions(
                "prod", 1, 2, masker=masker
            )
            pw = next(i for i in r["changed"] if i["key_path"] == "password")
            assert pw["masked"] is True
        finally:
            store.close()

    def test_identical_versions_empty(self, tmp_path):
        store = Store(str(tmp_path / "t.db"))
        try:
            _seed_baselines(store, str(tmp_path))
            from cfgdrift.core.compare import CompareEngine

            r = CompareEngine(store).compare_baseline_versions("prod", 2, 3)
            assert r["added"] == [] and r["removed"] == [] and r["changed"] == []
            assert r["summary"]["total"] == 0
        finally:
            store.close()

    def test_missing_version_raises(self, tmp_path):
        store = Store(str(tmp_path / "t.db"))
        try:
            _seed_baselines(store, str(tmp_path))
            from cfgdrift.core.compare import CompareEngine

            with pytest.raises(ValueError):
                CompareEngine(store).compare_baseline_versions("prod", 1, 99)
        finally:
            store.close()


@pytest.mark.skipif(not WEB_OK, reason="fastapi/httpx unavailable")
class TestBaselineCompareWeb:
    @pytest.fixture()
    def web_env(self, tmp_path):
        from cfgdrift.web.app import create_app

        store = Store(str(tmp_path / "cfgdrift.db"))
        _seed_baselines(store, str(tmp_path))
        app = create_app(store, home=str(tmp_path / "home"))
        client = TestClient(app)
        yield client, store
        store.close()

    def test_versions_endpoint(self, web_env):
        client, _ = web_env
        r = client.get("/api/baselines/prod/versions")
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["name"] == "prod"
        assert [v["version"] for v in data["versions"]] == [1, 2, 3]
        assert all("created_at" in v and "description" in v for v in data["versions"])

    def test_versions_404_unknown(self, web_env):
        client, _ = web_env
        r = client.get("/api/baselines/nope/versions")
        assert r.status_code == 404
        assert r.json()["code"] == 2

    def test_compare_endpoint_groups(self, web_env):
        client, _ = web_env
        r = client.get("/api/baselines/compare?name=prod&va=1&vb=2")
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["name"] == "prod"
        assert data["version_a"] == 1 and data["version_b"] == 2
        assert data["summary"]["total"] == len(data["changed"])
        assert data["summary"]["total"] > 0

    def test_compare_same_version_400(self, web_env):
        client, _ = web_env
        r = client.get("/api/baselines/compare?name=prod&va=1&vb=1")
        assert r.status_code == 400
        assert r.json()["code"] == 2

    def test_compare_missing_version_404(self, web_env):
        client, _ = web_env
        r = client.get("/api/baselines/compare?name=prod&va=1&vb=99")
        assert r.status_code == 404
        assert r.json()["code"] == 2

    def test_compare_identical_empty(self, web_env):
        client, _ = web_env
        r = client.get("/api/baselines/compare?name=prod&va=2&vb=3")
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["summary"]["total"] == 0

    def test_existing_baselines_endpoint_unchanged(self, web_env):
        client, _ = web_env
        r = client.get("/api/baselines")
        assert r.status_code == 200
        data = r.json()["data"]
        assert "baselines" in data
        bl = data["baselines"][0]
        for key in ("id", "name", "version", "description", "created_at",
                    "scan_root", "format"):
            assert key in bl


class TestVersionCompareLabels:
    """Static guard on the frontend group-label direction (QA P1).

    Backend ``compare_baseline_versions(va, vb)`` diffs va as old / vb as new,
    so ``added`` = present in B only, ``removed`` = present in A only.  The
    SPA labels must follow that direction — an A/B flip here misleads which
    version added/removed what.
    """

    _JS = os.path.join(ROOT, "src", "cfgdrift", "web", "static", "app.js")

    def test_labels_follow_backend_direction(self):
        with open(self._JS, encoding="utf-8") as fh:
            js = fh.read()
        # added group labeled as B-has-A-lacks (not the old reversed text)
        assert 'groupCard("新增（B 有 A 无）", data.added || [])' in js
        assert 'groupCard("消失（A 有 B 无）", data.removed || [])' in js
        # the reversed (incorrect) labels must not reappear
        assert 'groupCard("新增（A 有 B 无）"' not in js
        assert 'groupCard("消失（B 有 A 无）"' not in js
