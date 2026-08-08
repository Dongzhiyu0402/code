"""cfgdrift v0.11.0 constraint-candidate promote tests (P0-3).

Covers:
- ``mining.mark_promoted`` — atomic ``status: promoted`` write-back, unknown
  id -> ``ValueError``, corrupt file never destroyed;
- ``web.candidates.load_candidates_view`` — missing file -> zero-noise empty
  state with a mining guide, valid file -> candidates + metadata;
- ``web.candidates.promote_candidate`` — writes ``enabled: false`` into
  ``constraints.yaml`` through the same ``ConstraintConfig.add_rule`` path as
  the CLI ``constraint add``, marks the candidate promoted, is idempotent
  (already-promoted or already-added), raises for an unknown id;
- the two endpoints — ``GET /api/constraint-candidates`` and
  ``POST /api/constraint-candidates/{id}/promote`` (404 unknown / 400 corrupt);
- the copy-command surface in app.js emits a legal ``constraint add --rule``.
"""

from __future__ import annotations

import os
import re
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

from cfgdrift.core.model import Constraint  # noqa: E402
from cfgdrift.rules.constraints import ConstraintConfig  # noqa: E402
from cfgdrift.rules.constraints import default_path as constraints_path  # noqa: E402
from cfgdrift.rules.mining import ConstraintMiner, MinedCandidate, _constraint_base  # noqa: E402
from cfgdrift.web.candidates import (  # noqa: E402
    candidates_path,
    load_candidates_view,
    promote_candidate,
)

try:
    from fastapi.testclient import TestClient  # noqa: E402

    WEB_OK = True
except Exception:  # pragma: no cover - optional dependency
    TestClient = None  # type: ignore
    WEB_OK = False

_STATIC_JS = os.path.join(ROOT, "src", "cfgdrift", "web", "static", "app.js")


def _sample_candidates():
    return [
        MinedCandidate(
            id="mined_enum_1",
            kind="enum",
            constraint=_constraint_base(
                "mined_enum_1", "enum", "WARN",
                "api_key 必须是 x / y 之一",
                keys=["api_key"], allowed=["x", "y"],
            ),
            metrics={"support": 12, "confidence": 1.0, "samples": 12, "source": "scans"},
        )
    ]


def _seed(home):
    path = candidates_path(home)
    ConstraintMiner.save_candidates(path, _sample_candidates(), source="scans", min_support=5)
    return path


