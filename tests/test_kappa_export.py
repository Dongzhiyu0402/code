"""cfgdrift v0.10.0 kappa-export tests (P0-4): markdown + CSV renderers.

Covers V10-P0-4 acceptance:
- ``render_kappa_markdown`` produces a paper-appendix Markdown summary table
  (对比对 | kappa | 加权 kappa (linear) | 加权 kappa (quadratic) | n) plus
  the confusion-matrix table (rows = annotator A, columns = annotator B);
- ``render_kappa_csv`` produces per-instance rows with UTF-8 BOM + ``\\r\\n``
  (Excel / WPS friendly), mirroring ``cfgdrift.core.csvreport``;
- CLI ``corpus kappa --export PATH`` picks the renderer by extension,
  rejects anything but ``.md`` / ``.csv`` (exit 2), and conflicts with
  ``--json`` (exit 2);
- fewer than 2 annotators / fewer than 2 shared instances -> exit 2;
- the human-readable terminal output is unchanged without ``--export``
  (zero-noise), and the kappa *calculation* itself is untouched.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

from cfgdrift.corpus.annotations import (  # noqa: E402
    AnnotationStore,
    KappaCalculator,
    render_kappa_csv,
    render_kappa_markdown,
)
from cfgdrift.corpus.workspace import CorpusWorkspace  # noqa: E402

CATS = ("severe", "minor", "normal")


def _result():
    a = ["severe", "severe", "minor", "minor", "normal"]
    b = ["severe", "minor", "severe", "normal", "normal"]
    return KappaCalculator.cohen_kappa(a, b, CATS)


# ---------------------------------------------------------------------------
# renderers (pure functions)
# ---------------------------------------------------------------------------


class TestRenderKappaMarkdown:
    def test_summary_table_contents(self):
        md = render_kappa_markdown(_result(), "alice", "bob")
        assert "# Cohen's kappa" in md
        assert "| 对比对 | kappa | 加权 kappa (linear) | 加权 kappa (quadratic) | n |" in md
        assert "| alice vs bob | 0.118 | 0.348 | 0.571 | 5 |" in md
        assert "## 混淆矩阵（行 = alice，列 = bob）" in md
        # confusion matrix rows for every category
        for cat in CATS:
            assert "| %s |" % cat in md

    def test_matrix_values(self):
        md = render_kappa_markdown(_result(), "a", "b")
        # row "severe": 1 agreement + 1 off-diagonal
        assert "| severe | 1 | 1 | 0 |" in md


class TestRenderKappaCsv:
    def _rows(self):
        return [
            {"instance_id": "inst-0", "label_a": "severe", "label_b": "severe",
             "agree": True},
            {"instance_id": "inst-1", "label_a": "minor", "label_b": "severe",
             "agree": False},
        ]

    def test_csv_bom_and_line_ending(self):
        csv_text = render_kappa_csv(self._rows(), "alice", "bob")
        assert csv_text.startswith("\ufeff")
        assert "\r\n" in csv_text

    def test_csv_header_and_rows(self):
        csv_text = render_kappa_csv(self._rows(), "alice", "bob")
        lines = csv_text.split("\r\n")
        assert lines[0].lstrip("\ufeff") == "instance_id,alice,bob,一致,类别A,类别B"
        assert lines[1] == "inst-0,alice,bob,是,severe,severe"
        assert lines[2] == "inst-1,alice,bob,否,minor,severe"


# ---------------------------------------------------------------------------
# CLI --export end-to-end
# ---------------------------------------------------------------------------


def _run_cli(home, args):
    env = dict(os.environ)
    env["CFGDRIFT_HOME"] = str(home)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([sys.executable, "-m", "cfgdrift.cli"] + args,
                          capture_output=True, text=True, env=env, timeout=120)


@pytest.fixture()
def ws(tmp_path):
    w = CorpusWorkspace(str(tmp_path / "ws"))
    w.init()
    # seed 5 instances.jsonl entries
    instances = [
        {
            "schema_version": 1,
            "instance_id": "inst-%d" % i,
            "metadata": {"owner": "o", "repo": "r", "path": "conf/a.yaml",
                         "commit": "a" * 40, "commit_time": "t",
                         "author": "t", "message": "m"},
            "file": {"relpath": "conf/a.yaml", "format": "yaml"},
            "before": {"tree": None, "parse_ok": True, "present": False},
            "after": {"tree": {}, "parse_ok": True, "present": True},
            "diff": {"items": [], "summary": {}, "constraint_violations": [],
                     "feature": {}},
            "labels": {"severity": "NONE", "annotation": None,
                       "annotator": None},
        }
        for i in range(5)
    ]
    with open(w.instances_path(), "w", encoding="utf-8") as fh:
        for inst in instances:
            fh.write(json.dumps(inst, ensure_ascii=False) + "\n")
    return w


def _annotate(ws, pairs):
    store = AnnotationStore(ws)
    for instance_id, annotator, label in pairs:
        store.add(instance_id, annotator, label)


class TestCliKappaExport:
    def test_export_markdown_file(self, ws, tmp_path):
        _annotate(
            ws,
            [(f"inst-{i}", "alice", "minor") for i in range(5)]
            + [(f"inst-{i}", "bob", "severe" if i < 1 else "minor")
               for i in range(5)],
        )
        out = str(tmp_path / "kappa.md")
        r = _run_cli(tmp_path / "home",
                     ["corpus", "kappa", "--workspace", ws.root,
                      "--export", out])
        assert r.returncode == 0, r.stderr
        assert "kappa results written to" in r.stdout
        with open(out, encoding="utf-8") as fh:
            md = fh.read()
        assert "| 对比对 | kappa |" in md
        assert "alice vs bob" in md
        assert "混淆矩阵" in md

    def test_export_csv_file_bom(self, ws, tmp_path):
        _annotate(
            ws,
            [(f"inst-{i}", "alice", "minor") for i in range(5)]
            + [(f"inst-{i}", "bob", "severe" if i < 1 else "minor")
               for i in range(5)],
        )
        out = str(tmp_path / "kappa.csv")
        r = _run_cli(tmp_path / "home",
                     ["corpus", "kappa", "--workspace", ws.root,
                      "--export", out])
        assert r.returncode == 0, r.stderr
        with open(out, "rb") as fh:
            raw = fh.read()
        assert raw.startswith(b"\xef\xbb\xbf"), raw[:4]
        text = raw.decode("utf-8-sig")
        assert "instance_id" in text
        assert "alice" in text and "bob" in text
        assert "\r\n" in text
        assert text.count("\n") == 6  # header + 5 instances

    def test_bad_extension_exit2(self, ws, tmp_path):
        _annotate(
            ws,
            [(f"inst-{i}", "alice", "minor") for i in range(5)]
            + [(f"inst-{i}", "bob", "minor") for i in range(5)],
        )
        r = _run_cli(tmp_path / "home",
                     ["corpus", "kappa", "--workspace", ws.root,
                      "--export", str(tmp_path / "kappa.txt")])
        assert r.returncode == 2

    def test_export_conflicts_with_json_exit2(self, ws, tmp_path):
        _annotate(
            ws,
            [(f"inst-{i}", "alice", "minor") for i in range(5)]
            + [(f"inst-{i}", "bob", "minor") for i in range(5)],
        )
        r = _run_cli(tmp_path / "home",
                     ["corpus", "kappa", "--workspace", ws.root,
                      "--export", str(tmp_path / "k.md"), "--json"])
        assert r.returncode == 2

    def test_insufficient_annotators_exit2(self, ws, tmp_path):
        _annotate(ws, [(f"inst-{i}", "alice", "minor") for i in range(2)])
        r = _run_cli(tmp_path / "home",
                     ["corpus", "kappa", "--workspace", ws.root,
                      "--export", str(tmp_path / "k.md")])
        assert r.returncode == 2
        assert "至少 2 名标注人" in r.stderr

    def test_terminal_output_unchanged(self, ws, tmp_path):
        # Zero-noise: no --export -> human-readable output is byte-stable.
        _annotate(
            ws,
            [(f"inst-{i}", "alice", "minor") for i in range(5)]
            + [(f"inst-{i}", "bob", "severe" if i < 1 else "minor")
               for i in range(5)],
        )
        r = _run_cli(tmp_path / "home",
                     ["corpus", "kappa", "--workspace", ws.root])
        assert r.returncode == 0, r.stderr
        assert "Cohen's kappa = " in r.stdout
        assert "混淆矩阵 (行=alice, 列=bob):" in r.stdout
        assert "kappa results written to" not in r.stdout
