"""SeverityRule.constraint_id + differ pipeline tests (v0.8.0, C-13/D1)."""

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

from cfgdrift.core.constraints import ConstraintEngine  # noqa: E402
from cfgdrift.core.differ import SemanticDiffer  # noqa: E402
from cfgdrift.core.model import (  # noqa: E402
    ChangeType,
    Constraint,
    DriftItem,
    Severity,
    SeverityRule,
)

RANGE_C = Constraint(
    id="http_port_range",
    type="range",
    message="端口超出允许范围",
    severity=Severity.WARN,
    keys=["server.port"],
    min=1,
    max=65535,
)


def _item(key="server.port", change=ChangeType.MODIFIED, severity=Severity.WARN,
          file="app.json", old=8080, new=99999):
    return DriftItem(
        key_path=key, change_type=change, severity=severity, file=file,
        old_value=old, new_value=new,
        old_type="int" if old is not None else None,
        new_type="int" if new is not None else None,
    )


def test_constraint_id_normalization():
    rule = SeverityRule(name="r", severity=Severity.CRITICAL, constraint_id=["a"])
    assert rule.constraint_id == ["a"]
    rule2 = SeverityRule.from_dict(
        {"name": "r2", "severity": "CRITICAL", "constraint_id": "http_port_range"}
    )
    assert rule2.constraint_id == ["http_port_range"]
    rule3 = SeverityRule.from_dict(
        {"name": "r3", "severity": "CRITICAL",
         "constraint_id": ["a", "a", "b"]}
    )
    assert rule3.constraint_id == ["a", "b"]  # dedup + order


def test_constraint_id_invalid_raises():
    with pytest.raises(ValueError):
        SeverityRule(name="r", severity=Severity.CRITICAL, constraint_id=[""])
    with pytest.raises(ValueError):
        SeverityRule.from_dict(
            {"name": "r", "severity": "CRITICAL", "constraint_id": 3}
        )


def test_to_dict_zero_noise():
    rule = SeverityRule(name="r", severity=Severity.WARN, key_pattern=".*port")
    out = rule.to_dict()
    assert "constraint_id" not in out  # zero-noise: old rules byte-identical
    rule2 = SeverityRule(
        name="r2", severity=Severity.CRITICAL, constraint_id=["http_port_range"]
    )
    out2 = rule2.to_dict()
    assert out2["constraint_id"] == ["http_port_range"]


def test_matches_with_constraint_id():
    rule = SeverityRule(
        name="r", severity=Severity.CRITICAL, constraint_id=["http_port_range"]
    )
    item = _item()
    # No violations -> no match.
    assert not rule.matches(item)
    item.constraint_violations = [
        {"constraint_id": "http_port_range", "type": "range", "message": "x",
         "involved_keys": ["server.port"]}
    ]
    assert rule.matches(item)
    # Explicit violated_constraint_ids wins over item derivation.
    assert not rule.matches(item, violated_constraint_ids={"other"})
    assert rule.matches(item, violated_constraint_ids={"http_port_range"})


def test_matches_multi_constraint_ids_and_semantics():
    rule = SeverityRule(
        name="r", severity=Severity.CRITICAL,
        constraint_id=["http_port_range", "db_port_range"],
    )
    item = _item()
    item.constraint_violations = [{"constraint_id": "db_port_range"}]
    assert rule.matches(item)  # intersection non-empty
    # AND semantics: key_pattern AND constraint_id must both hold.
    rule2 = SeverityRule(
        name="r2", severity=Severity.CRITICAL,
        constraint_id=["http_port_range"], key_pattern="server\\.port",
    )
    # key matches but constraint doesn't -> no match.
    assert not rule2.matches(item)
    item.constraint_violations = [{"constraint_id": "http_port_range"}]
    assert rule2.matches(item)  # both conditions satisfied


