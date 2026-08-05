"""compare constraint-check loop tests (v0.8.0, D10)."""

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

from cfgdrift.core.compare import CompareEngine  # noqa: E402
from cfgdrift.core.constraints import ConstraintEngine  # noqa: E402
from cfgdrift.core.model import (  # noqa: E402
    Constraint,
    Severity,
)
from cfgdrift.storage.store import Store  # noqa: E402

RANGE_C = Constraint(
    id="test_port_range",
    type="range",
    message="端口 {value} 超出允许范围 {min}-{max}",
    severity=Severity.CRITICAL,
    keys=["server.port"],
    min=8000,
    max=9000,
)


def _make_store(tmp_path, baselines):
    """Create a Store with named baselines ``{name: data_dict}``.

    The baseline ``data`` is a snapshot ``{relpath: tree}`` (the shape the
    differ/check_tree consume); ``app.json`` is the single file.
    """
    store = Store(str(tmp_path / "test.db"))
    for name, data in baselines.items():
        store.create_baseline(
            name=name,
            description="",
            scan_root=str(tmp_path),
            format="json",
            data={"app.json": data},
            line_maps=None,
        )
    return store


def test_compare_report_zero_noise_to_dict():
    from cfgdrift.core.model import CompareReport, ScanSummary

    rep = CompareReport(
        baseline_a="a", baseline_b="b", created_at="t",
        summary=ScanSummary(), items=[],
    )
    out = rep.to_dict()
    assert "constraint_violations" not in out  # zero-noise contract


def test_compare_report_to_dict_with_violations():
    from cfgdrift.core.model import CompareReport, ScanSummary

    rep = CompareReport(
        baseline_a="a", baseline_b="b", created_at="t",
        summary=ScanSummary(), items=[],
        constraint_violations={"env_a": [{"constraint_id": "c"}]},
    )
    out = rep.to_dict()
    assert out["constraint_violations"]["env_a"][0]["constraint_id"] == "c"


def test_compare_constraints_split_by_env(tmp_path):
    store = _make_store(
        tmp_path,
        {
            "dev": {"server": {"port": 8080}},
            "prod": {"server": {"port": 9500}},  # violates test_port_range
        },
    )
    engine = CompareEngine(store)
    reports = engine.compare(
        ["dev", "prod"],
        constraints=[RANGE_C],
    )
    store.close()
    assert len(reports) == 1
    rep = reports[0]
    # env_b (prod) violates; env_a (dev) is fine.
    assert rep.constraint_violations["env_a"] == []
    assert len(rep.constraint_violations["env_b"]) == 1
    v = rep.constraint_violations["env_b"][0]
    assert v["constraint_id"] == "test_port_range"
    assert v["severity"] == "CRITICAL"
    assert "server.port" in v["involved_keys"]


def test_compare_no_constraints_zero_noise(tmp_path):
    store = _make_store(
        tmp_path,
        {
            "dev": {"server": {"port": 8080}},
            "prod": {"server": {"port": 8080}},
        },
    )
    engine = CompareEngine(store)
    reports = engine.compare(["dev", "prod"], constraints=None)
    store.close()
    assert reports[0].constraint_violations == {}


def test_compare_snapshots_forwards_constraints(tmp_path):
    store = _make_store(
        tmp_path,
        {
            "dev": {"server": {"port": 8080}},
            "prod": {"server": {"port": 9500}},
        },
    )
    engine = CompareEngine(store)
    items, summary = engine.compare_snapshots(
        "dev", "prod",
        {"app.json": {"server": {"port": 8080}}},
        {"app.json": {"server": {"port": 9500}}},
        constraints=[RANGE_C],
    )
    store.close()
    violated = [it for it in items if it.constraint_violations]
    assert violated, "the port change should attach a violation"
    assert violated[0].severity == Severity.CRITICAL


