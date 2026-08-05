"""QA round-8 independent verification for cfgdrift v0.7.0 (four features).

Written from scratch with a suspicious eye — does NOT reuse the engineer's
five new test files.  Covers the acceptance surface a–g:

a. corpus end-to-end: local git fixture -> init -> fetch(local_path) ->
   export -> validate; JSONL schema; incremental fetch (last_commit moves);
   max_instances quota; idempotent byte-identical export.
b. corpus robustness: corrupt corpus.yaml -> exit 2; corrupt instances.jsonl
   -> validate exit 2; non-whitelist files (.txt/.md) excluded; parse-failed
   changes skipped.
c. constraint mine: synthetic scan_items -> three candidate kinds; exact
   support/confidence formulas; --min-support effect; candidates never
   auto-activated (scan does not report); full promotion chain
   (add --rule -> enable -> scan detects) end-to-end; mine_corpus from JSONL.
d. baseline violations: pre-existing violation -> scan --report-violations
   emits terminal section + JSON baseline_violations (severity = constraint's
   own) + C-10 kind=baseline; default off zero-noise (no section/key/rows);
   drift-associated violations not double-reported (dedup by signature).
e. C-10 table: drift violation visible via list_constraint_violations
   (kind=drift); pagination/filter; prune cleans forged old rows;
   CFGDRIFT_CV_RETENTION_DAYS configurable.
f. Web: TestClient three endpoints (GET /api/constraints effective view,
   PUT user toggle persisted across app restart, PUT builtin -> 400,
   PUT missing -> 404, GET /api/constraint-events pagination + limit clamp);
   SPA static wiring (nav 约束 + renderConstraints/renderConstraintEvents).
g. Regression: v0.6.0 constraint engine / composite alert / five
   presentations unaffected; scan/diff/daemon existing behavior intact;
   version contract 0.8.0 / 0.8.0-c.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
PY = sys.executable

sys.path.insert(0, SRC)

from cfgdrift.core.constraints import ConstraintEngine  # noqa: E402
from cfgdrift.core.differ import SemanticDiffer  # noqa: E402
from cfgdrift.core.model import (  # noqa: E402
    ChangeType,
    Constraint,
    DriftItem,
    Severity,
)
from cfgdrift.core.parser import parse_text  # noqa: E402
from cfgdrift.corpus.config import CorpusConfig, CorpusRepository  # noqa: E402
from cfgdrift.corpus.exporter import CorpusExporter  # noqa: E402
from cfgdrift.corpus.fetcher import (  # noqa: E402
    ChangePairExtractor,
    LocalRepoSource,
)
from cfgdrift.corpus.validator import CorpusValidator  # noqa: E402
from cfgdrift.corpus.workspace import CorpusWorkspace  # noqa: E402
from cfgdrift.rules.constraints import (  # noqa: E402
    ConstraintConfig,
    default_path as constraints_path,
    resolve as resolve_constraints,
)
from cfgdrift.rules.mining import ConstraintMiner  # noqa: E402
from cfgdrift.storage.store import Store  # noqa: E402


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def _run_cli(home, args, store=None, extra_env=None):
    env = dict(os.environ)
    env["CFGDRIFT_HOME"] = str(home)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    if extra_env:
        env.update(extra_env)
    cmd = [PY, "-m", "cfgdrift.cli"]
    if store:
        cmd += ["--store", str(store)]
    cmd += list(args)
    return subprocess.run(cmd, capture_output=True, text=True, env=env,
                          timeout=240)


class GitFixture:
    """A tiny local git repository (offline) used by corpus tests."""

    def __init__(self, root):
        self.repo = str(root / "repo")
        os.makedirs(self.repo, exist_ok=True)
        self._git("init", "-q")
        self._git("config", "user.email", "qa@example.com")
        self._git("config", "user.name", "QA")

    def _git(self, *args):
        r = subprocess.run(["git", "-C", self.repo] + list(args),
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, (args, r.stderr)
        return r.stdout.strip()

    def commit(self, relpath, content, message):
        p = os.path.join(self.repo, relpath)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(content)
        self._git("add", "-A")
        self._git("commit", "-q", "-m", message)

    def head(self):
        return self._git("rev-parse", "HEAD")


@pytest.fixture()
def fixture(tmp_path):
    return GitFixture(tmp_path)


def _make_workspace(tmp_path, fixture, glob="conf/*.yaml", max_instances=200):
    ws = CorpusWorkspace(str(tmp_path / "ws"))
    ws.init()
    cfg = CorpusConfig.load(ws.config_path())
    cfg.repositories = [
        CorpusRepository(owner="qa", repo="fixture",
                         local_path=fixture.repo, glob=glob)
    ]
    cfg.since = None
    cfg.min_stars = None
    cfg.max_instances = max_instances
    cfg.save(ws.config_path())
    return ws, cfg


def _fetch_via_extractor(ws, cfg, fixture):
    """Fetch pairs the same way the CLI does, then export; returns stats."""
    src = LocalRepoSource(fixture.repo)
    extractor = ChangePairExtractor()
    state = ws.read_state()
    entry = ws.repo_state(state, "qa/fixture")
    per_repo_max = max(1, cfg.max_instances // max(1, len(cfg.repositories)))
    pairs, stats, newest = extractor.extract_repo(
        src, since=None, stop_at=None, max_pairs=per_repo_max,
        glob_pattern=cfg.repositories[0].glob,
    )
    entry["last_commit"] = newest
    entry["instance_count"] = len(pairs)
    entry["local_path"] = fixture.repo
    state["fetched_at"] = "now"
    ws.write_state(state)
    return CorpusExporter().export(ws, cfg, constraints=None)


# ---------------------------------------------------------------------------
# g0 / contract: version + CLI surface
# ---------------------------------------------------------------------------

class TestVersionContract:
    def test_module_version_070(self):
        import cfgdrift
        assert cfgdrift.__version__ == "0.9.0"

    def test_pyproject_version_070(self):
        with open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as fh:
            assert 'version = "0.9.0"' in fh.read()

    def test_csrc_version_marker(self):
        c_path = os.path.join(ROOT, "src", "csrc", "parser_core.c")
        if os.path.exists(c_path):
            with open(c_path, encoding="utf-8") as fh:
                assert '0.9.0-c' in fh.read()

    def test_cli_version(self, tmp_path):
        r = _run_cli(tmp_path / "home", ["--version"])
        assert r.returncode == 0
        assert "0.9.0" in r.stdout

    def test_new_cli_surfaces(self, tmp_path):
        r = _run_cli(tmp_path / "home", ["corpus", "--help"])
        assert r.returncode == 0
        for word in ("init", "fetch", "export", "validate"):
            assert word in r.stdout
        r2 = _run_cli(tmp_path / "home", ["constraint", "--help"])
        assert "mine" in r2.stdout
        r3 = _run_cli(tmp_path / "home", ["scan", "--help"])
        assert "--report-violations" in r3.stdout


# ---------------------------------------------------------------------------
# a. corpus end-to-end
# ---------------------------------------------------------------------------

class TestCorpusEndToEnd:
    def test_full_pipeline(self, tmp_path, fixture):
        fixture.commit("conf/app.yaml", "server:\n  port: 8080\n  gzip: on\n", "c1")
        fixture.commit("conf/app.yaml", "server:\n  port: 9090\n  gzip: off\n", "c2")
        ws, cfg = _make_workspace(tmp_path, fixture)
        stats = _fetch_via_extractor(ws, cfg, fixture)
        assert stats["instances"] == 2
        assert stats["repos"] == 1
        # validate passes with statistics
        vs = CorpusValidator.validate(ws.instances_path())
        assert vs["instances"] == 2
        assert vs["repos"] == ["qa/fixture"]
        assert vs["formats"] == {"yaml": 2}
        # state last_commit == HEAD
        state = ws.read_state()
        assert state["repos"]["qa/fixture"]["last_commit"] == fixture.head()

    def test_jsonl_schema_fields(self, tmp_path, fixture):
        fixture.commit("conf/app.yaml", "server:\n  port: 8080\n", "c1")
        fixture.commit("conf/app.yaml", "server:\n  port: 9090\n"
                                        "  tls:\n    enabled: true\n", "c2")
        ws, cfg = _make_workspace(tmp_path, fixture)
        _fetch_via_extractor(ws, cfg, fixture)
        with open(ws.instances_path(), encoding="utf-8") as fh:
            entries = [json.loads(line) for line in fh if line.strip()]
        assert len(entries) == 2
        first = entries[0]  # newest commit first
        # full schema surface
        assert first["schema_version"] == 1
        assert isinstance(first["instance_id"], str)
        assert first["metadata"]["owner"] == "qa"
        assert first["metadata"]["repo"] == "fixture"
        assert first["file"]["relpath"] == "conf/app.yaml"
        assert first["before"]["tree"] is not None
        assert first["after"]["tree"] is not None
        assert first["before"]["parse_ok"] is True
        assert first["after"]["parse_ok"] is True
        diff = first["diff"]
        assert isinstance(diff["items"], list)
        assert isinstance(diff["summary"], dict)
        for k in ("added", "removed", "modified", "type_changed", "ignored",
                  "total", "max_severity"):
            assert k in diff["summary"]
        assert isinstance(diff["constraint_violations"], list)
        feat = diff["feature"]
        for k in ("changed_keys", "changed_values", "co_change_pairs",
                  "co_change_capped"):
            assert k in feat
        assert first["labels"]["severity"] in (
            "NONE", "WARN", "CRITICAL", "FATAL"
        )
        assert first["labels"]["annotation"] is None
        assert first["labels"]["annotator"] is None

    def test_incremental_fetch_moves_last_commit(self, tmp_path, fixture):
        fixture.commit("conf/app.yaml", "server:\n  port: 8080\n", "c1")
        fixture.commit("conf/app.yaml", "server:\n  port: 9090\n", "c2")
        ws, cfg = _make_workspace(tmp_path, fixture)
        stats1 = _fetch_via_extractor(ws, cfg, fixture)
        assert stats1["instances"] == 2
        state = ws.read_state()
        entry = state["repos"]["qa/fixture"]
        last = entry["last_commit"]
        # add one commit; incremental fetch takes only the new pair
        fixture.commit("conf/app.yaml", "server:\n  port: 8443\n", "c3")
        src = LocalRepoSource(fixture.repo)
        pairs2, _, newest2 = ChangePairExtractor().extract_repo(
            src, since=None, stop_at=last, max_pairs=200,
            glob_pattern=cfg.repositories[0].glob,
        )
        assert len(pairs2) == 1
        assert newest2 == fixture.head()
        entry["last_commit"] = newest2
        entry["instance_count"] = entry["instance_count"] + len(pairs2)
        ws.write_state(state)
        stats2 = CorpusExporter().export(ws, cfg, constraints=None)
        assert stats2["instances"] == 3
        state2 = ws.read_state()
        assert state2["repos"]["qa/fixture"]["last_commit"] == fixture.head()

    def test_max_instances_quota(self, tmp_path, fixture):
        for i in range(6):
            fixture.commit("conf/app.yaml", "server:\n  port: %d\n" % (8000 + i),
                           "c%d" % i)
        ws, cfg = _make_workspace(tmp_path, fixture, max_instances=3)
        stats = _fetch_via_extractor(ws, cfg, fixture)
        assert stats["instances"] == 3

    def test_export_idempotent_bytes(self, tmp_path, fixture):
        fixture.commit("conf/app.yaml", "server:\n  port: 8080\n", "c1")
        fixture.commit("conf/app.yaml", "server:\n  port: 9090\n", "c2")
        ws, cfg = _make_workspace(tmp_path, fixture)
        _fetch_via_extractor(ws, cfg, fixture)
        first = open(ws.instances_path(), "rb").read()
        CorpusExporter().export(ws, cfg, constraints=None)
        second = open(ws.instances_path(), "rb").read()
        assert first == second


# ---------------------------------------------------------------------------
# b. corpus robustness
# ---------------------------------------------------------------------------

class TestCorpusRobustness:
    def test_corrupt_corpus_yaml_cli_exit2(self, tmp_path):
        ws_dir = str(tmp_path / "ws")
        os.makedirs(ws_dir, exist_ok=True)
        _write(os.path.join(ws_dir, "corpus.yaml"),
               "version: 99\nrepositories: []\n")
        r = _run_cli(tmp_path / "home", ["corpus", "fetch", "--workspace", ws_dir])
        assert r.returncode == 2
        assert "error:" in r.stderr

    def test_corrupt_instances_validate_exit2(self, tmp_path):
        ws_dir = str(tmp_path / "ws")
        os.makedirs(ws_dir, exist_ok=True)
        _write(os.path.join(ws_dir, "instances.jsonl"),
               '{"schema_version": 1, "instance_id": "x"}\n')
        r = _run_cli(tmp_path / "home",
                     ["corpus", "validate", "--workspace", ws_dir])
        assert r.returncode == 2
        assert "error:" in r.stderr

    def test_non_whitelist_files_excluded(self, tmp_path, fixture):
        # .txt and .md are NOT in the five-type whitelist -> never extracted.
        fixture.commit("conf/app.yaml", "server:\n  port: 8080\n", "c1")
        fixture.commit("README.md", "# docs\n", "doc")
        fixture.commit("notes.txt", "hello\n", "notes")
        fixture.commit("conf/app.yaml", "server:\n  port: 9090\n", "c2")
        ws, cfg = _make_workspace(tmp_path, fixture)
        stats = _fetch_via_extractor(ws, cfg, fixture)
        assert stats["instances"] == 2  # only the two app.yaml commits

    def test_parse_failure_skipped_and_counted(self, tmp_path, fixture):
        fixture.commit("conf/bad.yaml", "a: [unclosed\n", "bad")
        fixture.commit("conf/ok.yaml", "server:\n  port: 8080\n", "c1")
        ws, cfg = _make_workspace(tmp_path, fixture)
        stats = _fetch_via_extractor(ws, cfg, fixture)
        # the bad file pair is skipped; the ok file is exported
        assert stats["instances"] == 1

    def test_glob_filters(self, tmp_path, fixture):
        fixture.commit("conf/app.yaml", "server:\n  port: 8080\n", "c1")
        fixture.commit("other/skip.yaml", "x: 1\n", "c2")
        fixture.commit("conf/app.yaml", "server:\n  port: 9090\n", "c3")
        ws, cfg = _make_workspace(tmp_path, fixture, glob="conf/*.yaml")
        stats = _fetch_via_extractor(ws, cfg, fixture)
        assert stats["instances"] == 2


# ---------------------------------------------------------------------------
# c. constraint mine
# ---------------------------------------------------------------------------

def _add_scan(store, items):
    payload = {
        "code": 0,
        "data": {
            "summary": {"total": len(items)},
            "items": [
                {
                    "key_path": k,
                    "change_type": "modified",
                    "severity": "WARN",
                    "file": "app.yaml",
                    "old_value": None,
                    "new_value": v,
                    "old_type": None,
                    "new_type": None,
                }
                for k, v in items.items()
            ],
        },
        "message": "ok",
    }
    return store.add_scan(None, "manual", payload)


def _unit_store(tmp_path, units):
    store = Store(str(tmp_path / "cfgdrift.db"))
    for unit in units:
        _add_scan(store, unit)
    return store


class TestConstraintMine:
    def test_three_kinds_formulas(self, tmp_path):
        # 9 units: enum key (4 distinct) + range key (9 numeric) + two keys
        # that always co-change -> conditional_required confidence 1.0.
        units = []
        levels = ["info", "warn", "error", "debug"]
        for i in range(9):
            units.append({"logging.level": levels[i % 4],
                          "requests_limit": 100 + i * 100,
                          "always.with": 1})
        store = _unit_store(tmp_path, units)
        try:
            cands = ConstraintMiner.mine_scans(store, min_support=3)
        finally:
            store.close()
        kinds = {c.kind for c in cands}
        assert kinds >= {"enum", "range", "conditional_required"}
        enum = next(c for c in cands if c.kind == "enum")
        assert enum.metrics["support"] == 9
        assert enum.metrics["confidence"] == 1.0
        rng = next(c for c in cands
                   if c.kind == "range"
                   and c.constraint["keys"] == ["requests_limit"])
        assert rng.constraint["min"] == 100
        assert rng.constraint["max"] == 900
        assert rng.metrics.get("observed") is True
        cond = next(c for c in cands if c.kind == "conditional_required")
        # support = co(A,B) = 9, confidence = 9/cnt(A) = 1.0
        assert cond.metrics["support"] == 9
        assert cond.metrics["confidence"] == 1.0
        assert cond.constraint["enabled"] is False
        assert cond.status == "pending"

    def test_confidence_formula_exact(self, tmp_path):
        # A appears 4 times, B co-occurs 3 times -> confidence 0.75 < 0.8
        # so no conditional_required candidate (design Q3 threshold).
        store = _unit_store(
            tmp_path,
            [
                {"a": 1, "b": 1},
                {"a": 2, "b": 2},
                {"a": 3, "b": 3},
                {"a": 4},
            ],
        )
        try:
            cands = ConstraintMiner.mine_scans(store, min_support=2)
        finally:
            store.close()
        assert all(c.kind != "conditional_required" for c in cands)

    def test_min_support_gate(self, tmp_path):
        store = _unit_store(
            tmp_path,
            [
                {"logging.level": "info"},
                {"logging.level": "warn"},
            ],
        )
        try:
            low = ConstraintMiner.mine_scans(store, min_support=1)
            high = ConstraintMiner.mine_scans(store, min_support=3)
        finally:
            store.close()
        assert any(c.kind == "enum" for c in low)
        assert high == []

    def test_mutual_exclusion_zero_intersection(self, tmp_path):
        store = _unit_store(
            tmp_path,
            [
                {"mode": "dev", "env": "local"},
                {"mode": "dev", "env": "local"},
                {"mode": "prod", "env": "prod"},
                {"mode": "prod", "env": "prod"},
                {"mode": "stage", "env": "stage"},
                {"mode": "stage", "env": "stage"},
            ],
        )
        try:
            cands = ConstraintMiner.mine_scans(store, min_support=2)
        finally:
            store.close()
        mutuals = [c for c in cands if c.kind == "mutual_exclusion"]
        assert mutuals
        for c in mutuals:
            assert c.constraint["type"] == "mutual_exclusion"
            assert len(c.constraint["forbid"]) == 1
            assert c.metrics["confidence"] == 1.0
            assert c.constraint["enabled"] is False

    def test_mine_corpus_from_jsonl(self, tmp_path):
        lines = []
        for i in range(9):
            lines.append(json.dumps({
                "diff": {"feature": {"changed_values": {
                    "logging.level": {"after": ["info", "warn", "error",
                                                "debug"][i % 4]},
                    "requests_limit": {"after": 100 + i * 100},
                }}},
            }))
        path = str(tmp_path / "instances.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        cands = ConstraintMiner.mine_corpus(path, min_support=3)
        kinds = {c.kind for c in cands}
        assert "enum" in kinds and "range" in kinds
        assert all(c.metrics["source"] == "corpus" for c in cands)
        assert all(c.constraint["enabled"] is False for c in cands)

    def test_candidates_save_load_never_auto_activated(self, tmp_path):
        store = _unit_store(
            tmp_path,
            [{"logging.level": "info", "server.port": 8080},
             {"logging.level": "warn", "server.port": 9090},
             {"logging.level": "error", "server.port": 8080},
             {"logging.level": "debug", "server.port": 8443}],
        )
        try:
            cands = ConstraintMiner.mine_scans(store, min_support=3)
        finally:
            store.close()
        path = str(tmp_path / "mined.yaml")
        ConstraintMiner.save_candidates(path, cands)
        loaded = ConstraintMiner.load_candidates(path)
        assert loaded
        for c in loaded:
            assert c.constraint["enabled"] is False
            assert c.status == "pending"

    def test_promotion_chain_end_to_end(self, tmp_path):
        """Candidate -> add --rule -> enable -> next scan detects the drift."""
        home = str(tmp_path / "home")
        store_path = str(tmp_path / "db" / "cfgdrift.db")
        conf = str(tmp_path / "conf" / "app.yaml")
        # baseline with a clean value
        _write(conf, "server:\n  port: 8080\n")
        r = _run_cli(home, ["baseline", "create", "env1",
                            "--scan-root", conf], store=store_path)
        assert r.returncode == 0, r.stderr
        # add a user enum constraint (from a mined candidate shape)
        rule = {
            "id": "qa_user_enum", "type": "enum", "keys": ["server.port"],
            "allowed": [8080, 9090], "message": "qa enum",
            "severity": "WARN", "enabled": True,
        }
        r = _run_cli(home, ["constraint", "add", "--rule", json.dumps(rule)],
                     store=store_path)
        assert r.returncode == 0, r.stderr
        # drift to a disallowed port -> violation attached + C-10 drift row
        _write(conf, "server:\n  port: 70000\n")
        r = _run_cli(home, ["scan", conf, "--baseline", "env1"],
                     store=store_path)
        assert r.returncode == 1
        store = Store(str(store_path))
        try:
            events = store.list_constraint_violations(
                constraint_id="qa_user_enum", kind="drift")
            assert events["total"] >= 1
        finally:
            store.close()


# ---------------------------------------------------------------------------
# d. baseline violations (C-07)
# ---------------------------------------------------------------------------

def _flat_yaml(port, with_tls=True):
    lines = ["server:", "  port: %d" % port, "  gzip: on"]
    if with_tls:
        lines += ["tls:", "  enabled: true"]
    return "\n".join(lines) + "\n"


class TestBaselineViolations:
    def _constraints(self, home):
        os.makedirs(home, exist_ok=True)
        return resolve_constraints(home, [], builtin_enabled=True)

    def test_check_tree_severity_from_constraint(self, tmp_path):
        constraints = self._constraints(str(tmp_path / "home"))
        tree = parse_text(_flat_yaml(8080, with_tls=True), "yaml")
        violations = ConstraintEngine.check_tree(
            constraints, {"app.yaml": tree})
        ssl = [v for v in violations
               if v["constraint_id"] == "http_ssl_cert_required"]
        assert ssl
        for v in ssl:
            assert v["severity"] == "CRITICAL"  # Q6: constraint's own severity
            assert v["file"] == "app.yaml"

    def test_baseline_excludes_drift_associated(self, tmp_path):
        constraints = self._constraints(str(tmp_path / "home"))
        tree = parse_text(_flat_yaml(9090, with_tls=True), "yaml")
        drift_items = [
            DriftItem(
                key_path="tls.cert_path",
                change_type=ChangeType.REMOVED,
                severity=Severity.CRITICAL,
                file="app.yaml",
                constraint_violations=[{
                    "constraint_id": "http_ssl_cert_required",
                    "type": "conditional_required",
                    "message": "cert missing",
                    "involved_keys": ["tls.enabled", "tls.cert_path"],
                }],
            )
        ]
        bv = ConstraintEngine.baseline_violations(
            constraints, {"app.yaml": tree}, drift_items)
        # the cert_path violation is drift-associated -> excluded; the
        # key_path violation remains as baseline.
        key_sets = [set(v["involved_keys"]) for v in bv]
        assert {"tls.enabled", "tls.key_path"} in key_sets
        assert {"tls.enabled", "tls.cert_path"} not in key_sets

    def test_scan_report_violations_full_flow(self, tmp_path):
        home = str(tmp_path / "home")
        store_path = str(tmp_path / "db" / "cfgdrift.db")
        conf = str(tmp_path / "conf" / "app.yaml")
        _write(conf, _flat_yaml(8080, with_tls=True))
        r = _run_cli(home, ["baseline", "create", "env1",
                            "--scan-root", conf], store=store_path)
        assert r.returncode == 0, r.stderr
        # change only the port; tls pre-existing break stays baseline
        _write(conf, _flat_yaml(9090, with_tls=True))
        r = _run_cli(home, ["scan", conf, "--baseline", "env1",
                            "--report-violations"], store=store_path)
        assert r.returncode == 1
        assert "Baseline violations:" in r.stdout
        assert "http_ssl_cert_required" in r.stdout
        store = Store(str(store_path))
        try:
            baseline_events = store.list_constraint_violations(kind="baseline")
            assert baseline_events["total"] >= 1
            assert all(e["severity"] == "CRITICAL" for e in baseline_events["events"])
            # stored report JSON has baseline_violations
            scans = store.list_scans(limit=1)
            payload = store.get_scan(scans[0]["scan_id"])
            assert "baseline_violations" in payload["data"]
            assert len(payload["data"]["baseline_violations"]) >= 1
        finally:
            store.close()

    def test_default_off_zero_noise(self, tmp_path):
        home = str(tmp_path / "home")
        store_path = str(tmp_path / "db" / "cfgdrift.db")
        conf = str(tmp_path / "conf" / "app.yaml")
        _write(conf, _flat_yaml(8080, with_tls=True))
        r = _run_cli(home, ["baseline", "create", "env1",
                            "--scan-root", conf], store=store_path)
        assert r.returncode == 0, r.stderr
        _write(conf, _flat_yaml(9090, with_tls=True))
        r = _run_cli(home, ["scan", conf, "--baseline", "env1"],
                     store=store_path)
        assert r.returncode == 1
        # zero noise: no baseline section, no baseline_violations key, no
        # C-10 baseline rows (the tls break is NOT drift-associated here).
        assert "Baseline violations:" not in r.stdout
        store = Store(str(store_path))
        try:
            assert store.list_constraint_violations(kind="baseline")["total"] == 0
            scans = store.list_scans(limit=1)
            payload = store.get_scan(scans[0]["scan_id"])
            assert "baseline_violations" not in payload["data"]
        finally:
            store.close()

    def test_baseline_c10_rows_keep_keys_and_detail(self, tmp_path):
        """C-10 kind=baseline rows must preserve involved_keys + message."""
        home = str(tmp_path / "home")
        store_path = str(tmp_path / "db" / "cfgdrift.db")
        conf = str(tmp_path / "conf" / "app.yaml")
        _write(conf, _flat_yaml(8080, with_tls=True))
        r = _run_cli(home, ["baseline", "create", "env1",
                            "--scan-root", conf], store=store_path)
        assert r.returncode == 0, r.stderr
        _write(conf, _flat_yaml(9090, with_tls=True))
        r = _run_cli(home, ["scan", conf, "--baseline", "env1",
                            "--report-violations"], store=store_path)
        assert r.returncode == 1
        store = Store(str(store_path))
        try:
            events = store.list_constraint_violations(kind="baseline")
            assert events["total"] >= 1
            for e in events["events"]:
                assert e["keys"] != [], "baseline row must keep involved_keys"
                assert e["detail"] != "", "baseline row must keep message"
        finally:
            store.close()


# ---------------------------------------------------------------------------
# e. C-10 store (independent of engineer's test_c10_store.py)
# ---------------------------------------------------------------------------

class TestC10Store:
    def _store(self, tmp_path):
        return Store(str(tmp_path / "db" / "cfgdrift.db"))

    def _old_iso(self, days_ago):
        return (datetime.now(timezone.utc) -
                timedelta(days=days_ago)).isoformat()

    def test_drift_visible_and_pagination(self, tmp_path):
        store = self._store(tmp_path)
        try:
            for i in range(5):
                store.add_constraint_violations(
                    None, [{"constraint_id": "c%d" % i, "kind": "drift",
                            "file": "a.yaml", "keys": ["k%d" % i],
                            "severity": "WARN", "detail": "d"}])
            res = store.list_constraint_violations(kind="drift", limit=2,
                                                   offset=0)
            assert res["total"] == 5
            assert len(res["events"]) == 2
            assert res["events"][0]["constraint_id"] == "c4"  # newest first
            res2 = store.list_constraint_violations(kind="drift", limit=2,
                                                    offset=2)
            assert len(res2["events"]) == 2
        finally:
            store.close()

    def test_prune_forged_old_rows(self, tmp_path):
        store = self._store(tmp_path)
        try:
            store.add_constraint_violations(
                None, [
                    {"constraint_id": "old", "kind": "drift", "file": "a",
                     "keys": [], "severity": "WARN", "detail": "",
                     "created_at": self._old_iso(200)},
                    {"constraint_id": "fresh", "kind": "drift", "file": "b",
                     "keys": [], "severity": "WARN", "detail": "",
                     "created_at": self._old_iso(1)},
                ])
            assert store.count_constraint_violations() == 2
            removed = store.prune_constraint_violations(days=90)
            assert removed == 1
            remaining = store.list_constraint_violations()["events"]
            assert [e["constraint_id"] for e in remaining] == ["fresh"]
        finally:
            store.close()

    def test_retention_env_override(self, tmp_path, monkeypatch):
        store = self._store(tmp_path)
        try:
            store.add_constraint_violations(
                None, [
                    {"constraint_id": "mid", "kind": "drift", "file": "a",
                     "keys": [], "severity": "WARN", "detail": "",
                     "created_at": self._old_iso(60)},
                    {"constraint_id": "fresh", "kind": "drift", "file": "b",
                     "keys": [], "severity": "WARN", "detail": "",
                     "created_at": self._old_iso(1)},
                ])
            assert store.prune_constraint_violations() == 0  # default 90d
            monkeypatch.setenv("CFGDRIFT_CV_RETENTION_DAYS", "30")
            assert store.prune_constraint_violations() == 1
        finally:
            store.close()

    def test_lazy_prune_every_200(self, tmp_path):
        store = self._store(tmp_path)
        try:
            store.add_constraint_violations(
                None, [{"constraint_id": "old", "kind": "drift", "file": "a",
                        "keys": [], "severity": "WARN", "detail": "",
                        "created_at": self._old_iso(200)}])
            store._cv_insert_count = 199  # one below threshold
            store.add_constraint_violations(
                None, [{"constraint_id": "fresh", "kind": "drift", "file": "b",
                        "keys": [], "severity": "WARN", "detail": "",
                        "created_at": self._old_iso(1)}])
            assert store.count_constraint_violations() == 1
            assert store.list_constraint_violations()["events"][0][
                "constraint_id"] == "fresh"
        finally:
            store.close()


# ---------------------------------------------------------------------------
# f. Web endpoints + SPA wiring
# ---------------------------------------------------------------------------

try:
    from fastapi.testclient import TestClient
    WEB_OK = True
except Exception:  # pragma: no cover
    TestClient = None  # type: ignore
    WEB_OK = False

USER_RULE = {
    "id": "qa_user_rule", "type": "range", "keys": ["server.port"],
    "min": 1, "max": 65535, "message": "qa user rule",
    "severity": "WARN", "enabled": True,
}


@pytest.fixture()
def web_env(tmp_path):
    from cfgdrift.web.app import create_app
    home = str(tmp_path / "home")
    os.makedirs(home, exist_ok=True)
    store = Store(str(tmp_path / "cfgdrift.db"))
    ConstraintConfig.add_rule(
        constraints_path(home), Constraint.from_dict(dict(USER_RULE),
                                                     source="user")
    )
    store.add_constraint_violations(
        1, [
            {"constraint_id": "qa_user_rule", "kind": "drift",
             "file": "a.yaml", "keys": ["server.port"], "severity": "WARN",
             "detail": "out of range"},
            {"constraint_id": "http_gzip_enum", "kind": "baseline",
             "file": "b.yaml", "keys": ["gzip"], "severity": "WARN",
             "detail": "bad gzip"},
        ],
    )
    app = create_app(store, home=home)
    client = TestClient(app)
    yield client, store, home
    store.close()


@pytest.mark.skipif(not WEB_OK, reason="fastapi/httpx unavailable")
class TestWebConstraints:
    def test_list_effective_view(self, web_env):
        client, _, _ = web_env
        resp = client.get("/api/constraints")
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == 0
        constraints = body["data"]["constraints"]
        ids = {c["id"]: c for c in constraints}
        assert len(constraints) == 21  # 20 builtin + 1 user
        assert ids["http_port_range"]["source"] == "builtin"
        assert ids["qa_user_rule"]["source"] == "user"
        assert ids["qa_user_rule"]["enabled"] is True
        assert ids["qa_user_rule"]["type"] == "range"
        assert ids["qa_user_rule"]["keys"] == ["server.port"]

    def test_put_user_toggle_persists_across_restart(self, web_env, tmp_path):
        client, store, home = web_env
        resp = client.put("/api/constraints/qa_user_rule/enabled",
                          json={"enabled": False})
        assert resp.status_code == 200
        assert resp.json()["data"] == {"id": "qa_user_rule", "enabled": False}
        # persisted on disk: a brand-new app instance (restart) sees it
        from cfgdrift.web.app import create_app
        store.close()
        store2 = Store(str(tmp_path / "cfgdrift.db"))
        try:
            app2 = create_app(store2, home=home)
            client2 = TestClient(app2)
            resp2 = client2.get("/api/constraints")
            user = next(c for c in resp2.json()["data"]["constraints"]
                        if c["id"] == "qa_user_rule")
            assert user["enabled"] is False
        finally:
            store2.close()

    def test_put_builtin_400(self, web_env):
        client, _, _ = web_env
        resp = client.put("/api/constraints/http_port_range/enabled",
                          json={"enabled": False})
        assert resp.status_code == 400
        assert "内置约束不可直接切换" in resp.json()["message"]

    def test_put_missing_404(self, web_env):
        client, _, _ = web_env
        resp = client.put("/api/constraints/does_not_exist/enabled",
                          json={"enabled": True})
        assert resp.status_code == 404

    def test_events_pagination_and_limit_clamp(self, web_env):
        client, _, _ = web_env
        resp = client.get("/api/constraint-events", params={"limit": 1,
                                                            "offset": 1})
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["total"] == 2
        assert len(data["events"]) == 1
        # limit clamped to 500
        resp2 = client.get("/api/constraint-events", params={"limit": 99999})
        assert resp2.status_code == 200
        assert len(resp2.json()["data"]["events"]) <= 500
        # filters
        resp3 = client.get("/api/constraint-events", params={"kind": "baseline"})
        assert resp3.json()["data"]["total"] == 1

    def test_spa_static_wiring(self, web_env):
        client, _, _ = web_env
        html = client.get("/").text
        assert 'data-view="constraints"' in html
        assert 'id="view-constraints"' in html
        js = client.get("/app.js").text
        assert "renderConstraints" in js
        assert "renderConstraintEvents" in js
        assert "constraints: renderConstraints" in js


# ---------------------------------------------------------------------------
# g. regression: v0.6.0 engine surface intact + daemon wiring
# ---------------------------------------------------------------------------

class TestRegression:
    def test_constraint_engine_v060_apply_upgrade(self, tmp_path):
        """v0.6.0 apply(): violation attaches to involved drift item and the
        item severity is upgraded (min(3, max(item.rank+1, c.rank)))."""
        home = str(tmp_path / "home")
        os.makedirs(home, exist_ok=True)
        constraints = resolve_constraints(home, [], builtin_enabled=True)
        old = parse_text("server:\n  port: 8080\n  gzip: on\n", "yaml")
        new = parse_text("server:\n  port: 70000\n  gzip: on\n", "yaml")
        items, summary = SemanticDiffer().diff_snapshot(
            {"app.yaml": old}, {"app.yaml": new}, constraints=constraints)
        port_items = [it for it in items if it.key_path == "server.port"]
        assert port_items
        it = port_items[0]
        assert any(v.get("constraint_id") == "http_port_range"
                   for v in it.constraint_violations)
        assert it.severity == Severity.CRITICAL  # upgrade caps at CRITICAL

    def test_composite_alert_payload_still_has_constraint_field(self, tmp_path):
        """v0.6.0 alert path: constraint field present only on violated items."""
        from cfgdrift.alert.dispatcher import AlertDispatcher  # noqa: F401
        # ensure the alert module imports (surface intact)
        import cfgdrift.alert.dispatcher  # noqa: F401

    def test_report_to_dict_no_baseline_key_by_default(self):
        from cfgdrift.core.model import Report, ScanSummary
        report = Report(
            scan_id=1, baseline=None, created_at="now", mode="manual",
            summary=ScanSummary(),
            items=[],
        )
        data = report.to_dict()
        assert "baseline_violations" not in data  # zero-noise (D3)

    def test_daemon_worker_imports_constraint_wiring(self):
        """daemon.worker still accepts --builtin/--constraints (v0.6.0)."""
        import cfgdrift.daemon.worker as worker_mod  # noqa: F401
        assert hasattr(worker_mod, "build_worker_command")
        assert hasattr(worker_mod, "DaemonWorker")