def test_finish_pipeline_constraint_id_override(tmp_path):
    """D1: attach -> severity override (constraint_id rule) -> upgrade."""
    differ = SemanticDiffer()
    old = {"server": {"port": 8080}}
    new = {"server": {"port": 99999}}
    rule = SeverityRule(
        name="port-critical", severity=Severity.CRITICAL,
        constraint_id=["http_port_range"],
    )
    items, summary = differ.diff_snapshot(
        {"app.json": old}, {"app.json": new},
        severity_rules=[rule],
        constraints=[RANGE_C],
    )
    assert len(items) == 1
    it = items[0]
    # Constraint violated -> rule matches -> severity CRITICAL (override);
    # upgrade formula min(3, max(3+1, 2)) = 3 stays CRITICAL.
    assert it.severity == Severity.CRITICAL
    assert it.constraint_violations
    assert summary.max_severity == Severity.CRITICAL


def test_finish_pipeline_no_constraint_id_unchanged(tmp_path):
    """Without constraint_id rules the v0.7.0 behavior is byte-identical."""
    differ = SemanticDiffer()
    old = {"server": {"port": 8080}}
    new = {"server": {"port": 99999}}
    rule = SeverityRule(
        name="port-warn", severity=Severity.WARN, key_pattern="server\\.port",
    )
    items, summary = differ.diff_snapshot(
        {"app.json": old}, {"app.json": new},
        severity_rules=[rule],
        constraints=[RANGE_C],
    )
    it = items[0]
    # v0.7.0: WARN override then upgrade -> min(3, max(2+1, 1)) = 3? No:
    # RANGE_C severity WARN (rank 2) -> min(3, max(2+1, 2)) = 3.
    assert it.severity == Severity.CRITICAL


def test_finish_pipeline_constraint_id_no_violation_no_override():
    """Rule with constraint_id does NOT match when constraint is fine."""
    differ = SemanticDiffer()
    old = {"server": {"port": 8080}}
    new = {"server": {"port": 8081}}  # within range -> no violation
    rule = SeverityRule(
        name="port-critical", severity=Severity.CRITICAL,
        constraint_id=["http_port_range"],
    )
    items, summary = differ.diff_snapshot(
        {"app.json": old}, {"app.json": new},
        severity_rules=[rule],
        constraints=[RANGE_C],
    )
    it = items[0]
    assert it.severity == Severity.WARN  # default modified severity, not CRITICAL
    assert not it.constraint_violations


def test_apply_backward_compatible():
    item = _item(new=99999)
    ConstraintEngine.apply({"app.json": {"server": {"port": 99999}}}, [item], [RANGE_C])
    assert item.severity == Severity.CRITICAL  # upgrade from WARN


def test_attach_does_not_upgrade_then_upgrade():
    item = _item(new=99999)
    ConstraintEngine.attach(
        {"app.json": {"server": {"port": 99999}}}, [item], [RANGE_C]
    )
    assert item.severity == Severity.WARN  # attach only, no upgrade
    assert item.constraint_violations
    ConstraintEngine.upgrade([item], [RANGE_C])
    assert item.severity == Severity.CRITICAL


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def test_severity_add_list_cli(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    env["CFGDRIFT_HOME"] = str(home)
    r = subprocess.run(
        [PY, "-m", "cfgdrift.cli", "severity", "add", "--name", "p",
         "--severity", "CRITICAL", "--constraint-id", "http_port_range,db_port_range"],
        capture_output=True, text=True, env=env, cwd=ROOT, timeout=120,
    )
    assert r.returncode == 0, r.stderr
    assert "constraint=http_port_range,db_port_range" in r.stdout
    # severity.yaml persisted with constraint_id list.
    from cfgdrift.rules.severity import SeverityConfig, default_path

    rules = SeverityConfig.load(default_path(str(home)))
    assert rules[0].constraint_id == ["http_port_range", "db_port_range"]


def test_severity_list_cli_shows_constraint(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _write(
        os.path.join(home, "severity.yaml"),
        "version: 1\nrules:\n"
        "  - name: p\n    severity: CRITICAL\n"
        "    constraint_id: http_port_range\n"
        "  - name: legacy\n    severity: WARN\n    key_pattern: '.*tls'\n",
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    env["CFGDRIFT_HOME"] = str(home)
    r = subprocess.run(
        [PY, "-m", "cfgdrift.cli", "severity", "list"],
        capture_output=True, text=True, env=env, cwd=ROOT, timeout=120,
    )
    assert r.returncode == 0
    assert "constraint=http_port_range" in r.stdout
    assert "constraint=-" in r.stdout  # legacy rule shows dash
