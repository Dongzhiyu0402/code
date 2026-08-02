"""QA supplementary tests (independent, adversarial edge coverage).

These tests are written by the QA engineer (严过关) independently of the
engineer's tests/test_core.py + tests/test_cli.py.  They probe boundaries the
engineer may have missed: C-parser robustness, semantic-diff edges, severity
mapping, ignore-rule scoping, encoding, CLI exit codes, end-to-end flow and
the web dashboard.

NOTE: a handful of tests in this file intentionally assert *correct spec
behavior* and are expected to FAIL if the implementation has real bugs — they
are the evidence for the QA routing decision.  They are isolated in a clearly
marked section at the bottom.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
PY = sys.executable

sys.path.insert(0, SRC)

try:  # C extension is optional since v0.2.0 (pure-Python fallback exists)
    from cfgdrift._cfgdrift import parse_ini, parse_json, parse_toml  # noqa: E402
    _HAVE_C = True
except ImportError:  # pragma: no cover - pure-Python installs
    _HAVE_C = False

requires_c = pytest.mark.skipif(
    not _HAVE_C, reason="C extension not available (cfgdrift._cfgdrift)"
)

from cfgdrift.core.differ import SemanticDiffer  # noqa: E402
from cfgdrift.core.model import (  # noqa: E402
    ChangeType,
    DriftItem,
    IgnoreRule,
    Severity,
)
from cfgdrift.core.parser import parse_file, parse_text  # noqa: E402
from cfgdrift.rules.ignore import make_rule  # noqa: E402
from cfgdrift.storage.store import Store  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def run_cli(args, env=None, cwd=None):
    full_env = os.environ.copy()
    full_env["PYTHONPATH"] = SRC + os.pathsep + full_env.get("PYTHONPATH", "")
    if env:
        full_env.update(env)
    proc = subprocess.run(
        [PY, "-m", "cfgdrift.cli"] + args,
        capture_output=True,
        text=True,
        env=full_env,
        cwd=cwd or ROOT,
        timeout=90,
    )
    return proc


@pytest.fixture()
def cfg_home(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return str(home)


@pytest.fixture()
def project(tmp_path):
    conf = tmp_path / "conf"
    conf.mkdir()
    (conf / "app.json").write_text(
        json.dumps({"server": {"host": "localhost", "port": 8080}, "debug": False}),
        encoding="utf-8",
    )
    (conf / "app.yaml").write_text("mode: prod\n", encoding="utf-8")
    return str(conf)


# ===========================================================================
# 1. C parser boundaries
# ===========================================================================

class TestJsonBoundaries:
    """C-only: these probe the C parser's exact error wording / robustness."""

    pytestmark = requires_c

    def test_object_trailing_comma_rejected(self):
        with pytest.raises(ValueError, match="trailing comma"):
            parse_json('{"a": 1,}')

    def test_array_trailing_comma_rejected(self):
        with pytest.raises(ValueError, match="trailing comma"):
            parse_json("[1, 2,]")

    def test_valid_surrogate_pair(self):
        data = parse_json(r'{"s": "\ud83d\ude00"}')
        assert data["s"] == "\U0001F600"

    def test_unpaired_high_surrogate_rejected(self):
        with pytest.raises(ValueError, match="surrogate"):
            parse_json(r'{"s": "\ud83d"}')

    def test_unpaired_low_surrogate_rejected(self):
        with pytest.raises(ValueError, match="surrogate"):
            parse_json(r'{"s": "\ude00"}')

    def test_invalid_unicode_escape_rejected(self):
        with pytest.raises(ValueError, match=r"\\u escape"):
            parse_json(r'{"s": "\uZZZZ"}')

    def test_deep_nesting_no_crash(self):
        # 200 nested arrays must parse without crashing, preserving depth.
        def depth(v):
            if isinstance(v, list):
                return 1 + max((depth(x) for x in v), default=0)
            return 0

        s = "[" * 200 + "]" * 200
        result = parse_json(s)
        assert isinstance(result, list)
        assert depth(result) == 200

    def test_big_integer_preserved(self):
        data = parse_json('{"n": ' + "9" * 200 + "}")
        assert len(str(data["n"])) == 200

    def test_leading_zero_rejected(self):
        with pytest.raises(ValueError):
            parse_json('{"n": 01}')

    def test_bare_minus_rejected(self):
        with pytest.raises(ValueError, match="invalid number"):
            parse_json('{"n": -}')

    def test_last_wins_object(self):
        assert parse_json('{"a": 1, "a": 2}') == {"a": 2}


