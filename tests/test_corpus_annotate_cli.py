"""corpus annotate / kappa / stats CLI tests (v0.8.0, C-C5)."""

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

from click.testing import CliRunner  # noqa: E402

from cfgdrift.cli import cli  # noqa: E402
from cfgdrift.corpus.annotations import AnnotationStore  # noqa: E402
from cfgdrift.corpus.config import CorpusConfig, CorpusRepository  # noqa: E402
from cfgdrift.corpus.exporter import CorpusExporter  # noqa: E402
from cfgdrift.corpus.fetcher import ChangePairExtractor, LocalRepoSource  # noqa: E402
from cfgdrift.corpus.workspace import CorpusWorkspace  # noqa: E402


def _run_cli(args, cwd=ROOT):
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [PY, "-m", "cfgdrift.cli"] + args,
        capture_output=True, text=True, env=env, cwd=cwd, timeout=120,
    )


def _write_instances(ws, n=30):
    """Write a synthetic instances.jsonl with n minimal valid entries."""
    path = ws.instances_path()
    os.makedirs(ws.root, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for i in range(n):
            entry = {
                "schema_version": 1,
                "instance_id": "demo-fixture-%s-%d" % (hex(i)[2:].zfill(7), i),
                "metadata": {
                    "owner": "demo", "repo": "fixture", "path": "conf/app.yaml",
                    "commit": "a" * 40, "commit_time": "2026-08-01T00:00:00+00:00",
                    "author": "t", "message": "c",
                },
                "file": {"relpath": "conf/app.yaml", "format": "yaml"},
                "before": {"tree": None, "parse_ok": True, "present": False},
                "after": {"tree": {"server": {"port": 8080}}, "parse_ok": True,
                          "present": True},
                "diff": {
                    "items": [],
                    "summary": {"added": 0, "removed": 0, "modified": 0,
                                "type_changed": 0, "ignored": 0, "total": 0,
                                "max_severity": "NONE"},
                    "constraint_violations": [],
                    "feature": {"changed_keys": [], "changed_values": {},
                                "co_change_pairs": [], "co_change_capped": False},
                },
                "labels": {"severity": "NONE", "annotation": None, "annotator": None},
            }
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _batch_file(tmp_path, mapping):
    path = tmp_path / "labels.json"
    path.write_text(json.dumps(mapping), encoding="utf-8")
    return str(path)


@pytest.fixture()
def ws(tmp_path):
    w = CorpusWorkspace(str(tmp_path / "ws"))
    w.init()
    return w


def test_annotate_batch_two_annotators_kappa_stats(ws, tmp_path):
    _write_instances(ws, n=30)
    ids = [str(e["instance_id"]) for e in _load_all(ws)]

    # annotator1 labels all 30 (mixed).
    map1 = {iid: {"annotation": "minor"} for iid in ids}
    r = _run_cli(["corpus", "annotate", "--workspace", ws.root,
                  "--annotator", "alice", "--batch", _batch_file(tmp_path, map1)])
    assert r.returncode == 0, r.stderr
    assert "30 annotation(s)" in r.stdout

    # annotator2 labels all 30 (severe for the first 15, minor for the rest).
    map2 = {
        iid: {"annotation": "severe" if idx < 15 else "minor"}
        for idx, iid in enumerate(ids)
    }
    r = _run_cli(["corpus", "annotate", "--workspace", ws.root,
                  "--annotator", "bob", "--batch", _batch_file(tmp_path, map2)])
    assert r.returncode == 0, r.stderr

    # stats: double = 30.
    r = _run_cli(["corpus", "stats", "--workspace", ws.root])
    assert r.returncode == 0
    assert "双人完成           : 30" in r.stdout

    # kappa: n = 30, agreement = 15/30 (first half severe==severe).
    r = _run_cli(["corpus", "kappa", "--workspace", ws.root,
                  "--annotator-a", "alice", "--annotator-b", "bob"])
    assert r.returncode == 0, r.stderr
    assert "n=30" in r.stdout
    assert "Cohen's kappa" in r.stdout
    assert "混淆矩阵" in r.stdout

    # kappa --json carries structured fields.
    r = _run_cli(["corpus", "kappa", "--workspace", ws.root, "--json"])
    assert r.returncode == 0
    payload = json.loads(r.stdout)
    assert payload["data"]["n"] == 30
    assert payload["data"]["annotator_a"] == "alice"
    assert payload["data"]["annotator_b"] == "bob"


def test_kappa_auto_pair_most_overlap(ws, tmp_path):
    _write_instances(ws, n=6)
    ids = [str(e["instance_id"]) for e in _load_all(ws)]
    # alice+bob overlap on 4; alice+carol on 2.
    r = _run_cli(["corpus", "annotate", "--workspace", ws.root,
                  "--annotator", "alice", "--batch", _batch_file(
                      tmp_path, {iid: {"annotation": "normal"} for iid in ids})])
    assert r.returncode == 0
    r = _run_cli(["corpus", "annotate", "--workspace", ws.root,
                  "--annotator", "bob", "--batch", _batch_file(
                      tmp_path, {iid: {"annotation": "normal"}
                                 for iid in ids[:4]})])
    assert r.returncode == 0
    r = _run_cli(["corpus", "annotate", "--workspace", ws.root,
                  "--annotator", "carol", "--batch", _batch_file(
                      tmp_path, {iid: {"annotation": "minor"}
                                 for iid in ids[:2]})])
    assert r.returncode == 0
    r = _run_cli(["corpus", "kappa", "--workspace", ws.root])
    assert r.returncode == 0
    assert "alice vs bob" in r.stdout
    assert "n=4" in r.stdout


def test_kappa_less_than_two_annotators_exit2(ws, tmp_path):
    _write_instances(ws, n=3)
    r = _run_cli(["corpus", "annotate", "--workspace", ws.root,
                  "--annotator", "alice", "--batch", _batch_file(
                      tmp_path, {"demo-fixture-0000000-0": {"annotation": "minor"}})])
    assert r.returncode == 0
    r = _run_cli(["corpus", "kappa", "--workspace", ws.root])
    assert r.returncode == 2
    assert "至少 2 名标注人" in r.stderr


def test_annotate_interactive_cli_runner(ws):
    _write_instances(ws, n=3)
    runner = CliRunner()
    # Feed: 2 (minor) / 1 (severe) / s (skip) -> all processed.
    result = runner.invoke(
        cli, ["corpus", "annotate", "--workspace", ws.root, "--annotator", "alice"],
        input="2\n1\ns\n",
    )
    assert result.exit_code == 0, result.output
    store = AnnotationStore(ws)
    records = store.load()
    assert len(records) == 2
    annotations = {r.instance_id: r.annotation for r in records}
    assert set(annotations.values()) == {"minor", "severe"}
    # [q] exits without writing the current instance.
    result2 = runner.invoke(
        cli, ["corpus", "annotate", "--workspace", ws.root, "--annotator", "bob"],
        input="3\nq\n",
    )
    assert result2.exit_code == 0
    records2 = AnnotationStore(ws).load()
    bob_records = [r for r in records2 if r.annotator == "bob"]
    assert len(bob_records) == 1


class _GitFixture:
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


def test_export_merge_labels_and_repeat_export(tmp_path):
    """D3: export merges latest annotation into labels; repeat export keeps."""
    fixture = _GitFixture(tmp_path)
    fixture.commit("conf/app.yaml", "server:\n  port: 8080\n", "c1")
    fixture.commit("conf/app.yaml", "server:\n  port: 9090\n", "c2")
    fixture.commit("conf/app.yaml", "server:\n  port: 8080\n", "c3")

    ws = CorpusWorkspace(str(tmp_path / "ws"))
    ws.init()
    cfg = CorpusConfig.load(ws.config_path())
    cfg.repositories = [
        CorpusRepository(owner="demo", repo="fixture",
                         local_path=fixture.repo, glob="conf/*.yaml")
    ]
    cfg.since = None
    cfg.min_stars = None
    cfg.max_instances = 200
    cfg.save(ws.config_path())

    src = LocalRepoSource(fixture.repo)
    extractor = ChangePairExtractor()
    state = ws.read_state()
    entry = ws.repo_state(state, "demo/fixture")
    pairs, stats, newest = extractor.extract_repo(
        src, since=None, stop_at=None, max_pairs=200,
        glob_pattern=cfg.repositories[0].glob,
    )
    entry["last_commit"] = newest
    entry["instance_count"] = len(pairs)
    entry["local_path"] = fixture.repo
    ws.write_state(state)

    # First export: labels un-annotated.
    CorpusExporter().export(ws, cfg, constraints=None)
    entries = _load_all(ws)
    assert entries
    assert all(e["labels"]["annotation"] is None for e in entries)

    # Annotate all instances (one annotator, then a second).
    store = AnnotationStore(ws)
    for e in entries:
        store.add(e["instance_id"], "alice", "minor")
    for e in entries:
        store.add(e["instance_id"], "bob", "normal")

    # Second export: labels merged.
    CorpusExporter().export(ws, cfg, constraints=None)
    entries2 = _load_all(ws)
    assert all(e["labels"]["annotation"] == "normal" for e in entries2)
    assert all(e["labels"]["annotator"] == "bob" for e in entries2)

    # Third export (repeat): labels still merged — never lost (D3).
    CorpusExporter().export(ws, cfg, constraints=None)
    entries3 = _load_all(ws)
    assert all(e["labels"]["annotation"] == "normal" for e in entries3)


def test_stats_json(ws, tmp_path):
    _write_instances(ws, n=4)
    r = _run_cli(["corpus", "stats", "--workspace", ws.root, "--json"])
    assert r.returncode == 0
    payload = json.loads(r.stdout)
    assert payload["data"]["instances"] == 4
    assert payload["data"]["unannotated"] == 4
    assert payload["data"]["double"] == 0


def _load_all(ws):
    with open(ws.instances_path(), encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]
