"""Regression tests for the 4 real-world corpus toolchain fixes (v0.7.0).

Covers:
1. configurable git timeout (``--git-timeout`` / ``CFGDRIFT_GIT_TIMEOUT`` /
   ``_run_git(timeout=...)`` passthrough),
2. ``since`` tolerant parsing (unquoted YAML dates -> ISO string),
3. multi-pattern ``glob`` (str or list[str], OR semantics),
4. partial-success ``corpus fetch`` (per-repo state persistence, error
   markers, ``--retry-failed``, exit 0 on partial success / 2 on total
   failure).

All tests are offline: local git fixtures + subprocess mocks, no network.
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

from cfgdrift.corpus import fetcher  # noqa: E402
from cfgdrift.corpus.config import (  # noqa: E402
    CorpusConfig,
    CorpusRepository,
)
from cfgdrift.corpus.exporter import CorpusExporter  # noqa: E402
from cfgdrift.corpus.fetcher import (  # noqa: E402
    ChangePairExtractor,
    GitCloneSource,
    LocalRepoSource,
)
from cfgdrift.corpus.workspace import CorpusWorkspace  # noqa: E402


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


def _make_workspace(tmp_path, fixture, glob="conf/*.yaml", max_instances=200):
    ws = CorpusWorkspace(str(tmp_path / "ws"))
    ws.init()
    cfg = CorpusConfig.load(ws.config_path())
    cfg.repositories = [
        CorpusRepository(owner="demo", repo="fixture",
                         local_path=fixture.repo, glob=glob)
    ]
    cfg.since = None
    cfg.min_stars = None
    cfg.max_instances = max_instances
    cfg.save(ws.config_path())
    return ws, cfg


def _run_pipeline(ws, cfg, fixture):
    """Fetch pairs the same way the CLI does, then export; returns stats."""
    src = LocalRepoSource(fixture.repo)
    extractor = ChangePairExtractor()
    state = ws.read_state()
    entry = ws.repo_state(state, "demo/fixture")
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
# 1. git timeout
# ---------------------------------------------------------------------------

class TestGitTimeout:
    def test_default_timeout_constant(self):
        assert fetcher.DEFAULT_GIT_TIMEOUT == 120
        assert fetcher._resolve_git_timeout(None) == 120

    def test_env_timeout(self, monkeypatch):
        monkeypatch.setenv("CFGDRIFT_GIT_TIMEOUT", "300")
        assert fetcher._resolve_git_timeout(None) == 300

    def test_explicit_overrides_env(self, monkeypatch):
        monkeypatch.setenv("CFGDRIFT_GIT_TIMEOUT", "300")
        assert fetcher._resolve_git_timeout(600) == 600

    def test_invalid_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("CFGDRIFT_GIT_TIMEOUT", "abc")
        assert fetcher._resolve_git_timeout(None) == 120
        monkeypatch.setenv("CFGDRIFT_GIT_TIMEOUT", "-5")
        assert fetcher._resolve_git_timeout(None) == 120

    def test_non_positive_explicit_raises(self):
        with pytest.raises(ValueError):
            fetcher._resolve_git_timeout(0)

    def test_run_git_passes_timeout(self, monkeypatch):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured.update(kwargs)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(subprocess, "run", fake_run)
        fetcher._run_git("d", ["rev-parse"], timeout=42)
        assert captured["timeout"] == 42

    def test_run_git_env_timeout(self, monkeypatch):
        monkeypatch.setenv("CFGDRIFT_GIT_TIMEOUT", "300")
        captured = {}

        def fake_run(cmd, **kwargs):
            captured.update(kwargs)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(subprocess, "run", fake_run)
        fetcher._run_git("d", ["rev-parse"])
        assert captured["timeout"] == 300

    def test_run_git_explicit_beats_env(self, monkeypatch):
        monkeypatch.setenv("CFGDRIFT_GIT_TIMEOUT", "300")
        captured = {}

        def fake_run(cmd, **kwargs):
            captured.update(kwargs)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(subprocess, "run", fake_run)
        fetcher._run_git("d", ["rev-parse"], timeout=600)
        assert captured["timeout"] == 600

    def test_clone_source_passes_timeout(self, monkeypatch, tmp_path):
        captured = []

        def fake_run(cmd, **kwargs):
            captured.append(kwargs.get("timeout"))
            if "rev-parse" in cmd:
                return subprocess.CompletedProcess(cmd, 128, "", "not a repo")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(subprocess, "run", fake_run)
        src = GitCloneSource("o", "r", str(tmp_path / "repo"), git_timeout=600)
        src.clone_or_fetch()
        assert 600 in captured

    def test_timeout_expired_raises(self, monkeypatch):
        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 120))

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(RuntimeError, match="timed out"):
            fetcher._run_git("d", ["clone", "x"], timeout=1)


# ---------------------------------------------------------------------------
# 2. since tolerant parsing
# ---------------------------------------------------------------------------

class TestSinceTolerant:
    def test_load_unquoted_date(self, tmp_path):
        path = tmp_path / "corpus.yaml"
        _write(str(path), "version: 1\nsince: 2024-01-01\n"
                          "repositories:\n  - owner: a\n    repo: b\n"
                          "    since: 2023-02-02\n")
        cfg = CorpusConfig.load(str(path))
        assert cfg.since == "2024-01-01"
        assert cfg.repositories[0].since == "2023-02-02"

    def test_load_quoted_string(self, tmp_path):
        path = tmp_path / "corpus.yaml"
        _write(str(path), 'version: 1\nsince: "2024-01-01"\n'
                          "repositories:\n  - owner: a\n    repo: b\n"
                          '    since: "2023-02-02"\n')
        cfg = CorpusConfig.load(str(path))
        assert cfg.since == "2024-01-01"
        assert cfg.repositories[0].since == "2023-02-02"

    def test_from_dict_date_and_datetime(self):
        import datetime
        r1 = CorpusRepository.from_dict(
            {"owner": "a", "repo": "b", "since": datetime.date(2024, 1, 1)}, 0
        )
        assert r1.since == "2024-01-01"
        r2 = CorpusRepository.from_dict(
            {"owner": "a", "repo": "b",
             "since": datetime.datetime(2024, 1, 1, 12, 30, 0)}, 0
        )
        assert r2.since == "2024-01-01"

    def test_validate_mutates_date_to_str(self):
        import datetime
        repo = CorpusRepository(owner="a", repo="b",
                                since=datetime.date(2024, 1, 1))
        repo.validate()
        assert repo.since == "2024-01-01"
        assert repo.to_dict()["since"] == "2024-01-01"

    def test_invalid_since_still_rejected(self, tmp_path):
        path = tmp_path / "corpus.yaml"
        _write(str(path), "version: 1\nsince: 12345\nrepositories: []\n")
        with pytest.raises(ValueError):
            CorpusConfig.load(str(path))
        with pytest.raises(ValueError):
            CorpusRepository.from_dict({"owner": "a", "repo": "b", "since": 5}, 0)

    def test_effective_since_normalized(self):
        import datetime
        repo = CorpusRepository(owner="a", repo="b",
                                since=datetime.date(2023, 2, 2))
        cfg = CorpusConfig(since="2024-01-01", repositories=[repo])
        assert cfg.effective_since(repo) == "2023-02-02"


# ---------------------------------------------------------------------------
# 3. glob multi-pattern
# ---------------------------------------------------------------------------

class TestGlobMulti:
    def test_matches_glob_single_string(self):
        assert fetcher._matches_glob("conf/a.yaml", "conf/*.yaml") is True
        assert fetcher._matches_glob("other/a.yaml", "conf/*.yaml") is False

    def test_matches_glob_list_or_semantics(self):
        patterns = ["conf/*.yaml", "extra/*.toml"]
        assert fetcher._matches_glob("conf/a.yaml", patterns) is True
        assert fetcher._matches_glob("extra/a.toml", patterns) is True
        assert fetcher._matches_glob("skip/a.yaml", patterns) is False

    def test_matches_glob_empty_list_unfiltered(self):
        assert fetcher._matches_glob("anything/a.yaml", []) is True

    def test_matches_glob_none_unfiltered(self):
        assert fetcher._matches_glob("anything/a.yaml", None) is True

    def test_config_accepts_str_and_list(self, tmp_path):
        path = tmp_path / "corpus.yaml"
        _write(str(path),
               "version: 1\nrepositories:\n"
               "  - owner: a\n    repo: b\n    glob: 'conf/*.yaml'\n"
               "  - owner: c\n    repo: d\n    glob:\n"
               "      - '**/*.yml'\n      - '**/*.toml'\n")
        cfg = CorpusConfig.load(str(path))
        assert cfg.repositories[0].glob == "conf/*.yaml"
        assert cfg.repositories[1].glob == ["**/*.yml", "**/*.toml"]

    def test_config_empty_list_normalized_to_none(self, tmp_path):
        path = tmp_path / "corpus.yaml"
        _write(str(path), "version: 1\nrepositories:\n"
                          "  - owner: a\n    repo: b\n    glob: []\n")
        cfg = CorpusConfig.load(str(path))
        assert cfg.repositories[0].glob is None

    def test_config_rejects_non_string_glob(self, tmp_path):
        path = tmp_path / "corpus.yaml"
        _write(str(path), "version: 1\nrepositories:\n"
                          "  - owner: a\n    repo: b\n    glob: 5\n")
        with pytest.raises(ValueError):
            CorpusConfig.load(str(path))

    def test_list_config_files_multi_glob(self, tmp_path, fixture):
        fixture.commit("conf/app.yaml", "server:\n  port: 8080\n", "c1")
        fixture.commit("extra/app.toml", "[server]\nport = 8080\n", "c2")
        fixture.commit("skip/other.yaml", "x: 1\n", "c3")
        src = LocalRepoSource(fixture.repo)
        files = src.list_config_files(["conf/*.yaml", "extra/*.toml"])
        assert files == ["conf/app.yaml", "extra/app.toml"]

    def test_glob_list_end_to_end(self, tmp_path, fixture):
        fixture.commit("conf/app.yaml", "server:\n  port: 8080\n", "c1")
        fixture.commit("conf/app.yaml", "server:\n  port: 9090\n", "c2")
        fixture.commit("extra/app.toml", "[server]\nport = 8080\n", "c3")
        fixture.commit("skip/other.yaml", "x: 1\n", "c4")
        ws, cfg = _make_workspace(tmp_path, fixture,
                                  glob=["conf/*.yaml", "extra/*.toml"])
        stats = _run_pipeline(ws, cfg, fixture)
        # conf x2 + extra toml x1 = 3; skip/other.yaml excluded by the glob
        assert stats["instances"] == 3

    def test_glob_empty_list_end_to_end(self, tmp_path, fixture):
        fixture.commit("conf/app.yaml", "server:\n  port: 8080\n", "c1")
        fixture.commit("conf/app.yaml", "server:\n  port: 9090\n", "c2")
        fixture.commit("other/skip.yaml", "x: 1\n", "c3")
        ws, cfg = _make_workspace(tmp_path, fixture, glob=[])
        stats = _run_pipeline(ws, cfg, fixture)
        # empty list == no extra filter: all config changes included
        assert stats["instances"] == 3

    def test_glob_single_string_unchanged(self, tmp_path, fixture):
        fixture.commit("conf/app.yaml", "server:\n  port: 8080\n", "c1")
        fixture.commit("conf/app.yaml", "server:\n  port: 9090\n", "c2")
        fixture.commit("other/skip.yaml", "x: 1\n", "c3")
        ws, cfg = _make_workspace(tmp_path, fixture, glob="conf/*.yaml")
        stats = _run_pipeline(ws, cfg, fixture)
        assert stats["instances"] == 2


# ---------------------------------------------------------------------------
# 4. partial-success fetch
# ---------------------------------------------------------------------------

class _CliHelper:
    def _run_cli(self, args, env_extra=None):
        """Subprocess CLI so exit codes match the 0/1/2 contract."""
        env = dict(os.environ)
        env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            [PY, "-m", "cfgdrift.cli"] + list(args),
            capture_output=True, text=True, env=env, timeout=180,
        )

    def _init_workspace(self, tmp_path, repos):
        ws_dir = str(tmp_path / "ws")
        res = self._run_cli(["corpus", "init", "--workspace", ws_dir])
        assert res.returncode == 0, res.stderr
        cfg_path = os.path.join(ws_dir, "corpus.yaml")
        cfg = CorpusConfig.load(cfg_path)
        cfg.repositories = repos
        cfg.since = None
        cfg.min_stars = None
        cfg.save(cfg_path)
        return ws_dir

    def _state(self, ws_dir):
        with open(os.path.join(ws_dir, "state.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def _count_instances(self, ws_dir):
        path = os.path.join(ws_dir, "instances.jsonl")
        if not os.path.exists(path):
            return 0
        with open(path, encoding="utf-8") as fh:
            return sum(1 for line in fh if line.strip())


class TestPartialSuccess(_CliHelper):
    def _two_repo_workspace(self, tmp_path, fixture, bad_dir):
        fixture.commit("conf/app.yaml", "server:\n  port: 8080\n", "c1")
        fixture.commit("conf/app.yaml", "server:\n  port: 9090\n", "c2")
        os.makedirs(bad_dir, exist_ok=True)  # NOT a git repository
        return self._init_workspace(
            tmp_path,
            [
                CorpusRepository(owner="demo", repo="good",
                                 local_path=fixture.repo, glob="conf/*.yaml"),
                CorpusRepository(owner="demo", repo="bad",
                                 local_path=bad_dir, glob="conf/*.yaml"),
            ],
        )

    def test_partial_success_preserves_state_and_instances(
            self, tmp_path, fixture):
        bad_dir = str(tmp_path / "bad_repo")
        ws_dir = self._two_repo_workspace(tmp_path, fixture, bad_dir)
        res = self._run_cli(["corpus", "fetch", "--workspace", ws_dir])
        assert res.returncode == 0, res.stderr
        assert "error fetching demo/bad" in res.stderr
        # the successful repo's state is persisted immediately
        state = self._state(ws_dir)
        assert state["repos"]["demo/good"]["instance_count"] == 2
        assert state["repos"]["demo/good"]["last_commit"]
        # the failed repo carries an error marker
        assert "error" in state["repos"]["demo/bad"]
        # instances from the successful repo are NOT lost
        assert self._count_instances(ws_dir) == 2

    def test_error_marked_repo_skipped_next_run(self, tmp_path, fixture):
        bad_dir = str(tmp_path / "bad_repo")
        ws_dir = self._two_repo_workspace(tmp_path, fixture, bad_dir)
        res1 = self._run_cli(["corpus", "fetch", "--workspace", ws_dir])
        assert res1.returncode == 0, res1.stderr
        res2 = self._run_cli(["corpus", "fetch", "--workspace", ws_dir])
        assert res2.returncode == 0, res2.stderr
        assert "skip demo/bad: previously failed" in res2.stdout
        state = self._state(ws_dir)
        assert "error" in state["repos"]["demo/bad"]
        # successful repo still has its instances
        assert state["repos"]["demo/good"]["instance_count"] == 2

    def test_all_failed_exit2(self, tmp_path):
        bad_dir = str(tmp_path / "bad_repo")
        os.makedirs(bad_dir, exist_ok=True)
        ws_dir = self._init_workspace(
            tmp_path,
            [CorpusRepository(owner="demo", repo="bad",
                              local_path=bad_dir, glob="conf/*.yaml")],
        )
        res = self._run_cli(["corpus", "fetch", "--workspace", ws_dir])
        assert res.returncode == 2
        assert "error fetching demo/bad" in res.stderr
        state = self._state(ws_dir)
        assert "error" in state["repos"]["demo/bad"]

    def test_retry_failed_forces_retry(self, tmp_path, fixture):
        bad_dir = str(tmp_path / "bad_repo")
        ws_dir = self._two_repo_workspace(tmp_path, fixture, bad_dir)
        res1 = self._run_cli(["corpus", "fetch", "--workspace", ws_dir])
        assert res1.returncode == 0, res1.stderr
        # fix the failed repo: turn it into a valid git repository
        self._make_git_repo(bad_dir)
        # without --retry-failed it stays skipped (still error-marked)
        res2 = self._run_cli(["corpus", "fetch", "--workspace", ws_dir])
        assert "skip demo/bad: previously failed" in res2.stdout
        # with --retry-failed it is re-attempted and succeeds
        res3 = self._run_cli(
            ["corpus", "fetch", "--workspace", ws_dir, "--retry-failed"]
        )
        assert res3.returncode == 0, res3.stderr
        state = self._state(ws_dir)
        assert "error" not in state["repos"]["demo/bad"]
        assert state["repos"]["demo/bad"]["instance_count"] >= 1

    def _make_git_repo(self, path):
        os.makedirs(path, exist_ok=True)
        subprocess.run(["git", "-C", path, "init", "-q"],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", path, "config", "user.email", "t@example.com"],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", path, "config", "user.name", "Tester"],
                       check=True, capture_output=True)
        p = os.path.join(path, "conf", "app.yaml")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("server:\n  port: 8080\n")
        subprocess.run(["git", "-C", path, "add", "-A"],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", path, "commit", "-q", "-m", "c1"],
                       check=True, capture_output=True)


class TestFetchGitTimeoutCli(_CliHelper):
    def test_git_timeout_option_accepted(self, tmp_path, fixture):
        fixture.commit("conf/app.yaml", "server:\n  port: 8080\n", "c1")
        ws_dir = self._init_workspace(
            tmp_path,
            [CorpusRepository(owner="demo", repo="fixture",
                              local_path=fixture.repo, glob="conf/*.yaml")],
        )
        res = self._run_cli(
            ["corpus", "fetch", "--workspace", ws_dir, "--git-timeout", "5"]
        )
        assert res.returncode == 0, res.stderr

    def test_git_timeout_invalid_exit2(self, tmp_path, fixture):
        fixture.commit("conf/app.yaml", "server:\n  port: 8080\n", "c1")
        ws_dir = self._init_workspace(
            tmp_path,
            [CorpusRepository(owner="demo", repo="fixture",
                              local_path=fixture.repo, glob="conf/*.yaml")],
        )
        res = self._run_cli(
            ["corpus", "fetch", "--workspace", ws_dir, "--git-timeout", "0"]
        )
        assert res.returncode == 2
        assert "--git-timeout" in res.stderr

    def test_git_timeout_env_var(self, tmp_path, fixture):
        fixture.commit("conf/app.yaml", "server:\n  port: 8080\n", "c1")
        ws_dir = self._init_workspace(
            tmp_path,
            [CorpusRepository(owner="demo", repo="fixture",
                              local_path=fixture.repo, glob="conf/*.yaml")],
        )
        res = self._run_cli(
            ["corpus", "fetch", "--workspace", ws_dir],
            env_extra={"CFGDRIFT_GIT_TIMEOUT": "5"},
        )
        assert res.returncode == 0, res.stderr


# ---------------------------------------------------------------------------
# workspace error-marker helpers
# ---------------------------------------------------------------------------

class TestWorkspaceErrorMarkers:
    def test_set_and_clear_error(self, tmp_path):
        ws = CorpusWorkspace(str(tmp_path / "ws"))
        ws.init()
        state = ws.read_state()
        entry = ws.repo_state(state, "a/b")
        assert "error" not in entry
        ws.set_repo_error(state, "a/b", "git command timed out")
        assert state["repos"]["a/b"]["error"] == "git command timed out"
        ws.write_state(state)
        state2 = ws.read_state()
        assert state2["repos"]["a/b"]["error"] == "git command timed out"
        ws.clear_repo_error(state2["repos"]["a/b"])
        assert "error" not in state2["repos"]["a/b"]

    def test_error_preserves_previous_progress(self, tmp_path):
        ws = CorpusWorkspace(str(tmp_path / "ws"))
        ws.init()
        state = ws.read_state()
        entry = ws.repo_state(state, "a/b")
        entry["instance_count"] = 14
        entry["last_commit"] = "abc123"
        ws.set_repo_error(state, "a/b", "git fetch failed")
        assert state["repos"]["a/b"]["instance_count"] == 14
        assert state["repos"]["a/b"]["last_commit"] == "abc123"