class TestTomlBoundaries:
    """C-only: these probe the C parser's exact error wording / robustness."""

    pytestmark = requires_c

    def test_multiline_basic_string_without_leading_newline(self):
        data = parse_toml('a = """line1\nline2"""\n')
        assert data["a"] == "line1\nline2"

    def test_multiline_string_line_continuation(self):
        data = parse_toml('a = """one\\\n   two"""\n')
        assert data["a"] == "onetwo"

    def test_inline_table_with_array(self):
        data = parse_toml("a = { x = 1, y = [1, 2] }\n")
        assert data == {"a": {"x": 1, "y": [1, 2]}}

    def test_array_of_tables_multiple_keys(self):
        text = (
            "[[products]]\n"
            'name = "Hammer"\n'
            "sku = 1\n"
            "[[products]]\n"
            'name = "Nail"\n'
            "sku = 2\n"
        )
        data = parse_toml(text)
        assert data["products"] == [
            {"name": "Hammer", "sku": 1},
            {"name": "Nail", "sku": 2},
        ]

    def test_hex_oct_bin_ints(self):
        data = parse_toml("a = 0x1F\nb = 0o17\nc = 0b101\n")
        assert data == {"a": 31, "b": 15, "c": 5}

    def test_underscore_separators(self):
        data = parse_toml("a = 1_000_000\n")
        assert data["a"] == 1000000

    def test_negative_and_float(self):
        data = parse_toml("a = -3.5e2\nb = +17\n")
        assert data["a"] == -350.0
        assert data["b"] == 17

    def test_inf_nan(self):
        data = parse_toml("a = inf\nb = -inf\nc = nan\n")
        assert data["a"] == float("inf")
        assert data["b"] == float("-inf")
        import math

        assert math.isnan(data["c"])

    def test_datetime_normalized_to_iso_string(self):
        data = parse_toml("a = 1979-05-27T07:32:00Z\n")
        assert isinstance(data["a"], str)
        assert data["a"] == "1979-05-27T07:32:00Z"

    def test_duplicate_table_header_rejected(self):
        with pytest.raises(ValueError, match="duplicate table"):
            parse_toml("[a]\nx=1\n[a]\ny=2\n")

    def test_duplicate_key_rejected(self):
        with pytest.raises(ValueError, match="duplicate key"):
            parse_toml("a = 1\na = 2\n")

    def test_table_then_array_of_tables_conflict_rejected(self):
        with pytest.raises(ValueError):
            parse_toml("[a]\nx=1\n[[a]]\ny=2\n")


class TestIniBoundaries:
    """C-only: these probe the C parser's exact error wording / robustness."""

    pytestmark = requires_c

    def test_no_section_goes_to_top_level(self):
        data = parse_ini("a = 1\nb = 2\n")
        assert data == {"a": "1", "b": "2"}

    def test_colon_separator(self):
        data = parse_ini("a: 1\n")
        assert data["a"] == "1"

    def test_duplicate_key_last_wins(self):
        data = parse_ini("a = 1\na = 2\n")
        assert data["a"] == "2"

    def test_quoted_value_stripped(self):
        data = parse_ini('a = "hello"\nb = \'world\'\n')
        assert data["a"] == "hello"
        assert data["b"] == "world"

    def test_value_with_equals_sign(self):
        data = parse_ini("url = http://x?a=b\n")
        assert data["url"] == "http://x?a=b"

    def test_comment_lines_skipped(self):
        data = parse_ini("# c1\n; c2\na = 1\n")
        assert data == {"a": "1"}

    def test_key_case_preserved(self):
        data = parse_ini("Key = 1\n")
        assert "Key" in data and "key" not in data

    def test_unterminated_section_rejected(self):
        with pytest.raises(ValueError, match="unterminated section"):
            parse_ini("[sec\na = 1\n")

    def test_duplicate_section_merges(self):
        data = parse_ini("[s]\na = 1\n[s]\nb = 2\n")
        assert data["s"] == {"a": "1", "b": "2"}


