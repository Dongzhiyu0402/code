"""Engineer unit tests for cfgdrift v0.4.0 (five feature additions).

Author: 寇豆码 (Engineer).  Covers the v0.4.0 design (system_design.md):

1. Sensitive masking: default keywords (bare ``key`` NOT sensitive),
   case-insensitive per-segment matching, glob patterns, type preservation,
   payload masking, masking.yaml fallback.
2. Line maps: JSON/TOML/INI/YAML key-path -> line extraction.
3. Custom severity: SeverityRule matching, severity.yaml CRUD, differ
   first-match-wins override, summary.max_severity updated.
4. Multi-environment compare: CompareEngine + environments.yaml + exit codes.
5. alert_events storage + daemon status_dict.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cfgdrift.core.compare import CompareEngine  # noqa: E402
from cfgdrift.core.differ import SemanticDiffer  # noqa: E402
from cfgdrift.core.lines import build_line_map  # noqa: E402
from cfgdrift.core.masker import (  # noqa: E402
    DEFAULT_MASK,
    SensitiveMasker,
)
from cfgdrift.core.model import (  # noqa: E402
    ChangeType,
    DriftItem,
    Report,
    ScanSummary,
    Severity,
    SeverityRule,
)
from cfgdrift.daemon.daemon import DaemonManager  # noqa: E402
from cfgdrift.rules.severity import SeverityConfig, make_rule as make_sev_rule  # noqa: E402
from cfgdrift.storage.store import Store  # noqa: E402


def _mkstore(tmp_path) -> Store:
    return Store(str(tmp_path / "cfgdrift.db"))


def _item(key_path="a.b", change=ChangeType.MODIFIED, old="x", new="y",
          file="app.yaml", severity=Severity.WARN) -> DriftItem:
    return DriftItem(
        key_path=key_path,
        change_type=change,
        severity=severity,
        file=file,
        old_value=old,
        new_value=new,
        old_type="str",
        new_type="str",
    )


# ---------------------------------------------------------------------------
# 1. Sensitive masking
# ---------------------------------------------------------------------------

class TestMasker:
    def test_bare_key_not_sensitive(self):
        m = SensitiveMasker()
        # "key" is deliberately NOT in the default keyword list.
        assert not m.is_sensitive_key("key")
        assert not m.is_sensitive_key("keyboard")
        assert not m.is_sensitive_key("monkey")

    def test_default_keywords_hit(self):
        m = SensitiveMasker()
        for path in (
            "server.password",
            "db.passwd",
            "client_secret",
            "auth.token",
            "api_key",
            "credentials[0].value",
            "servers[0].access_token",
        ):
            assert m.is_sensitive_key(path), path

    def test_case_insensitive(self):
        m = SensitiveMasker()
        assert m.is_sensitive_key("Server.Password")
        assert m.is_sensitive_key("DB.PASSWD")
        assert m.is_sensitive_key("SeRvEr.ToKeN")

    def test_glob_pattern(self):
        m = SensitiveMasker(patterns=["*.secret", "*password*"])
        assert m.is_sensitive_key("tls.secret")
        assert m.is_sensitive_key("my.password.value")
        assert not m.is_sensitive_key("tls.enabled")

    def test_custom_keywords_replace_defaults(self):
        m = SensitiveMasker(keywords=["mypin"])
        assert m.is_sensitive_key("a.mypin")
        assert not m.is_sensitive_key("pin.value")  # 'mypin' not in 'pin'
        # custom keywords replace defaults, so "password" no longer matches.
        assert not m.is_sensitive_key("server.password")

    def test_mask_item_preserves_types(self):
        m = SensitiveMasker()
        item = _item(key_path="server.password", old="hunter2", new="hunter3")
        old_type_before = item.old_type
        m.mask_item(item)
        assert item.masked is True
        assert item.old_value == DEFAULT_MASK
        assert item.new_value == DEFAULT_MASK
        assert item.old_type == old_type_before  # type preserved
        assert item.change_type == ChangeType.MODIFIED

    def test_mask_item_non_sensitive_untouched(self):
        m = SensitiveMasker()
        item = _item(key_path="server.port", old=8080, new=9090)
        m.mask_item(item)
        assert item.masked is False
        assert item.old_value == 8080

    def test_mask_payload_report_envelope(self):
        m = SensitiveMasker()
        payload = {
            "code": 0,
            "data": {
                "items": [
                    {"key_path": "db.password", "old_value": "a", "new_value": "b"},
                    {"key_path": "server.port", "old_value": 1, "new_value": 2},
                ]
            },
        }
        m.mask_payload(payload)
        items = payload["data"]["items"]
        assert items[0]["old_value"] == DEFAULT_MASK
        assert items[0]["masked"] is True
        assert items[1]["old_value"] == 1
        assert "masked" not in items[1] or items[1]["masked"] is False

    def test_mask_payload_drift_items(self):
        m = SensitiveMasker()
        payload = {
            "drift_items": [
                {"key": "token", "baseline": "a", "current": "b"},
            ]
        }
        m.mask_payload(payload)
        assert payload["drift_items"][0]["baseline"] == DEFAULT_MASK
        assert payload["drift_items"][0]["masked"] is True

    def test_from_config_missing_falls_back(self, tmp_path):
        m = SensitiveMasker.from_config(str(tmp_path / "nope.yaml"))
        assert m.is_sensitive_key("password")

    def test_from_config_reads_file(self, tmp_path):
        path = tmp_path / "masking.yaml"
        path.write_text(
            "version: 1\nmask: '[[REDACTED]]'\nkeywords:\n  - mypin\n"
            "patterns:\n  - '*.cred'\n",
            encoding="utf-8",
        )
        m = SensitiveMasker.from_config(str(path))
        assert m.mask == "[[REDACTED]]"
        assert m.is_sensitive_key("a.mypin")
        assert m.is_sensitive_key("x.cred")
        assert not m.is_sensitive_key("server.password")

    def test_from_config_corrupt_falls_back(self, tmp_path):
        path = tmp_path / "masking.yaml"
        path.write_text(": : : not yaml [", encoding="utf-8")
        m = SensitiveMasker.from_config(str(path))
        assert m.is_sensitive_key("server.password")


# ---------------------------------------------------------------------------
# 2. Line maps
# ---------------------------------------------------------------------------

class TestLineMaps:
    def test_json_basic(self):
        text = '{\n  "server": {\n    "host": "x",\n    "port": 8080\n  },\n  "arr": [1, 2]\n}'
        lm = build_line_map(text, "json")
        assert lm["server"] == 2
        assert lm["server.host"] == 3
        assert lm["server.port"] == 4
        assert lm["arr"] == 6
        assert lm["arr[0]"] == 6
        assert lm["arr[1]"] == 6

    def test_json_duplicate_key_last_wins(self):
        text = '{\n  "a": 1,\n  "a": 2\n}'
        lm = build_line_map(text, "json")
        assert lm["a"] == 3

    def test_json_multiline_value(self):
        text = '{\n  "a": {\n    "b": 1\n  }\n}'
        lm = build_line_map(text, "json")
        assert lm["a"] == 2  # value ({) starts on line 2
        assert lm["a.b"] == 3

    def test_json_escaped_string(self):
        # The decoded key name (single backslash / embedded quote) must match
        # the differ's join_path convention (backslash escaped in the path).
        text = '{\n  "with\\\\backslash": 1,\n  "with\\"quote": 2\n}'
        lm = build_line_map(text, "json")
        # Key decodes to one backslash; the key path escapes it to two.
        assert "with\\\\backslash" in lm
        assert 'with"quote' in lm

    def test_ini_basic(self):
        text = "[server]\nhost = x\nport=8080\n\n[db]\nuser = root\n"
        lm = build_line_map(text, "ini")
        assert lm["server.host"] == 2
        assert lm["server.port"] == 3
        assert lm["db.user"] == 6

    def test_ini_no_section_top_level(self):
        text = "key = 1\n[sec]\nkey2 = 2\n"
        lm = build_line_map(text, "ini")
        assert lm["key"] == 1
        assert lm["sec.key2"] == 3

    def test_yaml_basic(self):
        text = "server:\n  host: x\n  port: 8080\narr:\n  - a\n  - b\n"
        lm = build_line_map(text, "yaml")
        assert lm["server"] == 2  # value (nested map) starts on line 2
        assert lm["server.host"] == 2
        assert lm["server.port"] == 3
        assert lm["arr"] == 5  # value (sequence) starts on line 5
        assert lm["arr[0]"] == 5
        assert lm["arr[1]"] == 6

    def test_yaml_block_scalar_keeps_key_line(self):
        text = "message: |\n  line1\n  line2\nnext: 1\n"
        lm = build_line_map(text, "yaml")
        assert lm["message"] == 1
        assert lm["next"] == 4

    def test_yaml_nested_list(self):
        text = "servers:\n  - host: a\n    port: 1\n  - host: b\n    port: 2\n"
        lm = build_line_map(text, "yaml")
        assert lm["servers"] == 2  # value (sequence) starts on line 2
        assert lm["servers[0].host"] == 2
        assert lm["servers[0].port"] == 3
        assert lm["servers[1].host"] == 4
        assert lm["servers[1].port"] == 5

    def test_toml_basic(self):
        text = (
            "[server]\nhost = \"x\"\nport = 8080\n\n"
            "[[apps]]\nname = \"a\"\n\n[[apps]]\nname = \"b\"\n"
        )
        lm = build_line_map(text, "toml")
        assert lm["server.host"] == 2
        assert lm["server.port"] == 3
        assert lm["apps[0].name"] == 6
        assert lm["apps[1].name"] == 9

    def test_toml_inline_table(self):
        text = "server = { host = \"x\", port = 8080 }\n"
        lm = build_line_map(text, "toml")
        assert lm["server.host"] == 1
        assert lm["server.port"] == 1

    def test_toml_multiline_array(self):
        text = "ports = [\n  80,\n  443,\n]\n"
        lm = build_line_map(text, "toml")
        assert lm["ports"] == 1
        assert lm["ports[0]"] == 2
        assert lm["ports[1]"] == 3

    def test_toml_dotted_key(self):
        text = "a.b.c = 1\n"
        lm = build_line_map(text, "toml")
        assert lm["a.b.c"] == 1

    def test_invalid_fmt_returns_empty(self):
        assert build_line_map("x", "unknown") == {}


# ---------------------------------------------------------------------------
# 3. Custom severity rules
# ---------------------------------------------------------------------------

class TestSeverityRules:
    def test_rule_matches_key_pattern(self):
        rule = SeverityRule(name="tls", severity=Severity.CRITICAL,
                            key_pattern=r".*tls\.enabled")
        item = _item(key_path="server.tls.enabled")
        assert rule.matches(item)
        assert not rule.matches(_item(key_path="server.tls.ciphers"))

    def test_rule_change_type_filter(self):
        rule = SeverityRule(name="r", severity=Severity.CRITICAL,
                            change_type="modified")
        assert rule.matches(_item(change=ChangeType.MODIFIED))
        assert not rule.matches(_item(change=ChangeType.ADDED))

    def test_rule_value_pattern(self):
        rule = SeverityRule(name="r", severity=Severity.CRITICAL,
                            value_pattern=r"supersecret")
        item = _item(old="supersecret", new="other")
        assert rule.matches(item)
        item2 = _item(old="plain", new="value")
        assert not rule.matches(item2)

    def test_rule_disabled(self):
        rule = SeverityRule(name="r", severity=Severity.CRITICAL,
                            key_pattern=r".*", enabled=False)
        assert not rule.matches(_item(key_path="anything"))

    def test_config_crud(self, tmp_path):
        path = str(tmp_path / "severity.yaml")
        assert SeverityConfig.list_rules(path) == []
        SeverityConfig.add_rule(path, make_sev_rule("tls", "CRITICAL",
                                                    key_pattern=r".*tls\.enabled"))
        SeverityConfig.add_rule(path, make_sev_rule("port", "WARN",
                                                    change_type="modified"))
        rules = SeverityConfig.list_rules(path)
        assert len(rules) == 2
        assert rules[0].severity == Severity.CRITICAL
        with pytest.raises(ValueError):
            SeverityConfig.add_rule(path, make_sev_rule("tls", "INFO"))
        SeverityConfig.remove_rule(path, "port")
        assert len(SeverityConfig.list_rules(path)) == 1
        SeverityConfig.set_enabled(path, "tls", False)
        assert SeverityConfig.list_rules(path)[0].enabled is False
        with pytest.raises(ValueError):
            SeverityConfig.remove_rule(path, "missing")

    def test_differ_override_first_match_wins(self):
        differ = SemanticDiffer()
        old = {"f.json": {"a": {"tls": {"enabled": True}}}}
        new = {"f.json": {"a": {"tls": {"enabled": False}}}}
        rules = [
            SeverityRule(name="first", severity=Severity.CRITICAL,
                         key_pattern=r".*tls\.enabled"),
            SeverityRule(name="second", severity=Severity.INFO,
                         key_pattern=r".*tls\.enabled"),
        ]
        items, summary = differ.diff_snapshot(old, new, severity_rules=rules)
        assert items[0].severity == Severity.CRITICAL  # first-match-wins
        assert summary.max_severity == Severity.CRITICAL
        assert summary.total == 1

    def test_differ_no_rules_uses_default(self):
        differ = SemanticDiffer()
        old = {"f.json": {"a": 1}}
        new = {"f.json": {"a": 2}}
        items, summary = differ.diff_snapshot(old, new)
        assert items[0].severity == Severity.WARN  # modified -> WARN
        assert summary.max_severity == Severity.WARN

    def test_differ_attach_lines(self):
        differ = SemanticDiffer()
        old = {"f.json": {"a": 1}}
        new = {"f.json": {"a": 2}}
        items, summary = differ.diff_snapshot(
            old, new,
            old_lines={"f.json": {"a": 10}},
            new_lines={"f.json": {"a": 12}},
        )
        assert items[0].key_path == "a"
        assert items[0].file == "f.json"
        assert items[0].line == 12  # new side preferred

    def test_differ_removed_falls_back_to_old_line(self):
        differ = SemanticDiffer()
        old = {"f.json": {"a": 1, "gone": 5}}
        new = {"f.json": {"a": 1}}
        items, summary = differ.diff_snapshot(
            old, new,
            old_lines={"f.json": {"gone": 3}},
            new_lines={"f.json": {"a": 1}},
        )
        removed = [it for it in items if it.change_type == ChangeType.REMOVED]
        assert removed[0].key_path == "gone"
        assert removed[0].line == 3  # falls back to old side


# ---------------------------------------------------------------------------
# 4. Multi-environment compare
# ---------------------------------------------------------------------------

class TestCompare:
    def _make_baselines(self, tmp_path):
        store = _mkstore(tmp_path)
        store.create_baseline(
            "prod", "", str(tmp_path), "json",
            {"app.json": {"port": 8080, "host": "prod"}},
            line_maps={"app.json": {"port": 1, "host": 2}},
        )
        store.create_baseline(
            "staging", "", str(tmp_path), "json",
            {"app.json": {"port": 9090, "host": "staging"}},
            line_maps={"app.json": {"port": 1, "host": 2}},
        )
        store.create_baseline(
            "prod-baseline", "", str(tmp_path), "json",
            {"app.json": {"port": 8080, "host": "prod"}},
        )
        return store

    def test_compare_reports(self, tmp_path):
        store = self._make_baselines(tmp_path)
        engine = CompareEngine(store)
        reports = engine.compare(["prod", "staging"])
        assert len(reports) == 1
        rep = reports[0]
        assert rep.baseline_a == "prod"
        assert rep.baseline_b == "staging"
        assert rep.summary.total == 2  # port + host modified
        # items are sorted by key_path: host first (line 2), then port (line 1).
        assert rep.items[0].key_path == "host"
        assert rep.items[0].line == 2

    def test_compare_env_map(self, tmp_path):
        store = self._make_baselines(tmp_path)
        engine = CompareEngine(store)
        env_map = engine.load_environments(str(tmp_path))
        assert env_map == {}
        # explicit mapping prod -> prod-baseline
        env_map2 = {"prod": "prod-baseline"}
        reports = engine.compare(["prod", "staging"], env_map=env_map2)
        assert reports[0].baseline_a == "prod-baseline"

    def test_compare_missing_baseline_raises(self, tmp_path):
        store = self._make_baselines(tmp_path)
        engine = CompareEngine(store)
        with pytest.raises(ValueError):
            engine.compare(["prod", "nope"])

    def test_compare_requires_two(self, tmp_path):
        store = self._make_baselines(tmp_path)
        engine = CompareEngine(store)
        with pytest.raises(ValueError):
            engine.compare(["prod"])


# ---------------------------------------------------------------------------
# 5. alert_events + status_dict
# ---------------------------------------------------------------------------

class TestAlertEvents:
    def test_add_list_count(self, tmp_path):
        store = _mkstore(tmp_path)
        eid = store.add_alert_event({
            "rule": "web", "baseline": "prod", "severity": "WARN",
            "status": "sent", "target": "/etc/app", "drift_count": 2,
            "attempts": 1, "fingerprint": "abc",
        })
        assert isinstance(eid, int)
        store.add_alert_event({
            "rule": "web", "baseline": "prod", "severity": "CRITICAL",
            "status": "failed", "error": "boom", "attempts": 3,
        })
        assert store.count_alert_events() == 2
        page = store.list_alert_events()
        assert page["total"] == 2
        assert page["events"][0]["status"] == "failed"
        assert page["events"][0]["error"] == "boom"
        # filters
        assert store.list_alert_events(status="sent")["total"] == 1
        assert store.list_alert_events(severity="CRITICAL")["total"] == 1
        assert store.list_alert_events(rule="nope")["total"] == 0
        # pagination
        page2 = store.list_alert_events(limit=1, offset=1)
        assert page2["total"] == 2
        assert len(page2["events"]) == 1

    def test_prune_by_age(self, tmp_path):
        store = _mkstore(tmp_path)
        store.add_alert_event({"rule": "r", "baseline": "b", "severity": "WARN",
                               "status": "sent"})
        # Manually backdate the created_at to 40 days ago.
        store._conn.execute(
            "UPDATE alert_events SET created_at = '2020-01-01T00:00:00+00:00' "
            "WHERE rule = 'r'"
        )
        store._conn.commit()
        removed = store.prune_alert_events(days=30, max_rows=5000)
        assert removed == 1
        assert store.count_alert_events() == 0

    def test_prune_cap_rows(self, tmp_path):
        store = _mkstore(tmp_path)
        for i in range(10):
            store.add_alert_event({"rule": "r%d" % i, "baseline": "b",
                                   "severity": "WARN", "status": "sent"})
        removed = store.prune_alert_events(days=30, max_rows=5)
        assert removed == 5
        assert store.count_alert_events() == 5

    def test_line_maps_migration(self, tmp_path):
        store = _mkstore(tmp_path)
        # init_schema already ran; verify the column exists and idempotency.
        store.init_schema()  # must not raise on second run
        cols = [r["name"] for r in store._conn.execute("PRAGMA table_info(baselines)")]
        assert "line_maps" in cols
        bl = store.create_baseline("b", "", str(tmp_path), "json",
                                   {"a": 1}, line_maps={"f": {"a": 1}})
        assert bl.line_maps == {"f": {"a": 1}}
        loaded = store.get_baseline("b")
        assert loaded.line_maps == {"f": {"a": 1}}


class TestStatusDict:
    def test_not_running(self, tmp_path):
        mgr = DaemonManager(str(tmp_path))
        st = mgr.status_dict()
        assert st["running"] is False
        assert st["pid"] is None
        assert st["error"] is None

    def test_corrupt_pid(self, tmp_path):
        mgr = DaemonManager(str(tmp_path))
        mgr._write_pid(12345)
        with open(mgr.pid_file, "w", encoding="utf-8") as fh:
            fh.write("not-a-pid")
        st = mgr.status_dict()
        assert st["running"] is False
        assert st["error"] is not None

    def test_running_with_stale_cleared(self, tmp_path):
        mgr = DaemonManager(str(tmp_path))
        # Use the current process: it certainly exists.
        mgr._write_pid(os.getpid())
        st = mgr.status_dict()
        assert st["running"] is True
        assert st["pid"] == os.getpid()
