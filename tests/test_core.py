"""Unit tests: C parsers, YAML normalization, diff, severity, ignore rules."""

from __future__ import annotations

import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

try:  # C extension is optional since v0.2.0 (pure-Python fallback exists)
    from cfgdrift._cfgdrift import parse_ini, parse_json, parse_toml, version  # noqa: E402
    _HAVE_C = True
except ImportError:  # pragma: no cover - pure-Python installs
    _HAVE_C = False

requires_c = pytest.mark.skipif(
    not _HAVE_C, reason="C extension not available (cfgdrift._cfgdrift)"
)

from cfgdrift.core.differ import SemanticDiffer, SeverityEngine  # noqa: E402
from cfgdrift.core.model import (  # noqa: E402
    ChangeType,
    DriftItem,
    IgnoreRule,
    Severity,
    escape_segment,
    join_path,
)
from cfgdrift.core.parser import _normalize, detect_format, parse_file, parse_text  # noqa: E402
from cfgdrift.rules.ignore import make_rule  # noqa: E402


# ---------------------------------------------------------------------------
# C extension basics
# ---------------------------------------------------------------------------

@requires_c
def test_c_version():
    assert isinstance(version(), str)
    assert version().startswith("0.5.0")


# ---------------------------------------------------------------------------
# JSON (C backend)
# ---------------------------------------------------------------------------

@requires_c
def test_json_basic():
    text = '{"a": 1, "b": [true, false, null], "c": {"d": 1.5}, "e": "héllo"}'
    data = parse_json(text)
    assert data == {"a": 1, "b": [True, False, None], "c": {"d": 1.5}, "e": "héllo"}
    assert isinstance(data["a"], int)
    assert isinstance(data["c"]["d"], float)


@requires_c
def test_json_unicode_escapes():
    text = r'{"s": "\u4e2d\u6587", "emoji": "\ud83d\ude00"}'
    data = parse_json(text)
    assert data["s"] == "中文"
    assert data["emoji"] == "\U0001F600"


@requires_c
def test_json_numbers():
    data = parse_json('{"i": -17, "f": 3.14, "e": 1e3, "E": -2.5E-2}')
    assert data["i"] == -17
    assert data["f"] == 3.14
    assert data["e"] == 1000.0
    assert abs(data["E"] - (-0.025)) < 1e-9


@requires_c
def test_json_duplicate_key_last_wins():
    data = parse_json('{"a": 1, "a": 2}')
    assert data == {"a": 2}


@requires_c
def test_json_errors():
    with pytest.raises(ValueError, match=r"line 1, column 7"):
        parse_json('{"a": }')
    with pytest.raises(ValueError, match=r"trailing comma"):
        parse_json("[1, 2,]")
    with pytest.raises(ValueError, match=r"single quote"):
        parse_json("{'a': 1}")
    with pytest.raises(ValueError, match=r"line 3"):
        parse_json('{\n"a": 1,\n}')


# ---------------------------------------------------------------------------
# TOML (C backend)
# ---------------------------------------------------------------------------

@requires_c
def test_toml_basic():
    text = """
title = "TOML Example"
num = 42
neg = -17
float = 3.14
bool = true
arr = [1, 2, 3]
inline = { x = 1, y = "two" }
date = 1979-05-27
time = 07:32:00
"""
    data = parse_toml(text)
    assert data["title"] == "TOML Example"
    assert data["num"] == 42
    assert data["neg"] == -17
    assert data["float"] == 3.14
    assert data["bool"] is True
    assert data["arr"] == [1, 2, 3]
    assert data["inline"] == {"x": 1, "y": "two"}
    assert data["date"] == "1979-05-27"  # datetime -> ISO string
    assert data["time"] == "07:32:00"


@requires_c
def test_toml_int_bases_and_underscores():
    data = parse_toml("a = 1_000\nb = 0xDEADBEEF\nc = 0o755\nd = 0b1010\n")
    assert data["a"] == 1000
    assert data["b"] == 0xDEADBEEF
    assert data["c"] == 0o755
    assert data["d"] == 0b1010


@requires_c
def test_toml_strings():
    text = (
        'a = "basic\\nstring"\n'
        "b = 'literal\\nno'\n"
        'c = """\nmulti\nline\n"""\n'
        "d = '''\nraw\n'''\n"
    )
    data = parse_toml(text)
    assert data["a"] == "basic\nstring"
    assert data["b"] == "literal\\nno"
    assert data["c"] == "multi\nline\n"
    assert data["d"] == "raw\n"