# ===========================================================================
# 2. Semantic diff boundaries
# ===========================================================================

def _diff(old, new, **kw):
    return SemanticDiffer().diff(old, new, file=kw.pop("file", "f.json"), **kw)


class TestDiffBoundaries:
    def test_empty_vs_empty_no_drift(self):
        items, summary = _diff({}, {})
        assert items == []
        assert summary.total == 0
        assert summary.max_severity == Severity.NONE

    def test_empty_vs_populated(self):
        items, _ = _diff({}, {"a": 1})
        assert len(items) == 1
        assert items[0].change_type == ChangeType.ADDED
        assert items[0].severity == Severity.INFO

    def test_top_level_list_wrap(self):
        old = {"$": [1, 2]}
        new = {"$": [1, 3]}
        items, _ = _diff(old, new)
        assert any(it.key_path == "$[1]" for it in items)

    def test_nested_empty_dict(self):
        items, _ = _diff({"a": {}}, {"a": {"b": 1}})
        assert any(it.key_path == "a.b" for it in items)
        assert items[0].change_type == ChangeType.ADDED

    def test_null_vs_missing_is_removed(self):
        items, _ = _diff({"a": None}, {})
        assert len(items) == 1
        assert items[0].change_type == ChangeType.REMOVED
        assert items[0].severity == Severity.CRITICAL

    def test_null_vs_value_is_type_change(self):
        items, _ = _diff({"a": None}, {"a": 1})
        assert len(items) == 1
        assert items[0].change_type == ChangeType.TYPE_CHANGED
        assert items[0].old_type == "null"
        assert items[0].new_type == "int"

    def test_list_growth(self):
        items, _ = _diff({"a": [1]}, {"a": [1, 2, 3]})
        assert any(it.key_path == "a[1]" and it.change_type == ChangeType.ADDED for it in items)
        assert any(it.key_path == "a[2]" and it.change_type == ChangeType.ADDED for it in items)

    def test_list_shrink(self):
        items, _ = _diff({"a": [1, 2, 3]}, {"a": [1]})
        assert any(it.key_path == "a[1]" and it.change_type == ChangeType.REMOVED for it in items)
        assert any(it.key_path == "a[2]" and it.change_type == ChangeType.REMOVED for it in items)

    def test_array_element_type_change(self):
        items, _ = _diff({"a": [1]}, {"a": ["1"]})
        assert len(items) == 1
        assert items[0].change_type == ChangeType.TYPE_CHANGED
        assert items[0].severity == Severity.CRITICAL

    def test_bool_vs_int_is_type_change(self):
        items, _ = _diff({"a": True}, {"a": 1})
        assert len(items) == 1
        assert items[0].change_type == ChangeType.TYPE_CHANGED

    def test_file_level_added_info_removed_critical(self):
        items, summary = SemanticDiffer().diff_snapshot(
            {"a.json": {"x": 1}}, {"b.json": {"y": 2}}
        )
        assert summary.added == 1 and summary.removed == 1
        by_file = {it.file: it for it in items}
        assert by_file["a.json"].severity == Severity.CRITICAL
        assert by_file["a.json"].key_path == ""
        assert by_file["b.json"].severity == Severity.INFO


