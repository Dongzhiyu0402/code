"""explain narrative pipeline tests (v0.8.0, direction A)."""

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

from cfgdrift.explain.engine import ExplainEngine  # noqa: E402
from cfgdrift.explain.llm import LLMBackend, OpenAICompatBackend  # noqa: E402
from cfgdrift.explain.templates import (  # noqa: E402
    KEY_SEMANTICS,
    TemplateEngine,
    match_semantics,
    merge_schema,
)
from cfgdrift.explain.validator import EvidenceValidator, build_facts  # noqa: E402


def _items():
    return [
        {
            "key_path": "services.web.ports[0]",
            "change_type": "modified",
            "severity": "WARN",
            "old_value": "8080:80",
            "new_value": "9090:80",
            "constraint_violations": [
                {
                    "constraint_id": "http_port_range",
                    "type": "range",
                    "message": "端口超出允许范围",
                    "involved_keys": ["services.web.ports[0]"],
                }
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
        {
            "key_path": "logging.level",
            "change_type": "added",
            "severity": "INFO",
            "old_value": None,
            "new_value": "debug",
            "constraint_violations": [],
        },
    ]


class _MockLLM(LLMBackend):
    def __init__(self, reply=None, available=True):
        self.reply = reply
        self.available_flag = available
        self.prompt = None

    def available(self):
        return self.available_flag

    def generate(self, prompt):
        self.prompt = prompt
        return self.reply


def test_key_semantics_builtin_count_and_match():
    # The built-in dictionary covers the 24 documented patterns.
    assert len(KEY_SEMANTICS) >= 24
    assert match_semantics("services.web.ports[0]", merge_schema(None))[1] == "监听端口"
    assert match_semantics("tls.enabled", merge_schema(None))[1].startswith("传输安全")
    assert match_semantics("db.password", merge_schema(None))[1] == "口令（敏感）"


def test_merge_schema_user_overrides():
    merged = merge_schema({"port": "自定义端口语义"})
    assert merged["port"] == "自定义端口语义"
    assert match_semantics("x.port", merged) == ("port", "自定义端口语义")
    # A brand-new user regex is matched first.
    merged2 = merge_schema({"myapp_redis": "Redis 配置"})
    assert match_semantics("myapp_redis.host", merged2) == (
        "myapp_redis", "Redis 配置",
    )


def test_template_deterministic():
    engine = TemplateEngine()
    a = engine.render(_items()[0])
    b = engine.render(_items()[0])
    assert a.to_dict() == b.to_dict()
    assert a.source == "template"
    assert a.impact
    assert a.evidence
    assert "http_port_range" in a.impact


def test_template_image_latest_special_case():
    engine = TemplateEngine()
    item = _items()[1]
    n = engine.render(item)
    assert "latest" in n.impact
    assert "不可复现" in n.impact


def test_template_severity_fallback():
    engine = TemplateEngine()
    n = engine.render(_items()[2])
    assert "新增" in n.impact
    assert n.source == "template"


def test_build_facts_whitelist():
    facts = build_facts(_items())
    assert "services.web.ports[0]" in facts.keys
    assert "http_port_range" in facts.constraints
    item = facts.by_key["services.web.ports[0]"]
    assert "key: services.web.ports[0]" in item.allowed_evidence
    assert "constraint: http_port_range 违反" in item.allowed_evidence


def test_evidence_validator_accepts_allowed():
    facts = build_facts(_items())
    narrative = {
        "key": "services.web.ports[0]",
        "impact": "端口变更导致约束违反",
        "evidence": [
            "key: services.web.ports[0]",
            "constraint: http_port_range 违反",
        ],
    }
    ok, reasons = EvidenceValidator.validate(narrative, facts)
    assert ok, reasons


def test_evidence_validator_rejects_fabricated_evidence():
    facts = build_facts(_items())
    narrative = {
        "key": "services.web.ports[0]",
        "impact": "影响很大",
        "evidence": ["key: services.web.ports[0]", "key: services.db.host"],
    }
    ok, reasons = EvidenceValidator.validate(narrative, facts)
    assert not ok
    assert any("not derived" in r for r in reasons)


def test_evidence_validator_rejects_fabricated_key_in_impact():
    facts = build_facts(_items())
    narrative = {
        "key": "services.web.ports[0]",
        "impact": "端口变更影响 services.api.port 与外部访问",
        "evidence": ["key: services.web.ports[0]"],
    }
    ok, reasons = EvidenceValidator.validate(narrative, facts)
    assert not ok
    assert any("not among the input facts" in r for r in reasons)


def test_evidence_validator_rejects_fabricated_constraint():
    facts = build_facts(_items())
    narrative = {
        "key": "services.web.ports[0]",
        "impact": "违反 my_fake_rule 约束",
        "evidence": ["key: services.web.ports[0]"],
    }
    ok, reasons = EvidenceValidator.validate(narrative, facts)
    assert not ok
    assert any("constraint" in r for r in reasons)


def test_evidence_validator_rejects_unknown_key():
    facts = build_facts(_items())
    narrative = {
        "key": "ghost.key",
        "impact": "x",
        "evidence": ["key: ghost.key"],
    }
    ok, reasons = EvidenceValidator.validate(narrative, facts)
    assert not ok


def test_engine_template_only():
    narratives = ExplainEngine().generate(_items())
    assert all(n.source == "template" for n in narratives)
    assert len(narratives) == 3


def test_engine_llm_valid_replaces():
    reply = json.dumps(
        [
            {
                "key": "services.web.ports[0]",
                "impact": "端口从 8080 改为 9090 且违反端口约束",
                "evidence": [
                    "key: services.web.ports[0]",
                    "value: \"8080:80\" -> \"9090:80\"",
                    "constraint: http_port_range 违反",
                ],
            }
        ]
    )
    narratives = ExplainEngine().generate(
        _items(), llm_backend=_MockLLM(reply), allow_llm=True
    )
    assert narratives[0].source == "llm"
    assert narratives[1].source == "template"
    assert narratives[2].source == "template"


def test_engine_llm_fabrication_falls_back():
    reply = json.dumps(
        [
            {
                "key": "services.web.ports[0]",
                "impact": "影响 services.api.port 配置",
                "evidence": ["key: services.web.ports[0]"],
            }
        ]
    )
    narratives = ExplainEngine().generate(
        _items(), llm_backend=_MockLLM(reply), allow_llm=True
    )
    assert narratives[0].source == "template"  # fabricated key -> fallback


def test_engine_llm_parse_failure_falls_back():
    narratives = ExplainEngine().generate(
        _items(), llm_backend=_MockLLM("not json"), allow_llm=True
    )
    assert all(n.source == "template" for n in narratives)


def test_engine_llm_fenced_json_parsed():
    reply = "```json\n[{\"key\": \"services.api.image\", \"impact\": \"镜像变化\", \"evidence\": [\"key: services.api.image\"]}]\n```"
    narratives = ExplainEngine().generate(
        _items(), llm_backend=_MockLLM(reply), allow_llm=True
    )
    assert narratives[1].source == "llm"


def test_engine_llm_unavailable_falls_back():
    narratives = ExplainEngine().generate(
        _items(), llm_backend=_MockLLM(available=False), allow_llm=True
    )
    assert all(n.source == "template" for n in narratives)


def test_openai_backend_unavailable_without_key(monkeypatch):
    monkeypatch.delenv("CFGDRIFT_LLM_KEY", raising=False)
    backend = OpenAICompatBackend()
    assert not backend.available()
    assert backend.generate("prompt") is None


def test_explain_cli_text(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    conf = tmp_path / "conf"
    conf.mkdir()
    (conf / "app.json").write_text(
        json.dumps({"server": {"port": 8080}}), encoding="utf-8"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    env["CFGDRIFT_HOME"] = str(home)
    r = subprocess.run(
        [PY, "-m", "cfgdrift.cli", "baseline", "create", "prod",
         "--scan-root", str(conf), "--format", "json"],
        capture_output=True, text=True, env=env, cwd=ROOT, timeout=120,
    )
    assert r.returncode == 0, r.stderr
    (conf / "app.json").write_text(
        json.dumps({"server": {"port": 9090}}), encoding="utf-8"
    )
    r2 = subprocess.run(
        [PY, "-m", "cfgdrift.cli", "explain", str(conf), "--baseline", "prod",
         "--no-llm", "--format", "json"],
        capture_output=True, text=True, env=env, cwd=ROOT, timeout=120,
    )
    assert r2.returncode == 1  # drift detected
    payload = json.loads(r2.stdout)
    assert payload["code"] == 0
    data = payload["data"]
    assert data
    for narrative in data:
        assert narrative["impact"]
        assert narrative["evidence"]
        assert narrative["source"] == "template"


def test_explain_cli_schema_merge(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    conf = tmp_path / "conf"
    conf.mkdir()
    (conf / "app.json").write_text(
        json.dumps({"myapp_redis": {"host": "localhost"}}), encoding="utf-8"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    env["CFGDRIFT_HOME"] = str(home)
    subprocess.run(
        [PY, "-m", "cfgdrift.cli", "baseline", "create", "prod",
         "--scan-root", str(conf), "--format", "json"],
        capture_output=True, text=True, env=env, cwd=ROOT, timeout=120,
    )
    (conf / "app.json").write_text(
        json.dumps({"myapp_redis": {"host": "10.0.0.1"}}), encoding="utf-8"
    )
    schema = tmp_path / "schema.yaml"
    schema.write_text(
        "version: 1\npatterns:\n  myapp_redis: Redis 配置\n", encoding="utf-8"
    )
    r2 = subprocess.run(
        [PY, "-m", "cfgdrift.cli", "explain", str(conf), "--baseline", "prod",
         "--no-llm", "--schema", str(schema), "--format", "json"],
        capture_output=True, text=True, env=env, cwd=ROOT, timeout=120,
    )
    assert r2.returncode == 1
    payload = json.loads(r2.stdout)
    assert "Redis 配置" in payload["data"][0]["impact"]


def test_diff_explain_appends_block(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    conf = tmp_path / "conf"
    conf.mkdir()
    (conf / "app.json").write_text(
        json.dumps({"server": {"port": 8080}}), encoding="utf-8"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    env["CFGDRIFT_HOME"] = str(home)
    subprocess.run(
        [PY, "-m", "cfgdrift.cli", "baseline", "create", "prod",
         "--scan-root", str(conf), "--format", "json"],
        capture_output=True, text=True, env=env, cwd=ROOT, timeout=120,
    )
    (conf / "app.json").write_text(
        json.dumps({"server": {"port": 9090}}), encoding="utf-8"
    )
    r2 = subprocess.run(
        [PY, "-m", "cfgdrift.cli", "diff", str(conf), "--baseline", "prod",
         "--no-llm", "--explain"],
        capture_output=True, text=True, env=env, cwd=ROOT, timeout=120,
    )
    assert r2.returncode == 1  # drift-based exit code (unchanged by explain)
    assert "漂移业务影响分析" in r2.stdout
    assert "source: template" in r2.stdout


def test_diff_compare_explain_rejected(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    env["CFGDRIFT_HOME"] = str(home)
    r = subprocess.run(
        [PY, "-m", "cfgdrift.cli", "diff", "--compare", "--env1", "a",
         "--env2", "b", "--explain"],
        capture_output=True, text=True, env=env, cwd=ROOT, timeout=120,
    )
    assert r.returncode == 2
    assert "--explain is not supported with --compare" in r.stderr
