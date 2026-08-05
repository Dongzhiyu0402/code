"""Independent QA verification tests for the 4 corpus toolchain fixes (v0.7.0).

This suite is written by QA (Edward) as an independent cross-check of the
engineer's ``tests/test_corpus_fixes.py``.  It deliberately uses different
techniques (in-process ``cli.main`` instead of subprocess CLI, class-method
monkeypatching for partial-success, direct ``_normalize_*`` checks) so a bug
in the engineer's test harness cannot mask a bug in the source.

Coverage:
a. git timeout precedence (explicit > env > 120) + invalid env fallback +
   ``--git-timeout`` 0/negative -> exit 2
b. ``since`` tolerant parsing (unquoted YAML date -> ISO str, effective_since
   always returns str, non-string rejected)
c. multi-pattern glob (str/list OR semantics, empty list == unset, single
   string backwards compatible)
d. partial-success fetch (per-repo state persisted, error markers, skip,
   --retry-failed clears, all-failed -> exit 2)
e. regression (version 0.7.0, corpus init/export/validate unchanged, other
   v0.7.0 CLI commands still registered)

All tests are offline: local git fixtures + in-process CLI, no network.
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

import cfgdrift  # noqa: E402
import cfgdrift.cli as cli_mod  # noqa: E402
from cfgdrift.cli import main as cli_main  # noqa: E402
from cfgdrift.corpus import fetcher  # noqa: E402
from cfgdrift.corpus.config import (  # noqa: E402
    CorpusConfig,
    CorpusRepository,
    _normalize_glob,
    _normalize_since,
)
from cfgdrift.corpus.fetcher import LocalRepoSource  # noqa: E402
from cfgdrift.corpus.workspace import CorpusWorkspace  # noqa: E402


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


class GitFixture:
    """Tiny local git repository with config commits (offline)."""

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


@pytest.fixture()
def fixture(tmp_path):
    return GitFixture(tmp_path)


def _init_workspace(tmp_path, repos, home=None):
    """Create a workspace + corpus.yaml with the given repositories."""
    ws_dir = str(tmp_path / "ws")
    ws = CorpusWorkspace(ws_dir)
    ws.init()
    cfg = CorpusConfig.load(ws.config_path())
    cfg.repositories = repos
    cfg.since = None
    cfg.min_stars = None
    cfg.save(ws.config_path())
    return ws, cfg


# ---------------------------------------------------------------------------
# a. git timeout
# ---------------------------------------------------------------------------

class TestGitTimeoutQa:
    def test_env_300_passed_to_run_git(self, monkeypatch):
        captured = {}
        monkeypatch.setenv("CFGDRIFT_GIT_TIMEOUT", "300")

        def fake_run(cmd, **kwargs):
            captured.update(kwargs)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(subprocess, "run", fake_run)
        fetcher._run_git("d", ["rev-parse"])
        assert captured["timeout"] == 300

    def test_explicit_beats_env(self, monkeypatch):
        monkeypatch.setenv("CFGDRIFT_GIT_TIMEOUT", "300")
        captured = {}

        def fake_run(cmd, **kwargs):
            captured.update(kwargs)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(subprocess, "run", fake_run)
        fetcher._run_git("d", ["rev-parse"], timeout=600)
        assert captured["timeout"] == 600

    def test_invalid_env_abc_falls_back_120(self, monkeypatch):
        monkeypatch.setenv("CFGDRIFT_GIT_TIMEOUT", "abc")
        captured = {}

        def fake_run(cmd, **kwargs):
            captured.update(kwargs)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(subprocess, "run", fake_run)
        fetcher._run_git("d", ["rev-parse"])  # must NOT raise
        assert captured["timeout"] == 120

    def test_cli_git_timeout_zero_exit2(self, tmp_path, capsys):
        # click validates --workspace (exists) before entering the function,
        # so create the directory; the git-timeout check then runs first
        ws_dir = str(tmp_path / "ws")
        os.makedirs(ws_dir, exist_ok=True)
        rc = cli_main(
            ["corpus", "fetch", "--workspace", ws_dir, "--git-timeout", "0"]
        )
        out = capsys.readouterr()
        assert rc == 2
        assert "--git-timeout" in out.err

    def test_cli_git_timeout_negative_exit2(self, tmp_path, capsys):
        ws_dir = str(tmp_path / "ws")
        os.makedirs(ws_dir, exist_ok=True)
        rc = cli_main(
            ["corpus", "fetch", "--workspace", ws_dir, "--git-timeout", "-1"]
        )
        out = capsys.readouterr()
        assert rc == 2
        assert "--git-timeout" in out.err


# ---------------------------------------------------------------------------
# b. since tolerant parsing
# ---------------------------------------------------------------------------

class TestSinceTolerantQa:
    def test_unquoted_date_load_normalized(self, tmp_path):
        path = tmp_path / "corpus.yaml"
        _write(str(path),
               "version: 1\nsince: 2024-01-01\nrepositories:\n"
               "  - owner: a\n    repo: b\n    since: 2023-02-02\n")
        cfg = CorpusConfig.load(str(path))
        assert cfg.since == "2024-01-01"
        assert cfg.repositories[0].since == "2023-02-02"

    def test_effective_since_returns_str_even_unvalidated(self):
        # build the config programmatically (bypassing validate()) with a
        # raw date object — effective_since must still return an ISO string
        repo = CorpusRepository(owner="a", repo="b",
                                since=datetime.date(2023, 2, 2))
        cfg = CorpusConfig(since=datetime.date(2024, 1, 1),
                           repositories=[repo])
        got = cfg.effective_since(repo)
        assert isinstance(got, str)
        assert got == "2023-02-02"
        # global fallback also normalized
        assert cfg.effective_since(CorpusRepository(owner="c", repo="d")) == \
            "2024-01-01"

    def test_invalid_int_since_rejected(self, tmp_path):
        path = tmp_path / "corpus.yaml"
        _write(str(path), "version: 1\nsince: 123\nrepositories: []\n")
        with pytest.raises(ValueError):
            CorpusConfig.load(str(path))

    def test_normalize_since_type_check(self):
        assert _normalize_since(None) is None
        assert _normalize_since("  ") is None
        assert _normalize_since("2024-01-01") == "2024-01-01"
        assert _normalize_since(datetime.date(2024, 1, 2)) == "2024-01-02"
        assert _normalize_since(
            datetime.datetime(2024, 1, 2, 3, 4, 5)) == "2024-01-02"
        with pytest.raises(ValueError):
            _normalize_since(123)


# ---------------------------------------------------------------------------
# c. glob multi-pattern
# ---------------------------------------------------------------------------

class TestGlobMultiQa:
    def test_matches_glob_or_semantics_yml_toml_txt(self):
        patterns = ["**/*.yml", "**/*.toml"]
        assert fetcher._matches_glob("conf/a.yml", patterns) is True
        assert fetcher._matches_glob("deep/nested/b.toml", patterns) is True
        assert fetcher._matches_glob("conf/a.txt", patterns) is False

    def test_normalize_glob_empty_list_is_none(self):
        assert _normalize_glob([]) is None
        assert _normalize_glob(["  "]) is None

    def test_normalize_glob_single_string_backcompat(self):
        assert _normalize_glob("conf/*.yaml") == "conf/*.yaml"
        assert _normalize_glob(["conf/*.yaml", "extra/*.toml"]) == [
            "conf/*.yaml", "extra/*.toml"]

    def test_glob_list_file_filter_end_to_end(self, tmp_path, fixture):
        fixture.commit("conf/app.yml", "a: 1\n", "c1")
        fixture.commit("extra/app.toml", "[x]\ny = 1\n", "c2")
        fixture.commit("notes/readme.txt", "hello\n", "c3")
        src = LocalRepoSource(fixture.repo)
        files = src.list_config_files(["**/*.yml", "**/*.toml"])
        assert files == ["conf/app.yml", "extra/app.toml"]

    def test_glob_empty_list_equivalent_unset_in_config(self, tmp_path):
        path = tmp_path / "corpus.yaml"
        _write(str(path), "version: 1\nrepositories:\n"
                          "  - owner: a\n    repo: b\n    glob: []\n")
        cfg = CorpusConfig.load(str(path))
        assert cfg.repositories[0].glob is None


# ---------------------------------------------------------------------------
# d. partial-success fetch (in-process CLI + method monkeypatch)
# ---------------------------------------------------------------------------

class TestPartialSuccessQa:
    def _two_repo_workspace(self, tmp_path, fixture, bad_dir):
        fixture.commit("conf/app.yaml", "server:\n  port: 8080\n", "c1")
        fixture.commit("conf/app.yaml", "server:\n  port: 9090\n", "c2")
        return _init_workspace(
            tmp_path,
            [
                CorpusRepository(owner="demo", repo="good",
                                 local_path=fixture.repo, glob="conf/*.yaml"),
                CorpusRepository(owner="demo", repo="bad",
                                 local_path=bad_dir, glob="conf/*.yaml"),
            ],
        )

    def _state(self, ws_dir):
        with open(os.path.join(ws_dir, "state.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def _count_instances(self, ws_dir):
        path = os.path.join(ws_dir, "instances.jsonl")
        if not os.path.exists(path):
            return 0
        with open(path, encoding="utf-8") as fh:
            return sum(1 for line in fh if line.strip())

    def _fail_second_repo(self, monkeypatch, bad_dir):
        """Make LocalRepoSource.clone_or_fetch fail only for ``bad_dir``."""
        orig = LocalRepoSource.clone_or_fetch
        bad = os.path.abspath(bad_dir)

        def fake(self):
            if os.path.abspath(self.dir) == bad:
                raise RuntimeError("git clone failed (qa mock)")
            return orig(self)

        monkeypatch.setattr(LocalRepoSource, "clone_or_fetch", fake)

    @staticmethod
    def _make_git_repo(path):
        """Turn ``path`` into a real local git repo with one config commit."""
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

    def test_partial_success_state_and_instances_kept(
            self, tmp_path, fixture, monkeypatch, capsys):
        bad_dir = str(tmp_path / "bad_repo")
        ws, _ = self._two_repo_workspace(tmp_path, fixture, bad_dir)
        self._fail_second_repo(monkeypatch, bad_dir)
        monkeypatch.setattr(cli_mod, "_daemon_home",
                            lambda: str(tmp_path / "home"))
        rc = cli_main(["corpus", "fetch", "--workspace", ws.root])
        capsys.readouterr()
        assert rc == 0
        state = self._state(ws.root)
        good = state["repos"]["demo/good"]
        assert good["local_path"] == os.path.abspath(fixture.repo)
        assert good["last_commit"]
        assert good["instance_count"] == 2
        assert "error" in state["repos"]["demo/bad"]
        # instances.jsonl keeps the successful repo's instances
        assert self._count_instances(ws.root) == 2

    def test_error_marked_repo_skipped_next_run(
            self, tmp_path, fixture, monkeypatch, capsys):
        bad_dir = str(tmp_path / "bad_repo")
        ws, _ = self._two_repo_workspace(tmp_path, fixture, bad_dir)
        self._fail_second_repo(monkeypatch, bad_dir)
        monkeypatch.setattr(cli_mod, "_daemon_home",
                            lambda: str(tmp_path / "home"))
        rc1 = cli_main(["corpus", "fetch", "--workspace", ws.root])
        capsys.readouterr()
        assert rc1 == 0
        rc2 = cli_main(["corpus", "fetch", "--workspace", ws.root])
        out = capsys.readouterr()
        assert rc2 == 0
        assert "skip demo/bad: previously failed" in out.out
        state = self._state(ws.root)
        assert "error" in state["repos"]["demo/bad"]
        assert state["repos"]["demo/good"]["instance_count"] == 2

    def test_retry_failed_clears_error(
            self, tmp_path, fixture, monkeypatch, capsys):
        bad_dir = str(tmp_path / "bad_repo")
        ws, _ = self._two_repo_workspace(tmp_path, fixture, bad_dir)
        monkeypatch.setattr(cli_mod, "_daemon_home",
                            lambda: str(tmp_path / "home"))
        # first run: bad repo fails
        orig = LocalRepoSource.clone_or_fetch
        bad = os.path.abspath(bad_dir)
        fail = {"on": True}

        def fake(self):
            if fail["on"] and os.path.abspath(self.dir) == bad:
                raise RuntimeError("git clone failed (qa mock)")
            return orig(self)

        monkeypatch.setattr(LocalRepoSource, "clone_or_fetch", fake)
        rc1 = cli_main(["corpus", "fetch", "--workspace", ws.root])
        capsys.readouterr()
        assert rc1 == 0
        state = self._state(ws.root)
        assert "error" in state["repos"]["demo/bad"]
        # second run with --retry-failed while the repo is still failing:
        # it is re-attempted (and fails again, so the marker stays)
        rc2 = cli_main(
            ["corpus", "fetch", "--workspace", ws.root, "--retry-failed"])
        out2 = capsys.readouterr()
        assert rc2 == 0
        assert "skip demo/bad" not in out2.out
        assert "error fetching demo/bad" in out2.err
        # now make the repo a real git repository and retry: the error marker
        # must be cleared and the repo must contribute instances
        self._make_git_repo(bad_dir)
        fail["on"] = False
        rc3 = cli_main(
            ["corpus", "fetch", "--workspace", ws.root, "--retry-failed"])
        capsys.readouterr()
        assert rc3 == 0
        state = self._state(ws.root)
        assert "error" not in state["repos"]["demo/bad"]
        assert state["repos"]["demo/bad"]["instance_count"] >= 1

    def test_all_failed_exit2(self, tmp_path, fixture, monkeypatch, capsys):
        bad_dir = str(tmp_path / "bad_repo")
        ws, _ = _init_workspace(
            tmp_path,
            [CorpusRepository(owner="demo", repo="bad1",
                              local_path=bad_dir, glob="conf/*.yaml"),
             CorpusRepository(owner="demo", repo="bad2",
                              local_path=str(tmp_path / "bad2"),
                              glob="conf/*.yaml")],
        )
        monkeypatch.setattr(cli_mod, "_daemon_home",
                            lambda: str(tmp_path / "home"))
        rc = cli_main(["corpus", "fetch", "--workspace", ws.root])
        out = capsys.readouterr()
        assert rc == 2
        state = self._state(ws.root)
        assert "error" in state["repos"]["demo/bad1"]
        assert "error" in state["repos"]["demo/bad2"]


# ---------------------------------------------------------------------------
# e. regression
# ---------------------------------------------------------------------------

class TestRegressionQa:
    def test_version_070(self):
        assert cfgdrift.__version__ == "0.8.0"

    def test_corpus_init_creates_layout(self, tmp_path, capsys):
        ws_dir = str(tmp_path / "ws")
        rc = cli_main(["corpus", "init", "--workspace", ws_dir])
        capsys.readouterr()
        assert rc == 0
        assert os.path.exists(os.path.join(ws_dir, "corpus.yaml"))
        assert os.path.exists(os.path.join(ws_dir, "state.json"))
        assert os.path.isdir(os.path.join(ws_dir, "repos"))
        cfg = CorpusConfig.load(os.path.join(ws_dir, "corpus.yaml"))
        assert cfg.since == "2023-01-01"
        assert len(cfg.repositories) == 2

    def test_corpus_export_validate_unchanged(self, tmp_path, capsys):
        ws_dir = str(tmp_path / "ws")
        rc = cli_main(["corpus", "init", "--workspace", ws_dir])
        capsys.readouterr()
        assert rc == 0
        rc = cli_main(["corpus", "export", "--workspace", ws_dir])
        out = capsys.readouterr()
        assert rc == 0
        assert "0 instance(s)" in out.out
        rc = cli_main(["corpus", "validate", "--workspace", ws_dir])
        out = capsys.readouterr()
        assert rc == 0
        assert "0 instance(s)" in out.out

    def test_v070_cli_commands_registered(self):
        group = cli_mod.cli
        names = set(group.commands.keys())
        for cmd in ("scan", "diff", "compare", "report", "corpus", "daemon",
                    "alert", "constraint", "ignore", "severity", "baseline",
                    "serve"):
            assert cmd in names, cmd
        corpus_cmds = set(group.commands["corpus"].commands.keys())
        assert {"init", "fetch", "export", "validate"} <= corpus_cmds