class TestSeverityMapping:
    def test_all_change_types(self):
        cases = [
            (ChangeType.REMOVED, Severity.CRITICAL),
            (ChangeType.TYPE_CHANGED, Severity.CRITICAL),
            (ChangeType.MODIFIED, Severity.WARN),
            (ChangeType.ADDED, Severity.INFO),
        ]
        from cfgdrift.core.differ import SeverityEngine

        for ct, expected in cases:
            assert SeverityEngine.classify(ct) == expected, ct


# ===========================================================================
# 3. Ignore rules: match types, filters, scope
# ===========================================================================

def _item(key_path, change_type="modified", file="f.json"):
    return DriftItem(
        key_path=key_path,
        change_type=ChangeType(change_type),
        severity=Severity.WARN,
        file=file,
    )


class TestIgnoreRulesExtra:
    def test_path_exact_does_not_match_prefix(self):
        rule = make_rule("r", "server.port", "path_exact")
        assert not rule.matches(_item("server.port2"))
        assert not rule.matches(_item("server"))

    def test_path_prefix_matches_deep(self):
        rule = make_rule("r", "server", "path_prefix")
        assert rule.matches(_item("server.tls.enabled"))
        assert rule.matches(_item("server"))

    def test_regex_key(self):
        rule = make_rule("r", r"^servers\[\d+\]\.host$", "regex")
        assert rule.matches(_item("servers[0].host"))
        assert not rule.matches(_item("servers[0].host.extra"))

    def test_invalid_regex_never_matches(self):
        rule = make_rule("r", "([unclosed", "regex")
        assert not rule.matches(_item("anything"))

    def test_file_pattern_scope(self):
        rule = make_rule(
            "r", "debug", "path_prefix", file_pattern=r"\.yaml$"
        )
        assert rule.matches(_item("debug.x", file="a.yaml"))
        assert not rule.matches(_item("debug.x", file="a.json"))

    def test_change_type_filter(self):
        rule = make_rule("r", "debug", "path_prefix", change_type="added")
        assert rule.matches(_item("debug.x", change_type="added"))
        assert not rule.matches(_item("debug.x", change_type="modified"))

    def test_disabled_rule_never_matches(self):
        rule = make_rule("r", "debug", "path_prefix", enabled=False)
        assert not rule.matches(_item("debug.x"))

    def test_store_global_vs_baseline_scope(self, tmp_path):
        store = Store(str(tmp_path / "t.db"))
        global_rule = make_rule("g", "noise", "path_prefix")
        gid = store.add_rule(global_rule)
        bl = store.create_baseline(
            name="prod", description="", scan_root=".", format="json", data={"a.json": {}}
        )
        scoped = make_rule("s", "secret", "path_prefix", baseline_id=bl.id)
        store.add_rule(scoped)
        # global scope sees only global rules
        assert [r.name for r in store.list_rules(None)] == ["g"]
        # baseline scope sees global + scoped
        names = [r.name for r in store.list_rules(bl.id)]
        assert "g" in names and "s" in names
        store.delete_rule(gid)
        assert store.list_rules(None) == []
        store.close()

    def test_ignored_items_excluded_and_counted(self):
        old = {"a": 1, "b": 2}
        new = {"a": 2, "b": 3}
        rules = [make_rule("r", "a", "path_exact")]
        items, summary = _diff(old, new, rules=rules)
        assert [it.key_path for it in items] == ["b"]
        assert summary.ignored == 1
        assert summary.modified == 1


# ===========================================================================
# 4. Encoding
# ===========================================================================