class TestMarkPromoted:
    def test_writes_status_atomically(self, tmp_path):
        home = str(tmp_path)
        path = _seed(home)
        ConstraintMiner.mark_promoted(path, "mined_enum_1")
        cands = ConstraintMiner.load_candidates(path)
        assert cands[0].status == "promoted"
        assert not os.path.exists(path + ".tmp")
        # other payload fields preserved
        with open(path, encoding="utf-8") as fh:
            content = fh.read()
        assert "min_support: 5" in content

    def test_unknown_id_raises(self, tmp_path):
        home = str(tmp_path)
        path = _seed(home)
        with pytest.raises(ValueError):
            ConstraintMiner.mark_promoted(path, "nope")

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(ValueError):
            ConstraintMiner.mark_promoted(
                candidates_path(str(tmp_path)), "x"
            )

    def test_corrupt_file_preserved_on_error(self, tmp_path):
        home = str(tmp_path)
        path = _seed(home)
        original = open(path, encoding="utf-8").read()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{corrupt")
        with pytest.raises(ValueError):
            ConstraintMiner.mark_promoted(path, "mined_enum_1")
        # original file unchanged (load raises before any write)
        assert open(path, encoding="utf-8").read() == "{corrupt"


class TestLoadCandidatesView:
    def test_missing_file_empty_state(self, tmp_path):
        v = load_candidates_view(str(tmp_path))
        assert v["candidates"] == []
        assert "constraint mine" in v["message"]
        assert v["generated_at"] is None and v["source"] is None

    def test_valid_file_metadata(self, tmp_path):
        _seed(str(tmp_path))
        v = load_candidates_view(str(tmp_path))
        assert len(v["candidates"]) == 1
        assert v["candidates"][0]["id"] == "mined_enum_1"
        assert v["candidates"][0]["kind"] == "enum"
        assert v["candidates"][0]["constraint"]["enabled"] is False
        assert v["candidates"][0]["metrics"]["support"] == 12
        assert v["source"] == "scans"
        assert v["min_support"] == 5
        assert v["message"] is None


class TestPromoteCandidate:
    def test_promote_writes_enabled_false(self, tmp_path):
        home = str(tmp_path)
        _seed(home)
        result = promote_candidate(home, "mined_enum_1")
        assert result["status"] == "promoted"
        assert result["enabled"] is False
        assert result["constraint_id"] == "mined_enum_1"
        rules = ConstraintConfig.list_rules(constraints_path(home))
        assert len(rules) == 1
        assert rules[0].id == "mined_enum_1"
        assert rules[0].enabled is False
        assert rules[0].source == "user"
        # candidate marked
        assert ConstraintMiner.load_candidates(candidates_path(home))[0].status == "promoted"
        # atomic
        assert not os.path.exists(candidates_path(home) + ".tmp")
        assert not os.path.exists(constraints_path(home) + ".tmp")

    def test_promote_idempotent(self, tmp_path):
        home = str(tmp_path)
        _seed(home)
        promote_candidate(home, "mined_enum_1")
        r2 = promote_candidate(home, "mined_enum_1")
        assert r2["status"] == "promoted"
        assert len(ConstraintConfig.list_rules(constraints_path(home))) == 1

    def test_promote_unknown_id_raises(self, tmp_path):
        home = str(tmp_path)
        _seed(home)
        with pytest.raises(ValueError):
            promote_candidate(home, "nope")

    def test_from_dict_roundtrip(self):
        # candidate constraint dict validates through the CLI-shared builder
        c = _sample_candidates()[0]
        obj = Constraint.from_dict(c.constraint, source="user")
        assert obj.id == "mined_enum_1"
        assert obj.enabled is False
        assert obj.source == "user"


@pytest.mark.skipif(not WEB_OK, reason="fastapi/httpx unavailable")
class TestCandidatesWeb:
    @pytest.fixture()
    def web_env(self, tmp_path):
        from cfgdrift.web.app import create_app
        from cfgdrift.storage.store import Store

        home = str(tmp_path / "home")
        os.makedirs(home, exist_ok=True)
        store = Store(str(tmp_path / "cfgdrift.db"))
        app = create_app(store, home=home)
        client = TestClient(app)
        yield client, store, home
        store.close()

    def test_empty_state_no_error(self, web_env):
        client, _, _ = web_env
        r = client.get("/api/constraint-candidates")
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["candidates"] == []
        assert "constraint mine" in data["message"]

    def test_promote_via_api(self, web_env):
        client, _, home = web_env
        _seed(home)
        r = client.post("/api/constraint-candidates/mined_enum_1/promote")
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["status"] == "promoted" and data["enabled"] is False
        # constraints list now contains it
        cr = client.get("/api/constraints")
        ids = [c["id"] for c in cr.json()["data"]["constraints"]]
        assert "mined_enum_1" in ids
        # candidates endpoint shows promoted status
        cand = client.get("/api/constraint-candidates").json()["data"]["candidates"]
        assert cand[0]["status"] == "promoted"

    def test_promote_unknown_404(self, web_env):
        client, _, _ = web_env
        r = client.post("/api/constraint-candidates/nope/promote")
        assert r.status_code == 404
        assert r.json()["code"] == 2

    def test_promote_corrupt_file_400(self, web_env):
        client, _, home = web_env
        path = candidates_path(home)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{corrupt")
        r = client.post("/api/constraint-candidates/mined_enum_1/promote")
        assert r.status_code == 400
        assert r.json()["code"] == 2
        assert "corrupt" in r.json()["message"]
        # original corrupt file untouched (no write attempted)
        assert open(path, encoding="utf-8").read() == "{corrupt"


class TestCopyCommandSurface:
    def test_appjs_emits_legal_add_rule(self):
        with open(_STATIC_JS, encoding="utf-8") as fh:
            js = fh.read()
        assert "constraint add --rule" in js
        assert "data-copyrule" in js
        assert "data-promote" in js
        # promote button wired + confirm dialog present
        assert "window.confirm" in js
        assert "/promote" in js
        # the command is built as `cfgdrift constraint add --rule '<json>'`
        m = re.search(r"cfgdrift constraint add --rule '.*?\.rule.*?'", js)
        assert m is not None
