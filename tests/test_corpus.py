"""corpus toolchain tests (v0.7.0, T03).

Covers: workspace init, corpus.yaml load/validate, fetch/export/validate with
a **local git fixture** (``local_path`` — fully offline, CI-safe), JSONL entry
schema, incremental fetch (``state.json`` ``last_commit``), idempotent export,
and the ``max_instances`` quota.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
PY = sys.executable

sys.path.insert(0, SRC)

from cfgdrift.corpus.config import (  # noqa: E402
    CorpusConfig,
    CorpusRepository,
    fmt_for_path,
    is_config_path,
)
from cfgdrift.corpus.exporter import CorpusExporter  # noqa: E402
from cfgdrift.corpus.fetcher import (  # noqa: E402
    ChangePairExtractor,
    LocalRepoSource,
)
from cfgdrift.corpus.validator import CorpusValidator  # noqa: E402
from cfgdrift.corpus.workspace import CorpusWorkspace  # noqa: E402
from cfgdrift.storage.store import Store  # noqa: E402


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


class GitFixture:
    """A tiny local git repository with config commits (offline)."""

    def __init__(self, root):
        self.repo = str(root / "repo")
        os.makedirs(self.repo, exist_ok=True)
        self._git("init", "-q")
        self._git("config", "user.email", "t@example.com")
        self._git("config", "user.name", "Tester")

    def _git(self, *args):
        r = subprocess.run(
            ["git", "-C", self.repo] + list(args),
            capture_output=True, text=True, timeout=60,
        )
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


def _make_workspace(tmp_path, fixture, glob="conf/*.yaml", max_instances=200,
                    local=True):
    ws = CorpusWorkspace(str(tmp_path / "ws"))
    ws.init()
    cfg = CorpusConfig.load(ws.config_path())
    cfg.repositories = [
        CorpusRepository(owner="demo", repo="fixture",
                         local_path=fixture.repo if local else None,
                         glob=glob)
    ]
    cfg.since = None
    cfg.min_stars = None
    cfg.max_instances = max_instances
    cfg.save(ws.config_path())
    return ws, cfg


class TestInitAndConfig:
    def test_init_creates_layout(self, tmp_path):
        ws = CorpusWorkspace(str(tmp_path / "ws"))
        ws.init()
        assert os.path.isdir(ws.repos_dir())
        assert os.path.exists(ws.config_path())
        assert os.path.exists(ws.state_path())
        cfg = CorpusConfig.load(ws.config_path())
        assert cfg.version == 1
        assert cfg.max_instances == 200
        assert cfg.repositories  # template ships with examples

    def test_init_idempotent(self, tmp_path):
        ws = CorpusWorkspace(str(tmp_path / "ws"))
        ws.init()
        before = open(ws.config_path(), encoding="utf-8").read()
        ws.init()
        assert open(ws.config_path(), encoding="utf-8").read() == before

    def test_config_validate_rejects_corrupt(self, tmp_path):
        path = tmp_path / "corpus.yaml"
        _write(str(path), "version: 99\nrepositories: []\n")
        with pytest.raises(ValueError):
            CorpusConfig.load(str(path))
        _write(str(path), "version: 1\nrepositories: [{owner: 'x'}]\n")
        with pytest.raises(ValueError):
            CorpusConfig.load(str(path))

    def test_repo_requires_owner_repo_or_local_path(self):
        with pytest.raises(ValueError):
            CorpusRepository.from_dict({"glob": "*.yaml"}, index=0)

    def test_fmt_mapping(self):
        assert fmt_for_path("a/b.yml") == "yaml"
        assert fmt_for_path("a/b.yaml") == "yaml"
        assert fmt_for_path("a/b.json") == "json"
        assert fmt_for_path("a/b.toml") == "toml"
        assert fmt_for_path("a/b.ini") == "ini"
        assert is_config_path("x/y.conf") is False  # whitelist is 5 types only
        assert is_config_path("x/y.yaml") is True


class TestFetchExportValidate:
    def _run_pipeline(self, ws, cfg, fixture):
        src = LocalRepoSource(fixture.repo)
        extractor = ChangePairExtractor()
        state = ws.read_state()
        entry = ws.repo_state(state, "demo/fixture")
        # per-repo quota mirrors the fetch CLI: max_instances / len(repos)
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
        stats_out = CorpusExporter().export(ws, cfg, constraints=None)
        return stats_out

    def test_fetch_export_validate(self, tmp_path, fixture):
        fixture.commit("conf/app.yaml", "server:\n  port: 8080\n  gzip: on\n", "c1")
        fixture.commit("conf/app.yaml", "server:\n  port: 9090\n  gzip: off\n", "c2")
        fixture.commit("conf/app.yaml", "server:\n  port: 8080\n  gzip: on\n", "c3")
        ws, cfg = _make_workspace(tmp_path, fixture)
        stats = self._run_pipeline(ws, cfg, fixture)
        assert stats["instances"] == 3
        assert stats["repos"] == 1
        # validate
        vs = CorpusValidator.validate(ws.instances_path())
        assert vs["instances"] == 3
        assert vs["repos"] == ["demo/fixture"]
        assert vs["formats"] == {"yaml": 3}
        assert vs["changes"]["modified"] == 4
        assert vs["changes"]["added"] == 1  # only the first commit adds the file
        # state
        state = ws.read_state()
        entry = state["repos"]["demo/fixture"]
        assert entry["last_commit"] == fixture.head()
        assert entry["instance_count"] == 3

    def test_instance_schema(self, tmp_path, fixture):
        fixture.commit("conf/app.yaml", "server:\n  port: 8080\n", "c1")
        fixture.commit("conf/app.yaml", "server:\n  port: 9090\n  tls:\n    enabled: true\n", "c2")
        ws, cfg = _make_workspace(tmp_path, fixture)
        self._run_pipeline(ws, cfg, fixture)
        with open(ws.instances_path(), encoding="utf-8") as fh:
            entries = [json.loads(line) for line in fh if line.strip()]
        first = entries[0]  # newest commit first
        assert first["schema_version"] == 1
        assert first["instance_id"].startswith("demo-fixture-")
        meta = first["metadata"]
        assert meta["owner"] == "demo"
        assert meta["repo"] == "fixture"
        assert meta["path"] == "conf/app.yaml"
        assert meta["commit"]
        assert meta["commit_time"]
        assert first["file"] == {"relpath": "conf/app.yaml", "format": "yaml"}
        assert first["before"]["present"] is True
        assert first["after"]["present"] is True
        assert first["before"]["parse_ok"] is True
        assert "tree" in first["before"] and "tree" in first["after"]
        diff = first["diff"]
        assert diff["items"]
        assert set(diff["summary"]) >= {"added", "removed", "modified",
                                        "type_changed", "ignored", "total",
                                        "max_severity"}
        assert isinstance(diff["constraint_violations"], list)
        feature = diff["feature"]
        assert isinstance(feature["changed_keys"], list)
        assert isinstance(feature["changed_values"], dict)
        assert isinstance(feature["co_change_pairs"], list)
        assert isinstance(feature["co_change_capped"], bool)
        assert first["labels"]["severity"]
        assert first["labels"]["annotation"] is None
        assert first["labels"]["annotator"] is None

    def test_export_idempotent(self, tmp_path, fixture):
        fixture.commit("conf/app.yaml", "server:\n  port: 8080\n", "c1")
        ws, cfg = _make_workspace(tmp_path, fixture)
        self._run_pipeline(ws, cfg, fixture)
        first = open(ws.instances_path(), encoding="utf-8").read()
        CorpusExporter().export(ws, cfg, constraints=None)
        second = open(ws.instances_path(), encoding="utf-8").read()
        assert first == second

    def test_incremental_fetch(self, tmp_path, fixture):
        fixture.commit("conf/app.yaml", "server:\n  port: 8080\n", "c1")
        fixture.commit("conf/app.yaml", "server:\n  port: 9090\n", "c2")
        ws, cfg = _make_workspace(tmp_path, fixture)
        stats1 = self._run_pipeline(ws, cfg, fixture)
        assert stats1["instances"] == 2
        state = ws.read_state()
        entry = state["repos"]["demo/fixture"]
        last = entry["last_commit"]

        # add a new commit -> incremental fetch only processes the new one
        fixture.commit("conf/app.yaml", "server:\n  port: 8443\n", "c3")
        src = LocalRepoSource(fixture.repo)
        pairs2, stats2, newest2 = ChangePairExtractor().extract_repo(
            src, since=None, stop_at=last, max_pairs=200,
            glob_pattern=cfg.repositories[0].glob,
        )
        assert len(pairs2) == 1
        assert newest2 == fixture.head()
        entry["last_commit"] = newest2
        entry["instance_count"] = entry["instance_count"] + len(pairs2)
        ws.write_state(state)
        stats2_out = CorpusExporter().export(ws, cfg, constraints=None)
        assert stats2_out["instances"] == 3

    def test_max_instances_cap(self, tmp_path, fixture):
        for i in range(5):
            fixture.commit("conf/app.yaml", "server:\n  port: %d\n" % (8000 + i), "c%d" % i)
        ws, cfg = _make_workspace(tmp_path, fixture, max_instances=3)
        stats = self._run_pipeline(ws, cfg, fixture)
        assert stats["instances"] == 3  # global + per-repo cap

    def test_glob_filters_files(self, tmp_path, fixture):
        fixture.commit("conf/app.yaml", "server:\n  port: 8080\n", "c1")
        fixture.commit("conf/app.yaml", "server:\n  port: 9090\n", "c2")
        fixture.commit("other/skip.yaml", "x: 1\n", "c3")
        ws, cfg = _make_workspace(tmp_path, fixture, glob="conf/*.yaml")
        stats = self._run_pipeline(ws, cfg, fixture)
        # the other/skip.yaml commit is excluded by the glob
        assert stats["instances"] == 2

    def test_parse_failure_skipped(self, tmp_path, fixture):
        fixture.commit("conf/bad.yaml", "a: [unclosed\n", "bad")
        fixture.commit("conf/ok.yaml", "server:\n  port: 8080\n", "c1")
        ws, cfg = _make_workspace(tmp_path, fixture)
        stats = self._run_pipeline(ws, cfg, fixture)
        assert stats["instances"] == 1  # unparseable pair skipped + counted

    def test_validate_rejects_corrupt_jsonl(self, tmp_path, fixture):
        fixture.commit("conf/app.yaml", "server:\n  port: 8080\n", "c1")
        ws, cfg = _make_workspace(tmp_path, fixture)
        self._run_pipeline(ws, cfg, fixture)
        bad = tmp_path / "bad.jsonl"
        _write(str(bad), '{"schema_version": 1}\n')
        with pytest.raises(ValueError):
            CorpusValidator.validate(str(bad))


class TestCliCorpus:
    def _run_cli(self, args):
        """Subprocess CLI so exit codes match the 0/1/2 contract (not click's
        standalone-mode 1 for exceptions)."""
        env = dict(os.environ)
        env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
        return subprocess.run(
            [PY, "-m", "cfgdrift.cli"] + list(args),
            capture_output=True, text=True, env=env, timeout=180,
        )

    def test_init_cli(self, tmp_path):
        ws_dir = str(tmp_path / "ws")
        res = self._run_cli(["corpus", "init", "--workspace", ws_dir])
        assert res.returncode == 0, res.stderr
        assert os.path.exists(os.path.join(ws_dir, "corpus.yaml"))
        assert os.path.exists(os.path.join(ws_dir, "state.json"))

    def test_fetch_export_validate_cli(self, tmp_path, fixture):
        fixture.commit("conf/app.yaml", "server:\n  port: 8080\n", "c1")
        ws_dir = str(tmp_path / "ws")
        self._run_cli(["corpus", "init", "--workspace", ws_dir])
        cfg_path = os.path.join(ws_dir, "corpus.yaml")
        cfg = CorpusConfig.load(cfg_path)
        cfg.repositories = [CorpusRepository(owner="demo", repo="fixture",
                                              local_path=fixture.repo,
                                              glob="conf/*.yaml")]
        cfg.since = None
        cfg.min_stars = None
        cfg.save(cfg_path)
        res = self._run_cli(["corpus", "fetch", "--workspace", ws_dir])
        assert res.returncode == 0, res.stderr
        assert "1 instance(s)" in res.stdout
        res = self._run_cli(["corpus", "validate", "--workspace", ws_dir])
        assert res.returncode == 0, res.stderr
        assert "1 instance(s)" in res.stdout

    def test_validate_corrupt_config_exit2(self, tmp_path):
        ws_dir = str(tmp_path / "ws")
        os.makedirs(ws_dir, exist_ok=True)
        _write(os.path.join(ws_dir, "corpus.yaml"), "version: 99\nrepositories: []\n")
        res = self._run_cli(["corpus", "fetch", "--workspace", ws_dir])
        assert res.returncode == 2
        assert "error:" in res.stderr