class TestEncodingExtra:
    def test_utf8_chinese_json(self, tmp_path):
        f = tmp_path / "c.json"
        f.write_bytes('{"name": "配置系统"}'.encode("utf-8"))
        assert parse_file(str(f), "json", warn=False)["name"] == "配置系统"

    def test_gbk_chinese_json(self, tmp_path):
        f = tmp_path / "c.json"
        f.write_bytes('{"name": "配置系统"}'.encode("gbk"))
        assert parse_file(str(f), "json", warn=False)["name"] == "配置系统"

    def test_gbk_chinese_toml(self, tmp_path):
        f = tmp_path / "c.toml"
        f.write_bytes('title = "配置项"\n'.encode("gbk"))
        assert parse_file(str(f), "toml", warn=False)["title"] == "配置项"

    def test_yaml_chinese(self, tmp_path):
        f = tmp_path / "c.yaml"
        f.write_bytes("title: 中文标题\n".encode("utf-8"))
        assert parse_file(str(f), "yaml", warn=False)["title"] == "中文标题"

    def test_utf8_fallback_warns(self, tmp_path, capsys):
        f = tmp_path / "w.json"
        f.write_bytes(b"\xff\xfe\x00 invalid")
        # bytes that fail both UTF-8 and GBK -> utf-8-replace fallback + warning;
        # the decoded text contains a NUL control char so JSON parsing fails.
        with pytest.raises(ValueError):
            parse_file(str(f), "json", warn=True)
        captured = capsys.readouterr()
        assert "warning" in captured.err


# ===========================================================================
# 5. CLI exit codes
# ===========================================================================

class TestCliExitCodes:
    def test_diff_without_baseline_flag_exit_2(self, cfg_home, project):
        proc = run_cli(["diff", project], env={"CFGDRIFT_HOME": cfg_home})
        assert proc.returncode == 2

    def test_invalid_format_exit_2(self, cfg_home, project):
        proc = run_cli(
            ["scan", project, "--format", "bogus"],
            env={"CFGDRIFT_HOME": cfg_home},
        )
        assert proc.returncode == 2

    def test_scan_unknown_single_file_exit_2(self, cfg_home, tmp_path):
        f = tmp_path / "app.unknown"
        f.write_text("x", encoding="utf-8")
        proc = run_cli(["scan", str(f)], env={"CFGDRIFT_HOME": cfg_home})
        assert proc.returncode == 2

    def test_ignore_remove_missing_exit_2(self, cfg_home):
        proc = run_cli(["ignore", "remove", "999"], env={"CFGDRIFT_HOME": cfg_home})
        assert proc.returncode == 2

    def test_baseline_create_missing_scan_root_exit_2(self, cfg_home):
        proc = run_cli(
            ["baseline", "create", "x"], env={"CFGDRIFT_HOME": cfg_home}
        )
        assert proc.returncode == 2

    def test_diff_nonexistent_baseline_exit_2(self, cfg_home, project):
        proc = run_cli(
            ["diff", project, "--baseline", "missing"],
            env={"CFGDRIFT_HOME": cfg_home},
        )
        assert proc.returncode == 2


# ===========================================================================
# 6. End-to-end flow (init -> baseline -> drift -> rollback -> ignore)
# ===========================================================================