def test_compare_cli_constraint_block(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    store = _make_store(
        tmp_path,
        {
            "dev": {"server": {"port": 8080}},
            "prod": {"server": {"port": 9500}},
        },
    )
    store.close()
    constraints_file = tmp_path / "constraints.yaml"
    constraints_file.write_text(
        "version: 1\nrules:\n"
        "  - id: test_port_range\n"
        "    type: range\n"
        "    message: 端口超出范围\n"
        "    severity: CRITICAL\n"
        "    keys: [server.port]\n"
        "    min: 8000\n"
        "    max: 9000\n",
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    env["CFGDRIFT_HOME"] = str(home)
    # --no-builtin keeps only our custom constraint -> deterministic block.
    proc = subprocess.run(
        [PY, "-m", "cfgdrift.cli", "--store", str(tmp_path / "test.db"),
         "compare", "dev", "prod", "--no-builtin",
         "--constraints", str(constraints_file)],
        capture_output=True, text=True, env=env, cwd=ROOT, timeout=120,
    )
    assert proc.returncode == 1  # drift-based exit code (D6)
    assert "约束检查 (D10 补全)" in proc.stdout
    assert "[env_b: prod] CRITICAL test_port_range" in proc.stdout
    assert "key_path: server.port" in proc.stdout


def test_compare_cli_no_builtin_removes_violations(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    store = _make_store(
        tmp_path,
        {
            "dev": {"server": {"port": 8080}},
            "prod": {"server": {"port": 9500}},
        },
    )
    store.close()
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    env["CFGDRIFT_HOME"] = str(home)
    proc = subprocess.run(
        [PY, "-m", "cfgdrift.cli", "--store", str(tmp_path / "test.db"),
         "compare", "dev", "prod", "--no-builtin"],
        capture_output=True, text=True, env=env, cwd=ROOT, timeout=120,
    )
    assert proc.returncode == 1  # drift still present
    assert "约束检查" not in proc.stdout


def test_compare_cli_json_field(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    store = _make_store(
        tmp_path,
        {
            "dev": {"server": {"port": 8080}},
            "prod": {"server": {"port": 9500}},
        },
    )
    store.close()
    constraints_file = tmp_path / "constraints.yaml"
    constraints_file.write_text(
        "version: 1\nrules:\n"
        "  - id: test_port_range\n"
        "    type: range\n"
        "    message: 端口超出范围\n"
        "    severity: CRITICAL\n"
        "    keys: [server.port]\n"
        "    min: 8000\n"
        "    max: 9000\n",
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    env["CFGDRIFT_HOME"] = str(home)
    proc = subprocess.run(
        [PY, "-m", "cfgdrift.cli", "--store", str(tmp_path / "test.db"),
         "compare", "dev", "prod", "--no-builtin",
         "--constraints", str(constraints_file), "--json"],
        capture_output=True, text=True, env=env, cwd=ROOT, timeout=120,
    )
    assert proc.returncode == 1
    payload = json.loads(proc.stdout)
    data = payload["data"][0]
    assert "constraint_violations" in data
    assert data["constraint_violations"]["env_b"][0]["constraint_id"] == "test_port_range"


def test_compare_no_violation_output_unchanged(tmp_path):
    """Both envs clean -> no 约束检查 block (zero-noise, D6.8)."""
    home = tmp_path / "home"
    home.mkdir()
    store = _make_store(
        tmp_path,
        {
            "dev": {"server": {"port": 8080}},
            "prod": {"server": {"port": 8080}},
        },
    )
    store.close()
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    env["CFGDRIFT_HOME"] = str(home)
    proc = subprocess.run(
        [PY, "-m", "cfgdrift.cli", "--store", str(tmp_path / "test.db"),
         "compare", "dev", "prod"],
        capture_output=True, text=True, env=env, cwd=ROOT, timeout=120,
    )
    assert proc.returncode == 0
    assert "约束检查" not in proc.stdout
    assert "no differences" in proc.stdout