@requires_c
def test_toml_tables_and_array_of_tables():
    text = """
[server]
host = "localhost"
port = 8080

[server.tls]
enabled = true

[[products]]
name = "Hammer"
[[products]]
name = "Nail"
"""
    data = parse_toml(text)
    assert data["server"]["host"] == "localhost"
    assert data["server"]["tls"]["enabled"] is True
    assert data["products"] == [{"name": "Hammer"}, {"name": "Nail"}]


@requires_c
def test_toml_dotted_keys():
    data = parse_toml("server.tls.enabled = true\nname = \"root\"\n")
    assert data == {"server": {"tls": {"enabled": True}}, "name": "root"}


@requires_c
def test_toml_duplicate_key_errors():
    with pytest.raises(ValueError, match="duplicate key"):
        parse_toml("a = 1\na = 2\n")
    with pytest.raises(ValueError, match="duplicate table"):
        parse_toml("[a]\nx = 1\n[a]\ny = 2\n")


# ---------------------------------------------------------------------------
# INI (C backend)
# ---------------------------------------------------------------------------

@requires_c
def test_ini_basic():
    text = """
# comment
; also comment
top = value
[section]
key1 = hello
key2: world
quoted = "  trimmed  "
clean = "hello"
CaseKey = kept
"""
    data = parse_ini(text)
    assert data["top"] == "value"
    assert data["section"]["key1"] == "hello"
    assert data["section"]["key2"] == "world"
    # Paired quotes are stripped; whitespace INSIDE quotes is preserved.
    assert data["section"]["quoted"] == "  trimmed  "
    assert data["section"]["clean"] == "hello"
    assert "CaseKey" in data["section"]
    assert data["section"]["CaseKey"] == "kept"


@requires_c
def test_ini_duplicate_last_wins():
    data = parse_ini("a = 1\na = 2\n")
    assert data["a"] == "2"


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------

def test_utf8_and_gbk_chinese(tmp_path):
    utf8_file = tmp_path / "utf8.json"
    utf8_file.write_bytes('{"name": "中文"}'.encode("utf-8"))
    data = parse_file(str(utf8_file), "json", warn=False)
    assert data["name"] == "中文"

    gbk_file = tmp_path / "gbk.ini"
    gbk_file.write_bytes("title = 配置项\n".encode("gbk"))
    data = parse_file(str(gbk_file), "ini", warn=False)
    assert data["title"] == "配置项"


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------

def test_detect_format():
    assert detect_format("a.json") == "json"
    assert detect_format("a.yaml") == "yaml"
    assert detect_format("a.yml") == "yaml"
    assert detect_format("a.toml") == "toml"
    assert detect_format("a.ini") == "ini"
    assert detect_format("a.cfg") == "ini"
    assert detect_format("a.conf") == "ini"
    assert detect_format("a.unknown") is None


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def test_normalize_top_level_wrap():
    assert _normalize(42) == 42
    assert _normalize([1, 2]) == [1, 2]
    assert parse_text("[1, 2]", "json") == {"$": [1, 2]}
    assert parse_text("42", "json") == {"$": 42}
    assert parse_text('"hi"', "json") == {"$": "hi"}


def test_normalize_yaml_datetime(tmp_path):
    f = tmp_path / "d.yaml"
    f.write_text("date: 2020-01-01\n", encoding="utf-8")
    data = parse_file(str(f), "yaml", warn=False)
    assert data["date"] == "2020-01-01"
    assert isinstance(data["date"], str)


def test_yaml_multi_document_errors(tmp_path):
    f = tmp_path / "m.yaml"
    f.write_text("a: 1\n---\nb: 2\n", encoding="utf-8")
    with pytest.raises(ValueError):
        parse_file(str(f), "yaml", warn=False)


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------

def _differ():
    return SemanticDiffer()


def test_diff_four_change_types():
    old = {"a": 1, "b": "x", "c": True, "d": [1, 2]}
    new = {"a": 1, "b": "y", "c": 42, "e": "new"}
    items, summary = _differ().diff(old, new, file="conf.json")
    by_path = {it.key_path: it for it in items}

    assert by_path["b"].change_type == ChangeType.MODIFIED
    assert by_path["b"].severity == Severity.WARN
    assert by_path["c"].change_type == ChangeType.TYPE_CHANGED
    assert by_path["c"].severity == Severity.CRITICAL
    assert by_path["d"].change_type == ChangeType.REMOVED
    assert by_path["d"].severity == Severity.CRITICAL
    assert by_path["e"].change_type == ChangeType.ADDED
    assert by_path["e"].severity == Severity.INFO

    assert summary.added == 1
    assert summary.removed == 1
    assert summary.modified == 1
    assert summary.type_changed == 1
    assert summary.total == 4
    assert summary.max_severity == Severity.CRITICAL