class TestEndToEnd:
    def test_full_workflow_and_json_report(self, cfg_home, project):
        env = {"CFGDRIFT_HOME": cfg_home}
        assert run_cli(["init"], env=env).returncode == 0

        # save baseline v1
        p = run_cli(["scan", project, "--save-as-baseline", "prod"], env=env)
        assert p.returncode == 0

        # modify -> drift, exit 1
        app = os.path.join(project, "app.json")
        with open(app, "w", encoding="utf-8") as fh:
            json.dump({"server": {"host": "changed", "port": 9090}, "debug": True}, fh)
        p = run_cli(["diff", project, "--baseline", "prod"], env=env)
        assert p.returncode == 1
        assert "server.host" in p.stdout or "server.port" in p.stdout

        # revert -> no drift, exit 0
        with open(app, "w", encoding="utf-8") as fh:
            json.dump({"server": {"host": "localhost", "port": 8080}, "debug": False}, fh)
        p = run_cli(["diff", project, "--baseline", "prod"], env=env)
        assert p.returncode == 0

        # verify stored report JSON structure (7.6)
        out = os.path.join(project, "report.json")
        assert run_cli(["report", "--json", out], env=env).returncode == 0
        with open(out, encoding="utf-8") as fh:
            payload = json.load(fh)
        assert set(payload) == {"code", "data", "message"}
        assert payload["code"] == 0
        assert payload["message"] == "ok"
        data = payload["data"]
        assert set(data) >= {"scan_id", "mode", "created_at", "baseline", "summary", "items"}
        assert data["baseline"]["name"] == "prod"
        assert set(data["summary"]) >= {
            "added", "removed", "modified", "type_changed", "ignored", "total", "max_severity",
        }
        assert isinstance(data["items"], list)

    def test_rollback_restores_previous_version(self, cfg_home, project):
        env = {"CFGDRIFT_HOME": cfg_home}
        run_cli(["init"], env=env)
        # v1 = state A
        run_cli(["scan", project, "--save-as-baseline", "prod"], env=env)
        # v2 = state B
        app = os.path.join(project, "app.json")
        with open(app, "w", encoding="utf-8") as fh:
            json.dump({"server": {"host": "changed", "port": 9090}, "debug": True}, fh)
        run_cli(["scan", project, "--save-as-baseline", "prod"], env=env)

        # current = v2 (state B); state A file -> drift
        with open(app, "w", encoding="utf-8") as fh:
            json.dump({"server": {"host": "localhost", "port": 8080}, "debug": False}, fh)
        assert run_cli(["diff", project, "--baseline", "prod"], env=env).returncode == 1

        # rollback -> current = v1 (state A); now no drift
        assert run_cli(["baseline", "rollback", "prod"], env=env).returncode == 0
        assert run_cli(["diff", project, "--baseline", "prod"], env=env).returncode == 0

        # only one version left -> rollback errors with exit 2
        assert run_cli(["baseline", "rollback", "prod"], env=env).returncode == 2

    def test_ignore_rule_via_cli_filters_drift(self, cfg_home, project):
        env = {"CFGDRIFT_HOME": cfg_home}
        run_cli(["init"], env=env)
        run_cli(["scan", project, "--save-as-baseline", "prod"], env=env)
        run_cli(
            ["ignore", "add", "hostrule", "server.host", "--match-type", "path_exact"],
            env=env,
        )
        app = os.path.join(project, "app.json")
        with open(app, "w", encoding="utf-8") as fh:
            json.dump({"server": {"host": "changed", "port": 8080}, "debug": False}, fh)
        p = run_cli(["diff", project, "--baseline", "prod"], env=env)
        # host is the ONLY change and it is ignored -> no drift -> exit 0
        assert p.returncode == 0, p.stdout + p.stderr
        assert "server.host" not in p.stdout

    def test_scan_without_baseline_records_history(self, cfg_home, project):
        env = {"CFGDRIFT_HOME": cfg_home}
        run_cli(["init"], env=env)
        p = run_cli(["scan", project], env=env)
        assert p.returncode == 0
        assert "recorded scan" in p.stdout
        # latest scan via report without --scan-id
        out = os.path.join(project, "r.json")
        assert run_cli(["report", "--json", out], env=env).returncode == 0
        with open(out, encoding="utf-8") as fh:
            payload = json.load(fh)
        assert payload["data"]["scan_id"] == 1


# ===========================================================================
# 7. Web dashboard smoke (uvicorn subprocess)
# ===========================================================================

