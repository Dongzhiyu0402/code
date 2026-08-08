"""QA Round-9 independent verification tests for cfgdrift v0.8.0.

Independent, skeptical verification of the four v0.8.0 features:
  a. kappa correctness (hand-computed values, weighted, edges)
  b. annotation closed loop (batch -> kappa/stats, export merge, repeat export)
  c. severity x constraint_id (override + max_severity, D1 v0.7.0 equivalence)
  d. compare constraint block (env side / --no-builtin / --constraints / exit)
  e. explain (template determinism, LLM fallback on fabrication, masking D7)
  f. regression / version contract (0.8.0 / 0.8.0 / 0.8.0-c)

These tests intentionally re-derive expected values by hand instead of
copying the engineer's test numbers.
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

from click.testing import CliRunner  # noqa: E402

from cfgdrift.cli import cli  # noqa: E402
from cfgdrift.core.compare import CompareEngine  # noqa: E402
from cfgdrift.core.constraints import ConstraintEngine  # noqa: E402
from cfgdrift.core.differ import SemanticDiffer  # noqa: E402
from cfgdrift.core.model import (  # noqa: E402
    ChangeType,
    CompareReport,
    Constraint,
    DriftItem,
    ScanSummary,
    Severity,
    SeverityRule,
)
from cfgdrift.corpus.annotations import (  # noqa: E402
    ANNOTATION_VALUES,
    AnnotationStore,
    KappaCalculator,
)
from cfgdrift.corpus.exporter import CorpusExporter  # noqa: E402
from cfgdrift.corpus.workspace import CorpusWorkspace  # noqa: E402
from cfgdrift.explain.engine import ExplainEngine  # noqa: E402
from cfgdrift.explain.llm import LLMBackend  # noqa: E402
from cfgdrift.explain.templates import (  # noqa: E402
    KEY_SEMANTICS,
    TemplateEngine,
)
from cfgdrift.explain.validator import EvidenceValidator, build_facts  # noqa: E402
from cfgdrift.storage.store import Store  # noqa: E402

CATS = ("severe", "minor", "normal")


# ---------------------------------------------------------------------------
# a. kappa correctness — hand-computed values
# ---------------------------------------------------------------------------


def test_kappa_hand_computed_partial_agreement():
    """po=0.4 pe=0.32 -> kappa=(0.08)/(0.68)=0.117647 (computed by hand)."""
    a = ["severe", "severe", "minor", "minor", "normal"]
    b = ["severe", "minor", "severe", "normal", "normal"]
    r = KappaCalculator.cohen_kappa(a, b, CATS)
    assert abs(r["po"] - 0.4) < 1e-9
    # rows: severe=2, minor=2, normal=1; cols: severe=2, minor=1, normal=2
    # pe = (2*2 + 2*1 + 1*2)/25 = 8/25 = 0.32
    assert abs(r["pe"] - 0.32) < 1e-9
    assert abs(r["kappa"] - 0.1176470588) < 1e-6
    assert r["n"] == 5


def test_kappa_weighted_hand_computed():
    """Weighted kappa hand-check for the same 5-sample set.

    linear: 1 - sum(w*o)/sum(w*e) = 1 - 0.3/0.46 = 0.347826...
    quadratic: 1 - 0.15/0.35 = 0.571428...
    """
    a = ["severe", "severe", "minor", "minor", "normal"]
    b = ["severe", "minor", "severe", "normal", "normal"]
    r = KappaCalculator.cohen_kappa(a, b, CATS)
    assert abs(r["weighted"]["linear"] - 0.3478260869) < 1e-6
    assert abs(r["weighted"]["quadratic"] - 0.5714285714) < 1e-6


def test_kappa_perfect_and_opposite_edges():
    # Perfect agreement -> 1.0
    a = ["severe", "minor", "normal", "minor", "severe"]
    r = KappaCalculator.cohen_kappa(a, list(a), CATS)
    assert abs(r["kappa"] - 1.0) < 1e-9
    # Completely opposite on 2 categories -> -1.0
    r2 = KappaCalculator.cohen_kappa(
        ["x", "y", "x", "y"], ["y", "x", "y", "x"], ("x", "y")
    )
    assert abs(r2["kappa"] - (-1.0)) < 1e-9


def test_kappa_pe_one_edge():
    """1 - pe == 0: pe=1 with po==1 -> kappa=1.0 (sklearn behavior)."""
    r = KappaCalculator.cohen_kappa(["x", "x"], ["x", "x"], ("x",))
    assert r["pe"] == 1.0
    assert r["kappa"] == 1.0
    r2 = KappaCalculator.cohen_kappa(["x", "x"], ["x", "x"], ("x", "y"))
    assert abs(r2["pe"] - 1.0) < 1e-9
    assert r2["kappa"] == 1.0


def test_kappa_n_lt_2_and_mismatch_errors():
    with pytest.raises(ValueError):
        KappaCalculator.cohen_kappa(["severe"], ["minor"], CATS)
    with pytest.raises(ValueError):
        KappaCalculator.weighted_kappa(["severe"], ["minor"], CATS, "linear")
    with pytest.raises(ValueError):
        KappaCalculator.cohen_kappa(["severe", "minor"], ["normal"], CATS)


def test_confusion_matrix_row_col_semantics():
    """Rows = annotator A, cols = annotator B."""
    a = ["severe", "minor", "normal", "severe"]
    b = ["severe", "severe", "normal", "minor"]
    cm = KappaCalculator.confusion_matrix(a, b, CATS)
    # A=severe & B=severe
    assert cm["severe"]["severe"] == 1
    # A=severe & B=minor (2nd severe from a paired with minor from b)
    assert cm["severe"]["minor"] == 1
    # A=minor & B=severe
    assert cm["minor"]["severe"] == 1
    assert cm["normal"]["normal"] == 1


def test_kappa_random_scale_near_zero():
    """A deliberately near-random pairing yields kappa close to 0."""
    a = ["severe", "minor", "normal", "severe", "minor", "normal", "severe"]
    b = ["minor", "normal", "severe", "normal", "severe", "minor", "minor"]
    r = KappaCalculator.cohen_kappa(a, b, CATS)
    assert -0.6 < r["kappa"] < 0.6  # not degenerate


# ---------------------------------------------------------------------------
# b. annotation closed loop
# ---------------------------------------------------------------------------


@pytest.fixture()
def ws(tmp_path):
    w = CorpusWorkspace(str(tmp_path / "ws"))
    w.init()
    return w


def _write_instances(ws, n=6):
    path = ws.instances_path()
    os.makedirs(ws.root, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for i in range(n):
            entry = {
                "schema_version": 1,
                "instance_id": "inst-%d" % i,
                "metadata": {"owner": "demo", "repo": "fixture",
                             "path": "conf/app.yaml", "commit": "a" * 40,
                             "commit_time": "2026-08-01T00:00:00+00:00",
                             "author": "t", "message": "c"},
                "file": {"relpath": "conf/app.yaml", "format": "yaml"},
                "before": {"tree": None, "parse_ok": True, "present": False},
                "after": {"tree": {"server": {"port": 8080}}, "parse_ok": True,
                          "present": True},
                "diff": {"items": [], "summary": {"added": 0, "removed": 0,
                                                  "modified": 0, "type_changed": 0,
                                                  "ignored": 0, "total": 0,
                                                  "max_severity": "NONE"},
                         "constraint_violations": [],
                         "feature": {"changed_keys": [], "changed_values": {},
                                     "co_change_pairs": [], "co_change_capped": False}},
                "labels": {"severity": "NONE", "annotation": None, "annotator": None},
            }
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _batch_file(tmp_path, mapping, name="labels.json"):
    path = tmp_path / name
    path.write_text(json.dumps(mapping), encoding="utf-8")
    return str(path)


def _load_all(ws):
    with open(ws.instances_path(), encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def test_annotate_batch_double_then_kappa_stats(ws, tmp_path):
    _write_instances(ws, n=6)
    ids = [str(e["instance_id"]) for e in _load_all(ws)]
    # alice: all minor; bob: severe for first 2, minor for rest
    # -> agreement on 4 instances (inst-2..5) = 4/6
    r = subprocess.run(
        [PY, "-m", "cfgdrift.cli", "corpus", "annotate", "--workspace", ws.root,
         "--annotator", "alice", "--batch", _batch_file(
             tmp_path, {iid: {"annotation": "minor"} for iid in ids})],
        capture_output=True, text=True, env={**os.environ, "PYTHONPATH": SRC},
        cwd=ROOT, timeout=120,
    )
    assert r.returncode == 0, r.stderr
    r = subprocess.run(
        [PY, "-m", "cfgdrift.cli", "corpus", "annotate", "--workspace", ws.root,
         "--annotator", "bob", "--batch", _batch_file(
             tmp_path, {iid: {"annotation": "severe" if i < 2 else "minor"}
                        for i, iid in enumerate(ids)})],
        capture_output=True, text=True, env={**os.environ, "PYTHONPATH": SRC},
        cwd=ROOT, timeout=120,
    )
    assert r.returncode == 0, r.stderr

    # stats: 6 instances, 0 unannotated, 6 double
    r = subprocess.run(
        [PY, "-m", "cfgdrift.cli", "corpus", "stats", "--workspace", ws.root,
         "--json"],
        capture_output=True, text=True, env={**os.environ, "PYTHONPATH": SRC},
        cwd=ROOT, timeout=120,
    )
    payload = json.loads(r.stdout)
    assert payload["data"]["instances"] == 6
    assert payload["data"]["unannotated"] == 0
    assert payload["data"]["single"] == {}
    assert payload["data"]["double"] == 6
    # alice=minor all; bob=severe,severe,minor,minor,minor,minor
    # agreement on inst-2..5 (4) only -> 4/6
    assert abs(payload["data"]["agreement_rate"] - 4.0 / 6.0) < 1e-9

    # kappa n=6, po=4/6
    r = subprocess.run(
        [PY, "-m", "cfgdrift.cli", "corpus", "kappa", "--workspace", ws.root,
         "--annotator-a", "alice", "--annotator-b", "bob", "--json"],
        capture_output=True, text=True, env={**os.environ, "PYTHONPATH": SRC},
        cwd=ROOT, timeout=120,
    )
    payload = json.loads(r.stdout)
    assert payload["data"]["n"] == 6
    assert abs(payload["data"]["po"] - 4.0 / 6.0) < 1e-9


def test_annotate_upsert_last_write_wins(ws):
    store = AnnotationStore(ws)
    first = store.add("inst-1", "alice", "minor")
    second = store.add("inst-1", "alice", "severe")
    records = store.load()
    assert len(records) == 1
    assert records[0].annotation == "severe"
    assert records[0].annotated_at >= first.annotated_at
    # stats sees alice single on inst-1
    stats = store.stats([{"instance_id": "inst-1"}])
    assert stats["single"] == {"alice": 1}
    assert stats["double"] == 0


def test_annotate_interactive_stdin(ws):
    _write_instances(ws, n=3)
    runner = CliRunner()
    # invalid input first -> retry -> then 2/minor, s/skip, q/quit
    result = runner.invoke(
        cli, ["corpus", "annotate", "--workspace", ws.root, "--annotator", "alice"],
        input="x\n2\ns\nq\n",
    )
    assert result.exit_code == 0, result.output
    store = AnnotationStore(ws)
    records = store.load()
    assert len(records) == 1
    assert records[0].annotation == "minor"


def test_export_merge_labels_and_repeat_export_no_loss(ws):
    """D3: export merges latest annotation; repeat export keeps labels."""
    _write_instances(ws, n=3)
    ids = [str(e["instance_id"]) for e in _load_all(ws)]
    store = AnnotationStore(ws)
    for iid in ids:
        store.add(iid, "alice", "minor")
    for iid in ids:
        store.add(iid, "bob", "normal")  # bob is later -> latest wins

    # First export: labels should now carry bob's annotation.
    from cfgdrift.corpus.config import CorpusConfig, CorpusRepository

    cfg = CorpusConfig.load(ws.config_path())
    cfg.repositories = []
    cfg.save(ws.config_path())
    CorpusExporter().export(ws, cfg, constraints=None)
    entries = _load_all(ws)
    assert all(e["labels"]["annotation"] == "normal" for e in entries)
    assert all(e["labels"]["annotator"] == "bob" for e in entries)

    # Repeat export: still merged (never lost).
    CorpusExporter().export(ws, cfg, constraints=None)
    entries2 = _load_all(ws)
    assert all(e["labels"]["annotation"] == "normal" for e in entries2)


# ---------------------------------------------------------------------------
# c. severity x constraint_id
# ---------------------------------------------------------------------------


RANGE_C = Constraint(
    id="http_port_range",
    type="range",
    message="端口超出允许范围",
    severity=Severity.WARN,
    keys=["server.port"],
    min=1,
    max=65535,
)


def test_severity_rule_constraint_id_override_and_max_severity():
    differ = SemanticDiffer()
    old = {"app.json": {"server": {"port": 8080}}}
    new = {"app.json": {"server": {"port": 99999}}}
    rule = SeverityRule(
        name="port-critical", severity=Severity.CRITICAL,
        constraint_id=["http_port_range"],
    )
    items, summary = differ.diff_snapshot(
        old, new, severity_rules=[rule], constraints=[RANGE_C],
    )
    it = items[0]
    assert it.severity == Severity.CRITICAL
    assert summary.max_severity == Severity.CRITICAL
    assert it.constraint_violations[0]["constraint_id"] == "http_port_range"


def test_severity_constraint_id_first_match_wins():
    differ = SemanticDiffer()
    old = {"app.json": {"server": {"port": 8080}}}
    new = {"app.json": {"server": {"port": 99999}}}
    rule1 = SeverityRule(
        name="first", severity=Severity.CRITICAL, constraint_id=["http_port_range"],
    )
    rule2 = SeverityRule(
        name="second", severity=Severity.NONE, key_pattern="server\\.port",
    )
    items, _ = differ.diff_snapshot(
        old, new, severity_rules=[rule1, rule2], constraints=[RANGE_C],
    )
    assert items[0].severity == Severity.CRITICAL  # first match wins

    # Reverse order -> second (no constraint_id) wins first; then the single
    # upgrade pass applies min(3, max(NONE+1, WARN)) = WARN (D1 monotone).
    items2, _ = differ.diff_snapshot(
        old, new, severity_rules=[rule2, rule1], constraints=[RANGE_C],
    )
    assert items2[0].severity == Severity.WARN


def test_d1_equivalence_no_constraint_id_byte_identical():
    """v0.7.0 pipeline (override -> apply attach+upgrade) == v0.8.0 output."""
    def v070(old, new, severity_rules, constraints):
        differ = SemanticDiffer()
        raw = []
        differ._diff_node(old, new, [], "app.json", raw)
        for item in raw:
            for rule in severity_rules:
                if rule.matches(item):
                    item.severity = rule.severity
                    break
        ConstraintEngine.apply({"app.json": new}, raw, constraints)
        return [it.to_dict() for it in raw]

    def v080(old, new, severity_rules, constraints):
        differ = SemanticDiffer()
        items, _ = differ.diff_snapshot(
            {"app.json": old}, {"app.json": new},
            severity_rules=severity_rules, constraints=constraints,
        )
        return [it.to_dict() for it in items]

    cases = [
        ({"server": {"port": 8080}}, {"server": {"port": 99999}}, [], [RANGE_C]),
        ({"server": {"port": 8080}}, {"server": {"port": 99999}},
         [SeverityRule(name="r", severity=Severity.INFO, key_pattern=r"server\.port")],
         [RANGE_C]),
        ({"a": 1}, {"a": 2}, [], [RANGE_C]),
        ({"a": 1}, {"a": 2},
         [SeverityRule(name="r", severity=Severity.NONE, key_pattern="a")], [RANGE_C]),
    ]
    for old, new, rules, cons in cases:
        assert v070(old, new, rules, cons) == v080(old, new, rules, cons)


def test_severity_add_constraint_id_and_list_cli(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "PYTHONPATH": SRC, "CFGDRIFT_HOME": str(home)}
    r = subprocess.run(
        [PY, "-m", "cfgdrift.cli", "severity", "add", "--name", "p",
         "--severity", "CRITICAL", "--constraint-id", "http_port_range"],
        capture_output=True, text=True, env=env, cwd=ROOT, timeout=120,
    )
    assert r.returncode == 0, r.stderr
    assert "constraint=http_port_range" in r.stdout
    r = subprocess.run(
        [PY, "-m", "cfgdrift.cli", "severity", "list"],
        capture_output=True, text=True, env=env, cwd=ROOT, timeout=120,
    )
    assert "constraint=http_port_range" in r.stdout


def test_severity_to_dict_zero_noise():
    legacy = SeverityRule(name="r", severity=Severity.WARN, key_pattern="a")
    d = legacy.to_dict()
    assert "constraint_id" not in d
    new_rule = SeverityRule(name="r2", severity=Severity.CRITICAL,
                            constraint_id=["c1"])
    assert new_rule.to_dict()["constraint_id"] == ["c1"]


# ---------------------------------------------------------------------------
# d. compare constraints
# ---------------------------------------------------------------------------


def _make_store(tmp_path, baselines):
    store = Store(str(tmp_path / "test.db"))
    for name, data in baselines.items():
        store.create_baseline(
            name=name, description="", scan_root=str(tmp_path), format="json",
            data={"app.json": data}, line_maps=None,
        )
    return store


def test_compare_report_empty_lists_zero_noise():
    """Fixed bug: {"env_a": [], "env_b": []} must NOT emit the key."""
    rep = CompareReport(
        baseline_a="a", baseline_b="b", created_at="t",
        summary=ScanSummary(), items=[],
        constraint_violations={"env_a": [], "env_b": []},
    )
    out = rep.to_dict()
    assert "constraint_violations" not in out


def test_compare_report_any_values_output():
    rep = CompareReport(
        baseline_a="a", baseline_b="b", created_at="t",
        summary=ScanSummary(), items=[],
        constraint_violations={"env_a": [{"constraint_id": "c"}], "env_b": []},
    )
    out = rep.to_dict()
    assert out["constraint_violations"]["env_a"][0]["constraint_id"] == "c"
    assert out["constraint_violations"]["env_b"] == []


def test_compare_split_by_env_and_structure(tmp_path):
    tight = Constraint(
        id="prod_port_range",
        type="range",
        message="端口 {value} 超出允许范围 {min}-{max}",
        severity=Severity.WARN,
        keys=["server.port"],
        min=8000,
        max=9000,
    )
    store = _make_store(
        tmp_path,
        {"dev": {"server": {"port": 8080}},
         "prod": {"server": {"port": 9500}}},
    )
    engine = CompareEngine(store)
    reports = engine.compare(["dev", "prod"], constraints=[tight])
    store.close()
    rep = reports[0]
    assert rep.constraint_violations["env_a"] == []
    assert len(rep.constraint_violations["env_b"]) == 1
    v = rep.constraint_violations["env_b"][0]
    assert set(v) >= {"constraint_id", "type", "message", "involved_keys",
                      "file", "severity"}
    assert v["constraint_id"] == "prod_port_range"
    assert v["severity"] == "WARN"


def test_compare_exit_code_drift_based_no_drift_violations(tmp_path):
    """D6: violations without drift -> exit 0, but block still renders."""
    home = tmp_path / "home"
    home.mkdir()
    store = _make_store(
        tmp_path,
        {"dev": {"server": {"port": 9500}},
         "prod": {"server": {"port": 9500}}},  # same data, both violate
    )
    store.close()
    constraints_file = tmp_path / "constraints.yaml"
    constraints_file.write_text(
        "version: 1\nrules:\n"
        "  - id: prod_port_range\n    type: range\n    message: 端口超出范围\n"
        "    severity: WARN\n    keys: [server.port]\n    min: 8000\n    max: 9000\n",
        encoding="utf-8",
    )
    env = {**os.environ, "PYTHONPATH": SRC, "CFGDRIFT_HOME": str(home)}
    proc = subprocess.run(
        [PY, "-m", "cfgdrift.cli", "--store", str(tmp_path / "test.db"),
         "compare", "dev", "prod", "--no-builtin",
         "--constraints", str(constraints_file)],
        capture_output=True, text=True, env=env, cwd=ROOT, timeout=120,
    )
    assert proc.returncode == 0  # no drift -> 0 even with violations (D6)
    assert "no differences" in proc.stdout
    assert "约束检查" in proc.stdout
    assert "[env_a: dev] WARN prod_port_range" in proc.stdout
    assert "[env_b: prod] WARN prod_port_range" in proc.stdout


def test_compare_no_violation_no_block_and_exit(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    store = _make_store(
        tmp_path,
        {"dev": {"server": {"port": 8080}},
         "prod": {"server": {"port": 8081}}},
    )
    store.close()
    env = {**os.environ, "PYTHONPATH": SRC, "CFGDRIFT_HOME": str(home)}
    proc = subprocess.run(
        [PY, "-m", "cfgdrift.cli", "--store", str(tmp_path / "test.db"),
         "compare", "dev", "prod", "--no-builtin"],
        capture_output=True, text=True, env=env, cwd=ROOT, timeout=120,
    )
    assert proc.returncode == 1  # drift present
    assert "约束检查" not in proc.stdout


# ---------------------------------------------------------------------------
# e. explain
# ---------------------------------------------------------------------------


class _MockLLM(LLMBackend):
    def __init__(self, reply=None, available=True):
        self.reply = reply
        self.flag = available
        self.prompt = None

    def available(self):
        return self.flag

    def generate(self, prompt):
        self.prompt = prompt
        return self.reply


def _drift_items():
    return [
        {
            "key_path": "services.web.ports[0]",
            "change_type": "modified",
            "severity": "CRITICAL",
            "old_value": "8080:80",
            "new_value": "9090:80",
            "constraint_violations": [
                {"constraint_id": "http_port_range", "type": "range",
                 "message": "端口超出允许范围",
                 "involved_keys": ["services.web.ports[0]"]}
            ],
        },
        {
            "key_path": "services.api.image",
            "change_type": "modified",
            "severity": "WARN",
            "old_value": "nginx:1.25",
            "new_value": "nginx:latest",
            "constraint_violations": [],
        },
    ]


def test_explain_template_deterministic_and_evidence_subset():
    engine = TemplateEngine()
    n1 = engine.render(_drift_items()[0])
    n2 = engine.render(_drift_items()[0])
    assert n1.to_dict() == n2.to_dict()
    assert n1.source == "template"
    assert n1.impact and n1.evidence
    # evidence strings ⊆ allowed set built from facts
    facts = build_facts(_drift_items())
    allowed = facts.by_key[n1.key].allowed_evidence
    assert all(e in allowed for e in n1.evidence)


def test_explain_no_llm_key_template_mode(monkeypatch):
    monkeypatch.delenv("CFGDRIFT_LLM_KEY", raising=False)
    narratives = ExplainEngine().generate(_drift_items())
    assert len(narratives) == 2
    assert all(n.source == "template" for n in narratives)
    assert all(n.impact and n.evidence for n in narratives)


def test_explain_llm_fabrication_falls_back():
    reply = json.dumps([
        {
            "key": "services.web.ports[0]",
            "impact": "影响 services.api.port 配置",  # fabricated key
            "evidence": ["key: services.web.ports[0]"],
        }
    ])
    narratives = ExplainEngine().generate(
        _drift_items(), llm_backend=_MockLLM(reply), allow_llm=True,
    )
    assert narratives[0].source == "template"  # rejected -> fallback


def test_explain_llm_valid_replaces_source():
    reply = json.dumps([
        {
            "key": "services.api.image",
            "impact": "容器镜像从 nginx:1.25 改为 nginx:latest，使用 latest 标签可能导致部署不可复现",
            "evidence": ["key: services.api.image",
                         "value: \"nginx:1.25\" -> \"nginx:latest\""],
        }
    ])
    narratives = ExplainEngine().generate(
        _drift_items(), llm_backend=_MockLLM(reply), allow_llm=True,
    )
    assert narratives[1].source == "llm"
    assert narratives[0].source == "template"


def test_explain_format_text_and_json_cli(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    conf = tmp_path / "conf"
    conf.mkdir()
    (conf / "app.json").write_text(json.dumps({"server": {"port": 8080}}),
                                   encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": SRC, "CFGDRIFT_HOME": str(home)}
    r = subprocess.run(
        [PY, "-m", "cfgdrift.cli", "baseline", "create", "prod",
         "--scan-root", str(conf), "--format", "json"],
        capture_output=True, text=True, env=env, cwd=ROOT, timeout=120,
    )
    assert r.returncode == 0, r.stderr
    (conf / "app.json").write_text(json.dumps({"server": {"port": 9090}}),
                                   encoding="utf-8")
    r2 = subprocess.run(
        [PY, "-m", "cfgdrift.cli", "explain", str(conf), "--baseline", "prod",
         "--no-llm", "--format", "json"],
        capture_output=True, text=True, env=env, cwd=ROOT, timeout=120,
    )
    assert r2.returncode == 1
    payload = json.loads(r2.stdout)
    assert payload["code"] == 0
    for narrative in payload["data"]:
        assert narrative["source"] == "template"
        assert narrative["impact"] and narrative["evidence"]
    # text format renders a human block
    r3 = subprocess.run(
        [PY, "-m", "cfgdrift.cli", "explain", str(conf), "--baseline", "prod",
         "--no-llm", "--format", "text"],
        capture_output=True, text=True, env=env, cwd=ROOT, timeout=120,
    )
    assert "漂移业务影响分析" in r3.stdout
    assert "source: template" in r3.stdout


def test_explain_masking_d7_no_sensitive_leak():
    """Sensitive keys are masked in narratives (values never leak)."""
    items = [{
        "key_path": "db.password",
        "change_type": "modified",
        "severity": "CRITICAL",
        "old_value": "s3cr3t!",
        "new_value": "n3wp@ss",
        "constraint_violations": [],
    }]
    from cfgdrift.core.masker import default_masker

    masker = default_masker()
    masked = []
    for item in items:
        data = dict(item)
        masker.mask_item(data)
        masked.append(data)
    assert masked[0]["old_value"] == "******"
    narratives = ExplainEngine().generate(masked)
    text = narratives[0].impact + " ".join(narratives[0].evidence)
    assert "s3cr3t" not in text and "n3wp" not in text
    assert "******" in text


def test_explain_diff_explain_appends_block(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    conf = tmp_path / "conf"
    conf.mkdir()
    (conf / "app.json").write_text(json.dumps({"server": {"port": 8080}}),
                                   encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": SRC, "CFGDRIFT_HOME": str(home)}
    subprocess.run(
        [PY, "-m", "cfgdrift.cli", "baseline", "create", "prod",
         "--scan-root", str(conf), "--format", "json"],
        capture_output=True, text=True, env=env, cwd=ROOT, timeout=120,
    )
    (conf / "app.json").write_text(json.dumps({"server": {"port": 9090}}),
                                   encoding="utf-8")
    r2 = subprocess.run(
        [PY, "-m", "cfgdrift.cli", "diff", str(conf), "--baseline", "prod",
         "--no-llm", "--explain"],
        capture_output=True, text=True, env=env, cwd=ROOT, timeout=120,
    )
    assert r2.returncode == 1
    assert "漂移业务影响分析" in r2.stdout
    assert "source: template" in r2.stdout


# ---------------------------------------------------------------------------
# f. regression / version contract
# ---------------------------------------------------------------------------


def test_version_contract():
    import cfgdrift

    assert cfgdrift.__version__ == "0.11.0"
    # pyproject.toml
    with open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as fh:
        assert 'version = "0.11.0"' in fh.read()
    # C backend version
    try:
        from cfgdrift import _cfgdrift

        assert _cfgdrift.version() == "0.11.0-c"
    except ImportError:
        pytest.skip("C backend not built in this environment")


def test_legacy_diff_no_constraints_zero_noise():
    differ = SemanticDiffer()
    items, summary = differ.diff_snapshot(
        {"a.json": {"x": 1}}, {"a.json": {"x": 2}},
    )
    d = items[0].to_dict()
    assert "constraint_violations" not in d  # zero-noise


def test_kappa_cli_less_than_two_annotators_exit2(ws, tmp_path):
    _write_instances(ws, n=2)
    r = subprocess.run(
        [PY, "-m", "cfgdrift.cli", "corpus", "annotate", "--workspace", ws.root,
         "--annotator", "alice", "--batch", _batch_file(
             tmp_path, {"inst-0": {"annotation": "minor"}})],
        capture_output=True, text=True, env={**os.environ, "PYTHONPATH": SRC},
        cwd=ROOT, timeout=120,
    )
    assert r.returncode == 0
    r = subprocess.run(
        [PY, "-m", "cfgdrift.cli", "corpus", "kappa", "--workspace", ws.root],
        capture_output=True, text=True, env={**os.environ, "PYTHONPATH": SRC},
        cwd=ROOT, timeout=120,
    )
    assert r.returncode == 2
    assert "至少 2 名标注人" in r.stderr


def test_annotation_values_enum():
    assert ANNOTATION_VALUES == ("severe", "minor", "normal")