def test_diff_list_by_index():
    old = {"servers": [{"host": "a"}, {"host": "b"}]}
    new = {"servers": [{"host": "a"}, {"host": "c"}]}
    items, _ = _differ().diff(old, new, file="t.toml")
    assert any(it.key_path == "servers[1].host" for it in items)


def test_diff_int_float_is_type_change():
    items, _ = _differ().diff({"n": 1}, {"n": 1.0}, file="f.json")
    assert len(items) == 1
    assert items[0].change_type == ChangeType.TYPE_CHANGED


def test_diff_ignores_key_order():
    items, summary = _differ().diff(
        {"a": 1, "b": 2}, {"b": 2, "a": 1}, file="f.json"
    )
    assert items == []
    assert summary.total == 0
    assert summary.max_severity == Severity.NONE


def test_diff_file_level_drift():
    old_snap = {"a.json": {"x": 1}, "gone.json": {"y": 2}}
    new_snap = {"a.json": {"x": 1}, "new.json": {"z": 3}}
    items, summary = _differ().diff_snapshot(old_snap, new_snap)
    by_file = {it.file: it for it in items}

    assert by_file["gone.json"].change_type == ChangeType.REMOVED
    assert by_file["gone.json"].severity == Severity.CRITICAL
    assert by_file["gone.json"].key_path == ""
    assert by_file["new.json"].change_type == ChangeType.ADDED
    assert by_file["new.json"].severity == Severity.INFO
    assert summary.removed == 1
    assert summary.added == 1


def test_severity_engine():
    assert SeverityEngine.classify(ChangeType.REMOVED) == Severity.CRITICAL
    assert SeverityEngine.classify(ChangeType.TYPE_CHANGED) == Severity.CRITICAL
    assert SeverityEngine.classify(ChangeType.MODIFIED) == Severity.WARN
    assert SeverityEngine.classify(ChangeType.ADDED) == Severity.INFO


# ---------------------------------------------------------------------------
# Ignore rules
# ---------------------------------------------------------------------------

def _item(key_path, change_type="modified", file="f.json", severity=Severity.WARN):
    return DriftItem(
        key_path=key_path,
        change_type=ChangeType(change_type),
        severity=severity,
        file=file,
    )


def test_ignore_rule_exact():
    rule = make_rule("r", "server.port", "path_exact")
    assert rule.matches(_item("server.port"))
    assert not rule.matches(_item("server.port.extra"))


def test_ignore_rule_prefix():
    rule = make_rule("r", "server.", "path_prefix")
    assert rule.matches(_item("server.tls.enabled"))
    assert not rule.matches(_item("servers"))


def test_ignore_rule_regex():
    rule = make_rule("r", r"servers\[\d+\]\.host", "regex")
    assert rule.matches(_item("servers[0].host"))
    assert not rule.matches(_item("servers[0].port"))


def test_ignore_rule_file_and_change_type_filter():
    rule = make_rule(
        "r", "debug", "path_prefix",
        file_pattern=r"\.yaml$", change_type="added",
    )
    assert rule.matches(_item("debug.log", change_type="added", file="a.yaml"))
    assert not rule.matches(_item("debug.log", change_type="removed", file="a.yaml"))
    assert not rule.matches(_item("debug.log", change_type="added", file="a.json"))


def test_ignore_rule_applied_in_diff():
    old = {"server": {"port": 8080, "host": "a"}, "noise": 1}
    new = {"server": {"port": 9090, "host": "b"}, "noise": 2}
    rules = [make_rule("r", "server.", "path_prefix")]
    items, summary = _differ().diff(old, new, file="f.json", rules=rules)
    assert all(it.key_path.startswith("noise") for it in items)
    assert summary.ignored == 2


# ---------------------------------------------------------------------------
# Key path helpers
# ---------------------------------------------------------------------------

def test_join_path_and_escape():
    assert join_path([("key", "a"), ("key", "b"), ("index", 0), ("key", "c")]) == "a.b[0].c"
    assert escape_segment("a.b") == "a\\.b"
    assert escape_segment("x[y]") == "x\\[y\\]"
    assert escape_segment("a\\b") == "a\\\\b"


# ---------------------------------------------------------------------------
# Parse error message format (C backend)
# ---------------------------------------------------------------------------

@requires_c
def test_error_line_col_format():
    with pytest.raises(ValueError) as exc:
        parse_json('{\n  "a": 1,\n  "b": tru\n}')
    msg = str(exc.value)
    assert msg.startswith("parse error at line")
    assert "column" in msg