class TestWebSmoke:
    @pytest.mark.skipif(
        __import__("importlib.util", fromlist=["find_spec"]).find_spec("uvicorn") is None,
        reason="uvicorn not installed",
    )
    def test_serve_endpoints(self, cfg_home):
        env = {"CFGDRIFT_HOME": cfg_home}
        run_cli(["init"], env=env)
        port = 8143
        proc = subprocess.Popen(
            [PY, "-m", "cfgdrift.cli", "serve", "--port", str(port)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env={**os.environ, "PYTHONPATH": SRC, "CFGDRIFT_HOME": cfg_home},
        )
        try:
            deadline = time.time() + 20
            while time.time() < deadline:
                try:
                    with urllib.request.urlopen(
                        "http://127.0.0.1:%d/api/health" % port, timeout=2
                    ) as resp:
                        assert resp.status == 200
                        assert json.loads(resp.read().decode("utf-8"))["code"] == 0
                    break
                except Exception:
                    time.sleep(0.5)
            else:
                pytest.fail("server did not respond")

            with urllib.request.urlopen("http://127.0.0.1:%d/" % port, timeout=5) as resp:
                assert resp.status == 200
                body = resp.read().decode("utf-8")
                assert "cfgdrift" in body.lower() or "<!doctype" in body.lower()

            with urllib.request.urlopen(
                "http://127.0.0.1:%d/api/overview" % port, timeout=5
            ) as resp:
                assert resp.status == 200
                payload = json.loads(resp.read().decode("utf-8"))
                assert payload["code"] == 0
                assert "latest_scan" in payload["data"]
                assert "timeline" in payload["data"]
        finally:
            proc.terminate()
            proc.communicate(timeout=10)


# ===========================================================================
# 8. Adversarial probes — evidence of REAL bugs found by QA (expected to FAIL
#    against the current implementation; they assert *correct spec behavior*).
#    Kept in subprocess isolation so a crash cannot kill the whole suite.
# ===========================================================================

class TestAdversarialEvidence:
    """These document confirmed C source-code defects.  A failure here is the
    evidence routed to the Engineer, NOT a test bug.  C-only since they probe
    the C extension directly."""

    pytestmark = requires_c

    def test_toml_long_integer_does_not_crash(self):
        # TOML v1.0 integers are arbitrary precision; a 200-digit decimal
        # literal must parse (or at worst raise a clean ValueError) — it must
        # NOT crash the process with a stack buffer overrun.
        code = (
            "from cfgdrift import _cfgdrift\n"
            "big = '9' * 200\n"
            "r = _cfgdrift.parse_toml('a = ' + big + '\\n')\n"
            "print('OK', len(str(r['a'])))\n"
        )
        p = subprocess.run(
            [PY, "-c", code], capture_output=True, text=True, timeout=30,
            env={**os.environ, "PYTHONPATH": SRC},
        )
        assert p.returncode == 0, (
            "TOML parser crashed on a long integer literal "
            "(returncode=%r, stderr=%r)" % (p.returncode, p.stderr[:300])
        )
        assert "OK 200" in p.stdout

    def test_toml_79_digit_integer_preserved(self):
        # 20-79 digit integers are silently truncated by strtoll -> data loss.
        code = (
            "from cfgdrift import _cfgdrift\n"
            "r = _cfgdrift.parse_toml('a = ' + '123456789012345678901234567890' + '\\n')\n"
            "print('VALUE', r['a'])\n"
        )
        p = subprocess.run(
            [PY, "-c", code], capture_output=True, text=True, timeout=30,
            env={**os.environ, "PYTHONPATH": SRC},
        )
        assert p.returncode == 0
        # the 30-digit value 123456789012345678901234567890 must survive intact
        assert "123456789012345678901234567890" in p.stdout, (
            "TOML integer silently truncated (precision loss)"
        )

    def test_toml_no_refcount_leak(self):
        import sys as _sys

        d = parse_toml("a = 1\n")
        # local var + getrefcount temp = 2; an extra leaked ref => 3
        assert _sys.getrefcount(d) == 2, (
            "parse_toml leaks one reference on the root dict (getrefcount=%d)"
            % _sys.getrefcount(d)
        )

    def test_toml_quoted_dot_key_and_dotted_key_are_distinct(self):
        # Per TOML v1.0, "a.b" (a single key containing a dot) and a.b (nested
        # keys) are distinct and may coexist.  The parser falsely reports a
        # duplicate key because both collapse to the same string path.
        data = parse_toml('"a.b" = 1\na.b = 2\n')
        assert data == {"a.b": 1, "a": {"b": 2}}

    def test_toml_signed_nan_supported(self):
        # TOML v1.0 allows +nan / -nan as float literals.
        import math

        data = parse_toml("a = +nan\n")
        assert math.isnan(data["a"])
