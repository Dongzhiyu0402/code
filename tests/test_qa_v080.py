"""QA round-7 independent verification for cfgdrift v0.6.0 (direction B).

Written from scratch with a suspicious eye — deliberately does NOT reuse the
engineer's four new test files.  Covers the acceptance surface a-l:

a. Scenario A  tls.enabled false->true without cert_path -> composite alert.
b. Scenario B  server.port 8080->99999 -> JSON constraint violation.
c. Scenario C  custom constraint added to constraints.yaml takes effect on
   the next diff; resolve() merges builtin + user + extra files.
d. Zero noise  legal changes produce output identical to the no-constraint
   path; DriftItem.to_dict omits constraint_violations when empty.
e. Five types  range boundaries (min/max inclusive), enum, conditional
   required, correlation ops (>=,>,<=,<,==,!=), mutual exclusion default and
   forbid; missing-key / non-numeric skip semantics.
f. Upgrade formula  min(3, max(item.rank+1, max(constraint.rank))); one-shot.
g. Association  violations attach to every involved drift item; unrelated
   drift stays clean; pre-existing baseline violations are NOT reported (Q2).
h. CLI  constraint add / list / remove / enable / disable; --builtin off;
   --constraints extra file; missing file exit 2.
i. daemon  worker.main() must accept --no-builtin / --constraints (this is
   where the round-7 review found a real argparse bug); build_worker_command.
j. Presentation  terminal line, JSON field, HTML column, Web payload source,
   alert payload constraint field only on violated items.
k. Performance  10k-key snapshot incremental check < 10 ms.
l. Regression  v0.5.0 semantics unchanged; version contract 0.6.0 / 0.6.0-c.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
PY = sys.executable

sys.path.insert(0, SRC)

from cfgdrift.core.constraints import (  # noqa: E402
    BUILTIN_CONSTRAINTS,
    BUILTIN_CONSTRAINTS_BY_ID,
    ConstraintEngine,
)
from cfgdrift.core.differ import SemanticDiffer  # noqa: E402
from cfgdrift.core.model import (  # noqa: E402
    ChangeType,
    Constraint,
    DriftItem,
    Report,
    ScanSummary,
    Severity,
)
from cfgdrift.core.reporter import Reporter  # noqa: E402
from cfgdrift.daemon import worker as worker_mod  # noqa: E402
from cfgdrift.rules.constraints import (  # noqa: E402
    ConstraintConfig,
    resolve as resolve_constraints,
)


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def _run_cli(home, args, store=None):
    env = dict(os.environ)
    env["CFGDRIFT_HOME"] = str(home)
    cmd = [PY, "-m", "cfgdrift.cli"]
    if store:
        cmd += ["--store", str(store)]
    cmd += list(args)
    return subprocess.run(cmd, capture_output=True, text=True, env=env,
                          timeout=180)


def _mk_item(key_path="server.port", change=ChangeType.MODIFIED,
             severity=Severity.WARN, file="app.json", old=8080, new=9090):
    return DriftItem(
        key_path=key_path, change_type=change, severity=severity, file=file,
        old_value=old, new_value=new,
        old_type="int" if old is not None else None,
        new_type="int" if new is not None else None,
    )


def _diff(old, new, constraints=None, files=("app.json",)):
    return SemanticDiffer().diff_snapshot(
        {files[0]: old}, {files[0]: new}, constraints=constraints,
    )


# ---------------------------------------------------------------------------
# l. version contract (0.6.0 / 0.6.0-c / CLI --version)
# ---------------------------------------------------------------------------

class TestVersionContract:
    def test_module_version(self):
        import cfgdrift
        assert cfgdrift.__version__ == "0.8.0"

    def test_cli_version_output(self, tmp_path):
        r = _run_cli(tmp_path / "home", ["--version"])
        assert r.returncode == 0
        assert "0.8.0" in r.stdout

    def test_c_extension_version_when_available(self):
        # The C parser is compiled on some platforms; when present its
        # version() must be "0.8.0-c" (design 1.8).  On the current Windows
        # run the pure-Python backend is active, so skip gracefully but keep
        # the contract documented.
        from cfgdrift.core import parser as parser_mod
        csrc = getattr(parser_mod, "_csrc", None)
        if csrc is None:
            pytest.skip("C parser backend not active in this environment")
        assert csrc.version() == "0.8.0-c"


# ---------------------------------------------------------------------------
# a. Scenario A — composite alert, WARN -> CRITICAL, message contains 缺失
# ---------------------------------------------------------------------------

class TestScenarioA:
    BASELINE = {"tls": {"enabled": False}, "server": {"port": 8080}}
    CURRENT = {"tls": {"enabled": True}, "server": {"port": 8080}}

    def test_terminal_composite_alert(self):
        items, summary = _diff(self.BASELINE, self.CURRENT,
                               constraints=BUILTIN_CONSTRAINTS,
                               files=("nginx.conf",))
        report = Report(None, None, "2026-08-04T00:00:00+00:00", "manual",
                        summary, items)
        text = Reporter().render_terminal(report, color=False, masker=None)
        # color=False renders plain "CRITICAL tls.enabled" (no brackets).
        assert "CRITICAL tls.enabled" in text  # WARN -> CRITICAL
        assert ("constraint http_ssl_cert_required "
                "[conditional_required]") in text
        assert "tls.cert_path 缺失" in text
        assert "max=CRITICAL" in text

    def test_json_violation_shape(self):
        items, summary = _diff(self.BASELINE, self.CURRENT,
                               constraints=BUILTIN_CONSTRAINTS,
                               files=("nginx.conf",))
        item = items[0]
        assert item.key_path == "tls.enabled"
        assert item.severity == Severity.CRITICAL
        assert summary.max_severity == Severity.CRITICAL
        cvs = item.constraint_violations
        assert len(cvs) >= 1
        first = cvs[0]
        assert first["constraint_id"] == "http_ssl_cert_required"
        assert first["type"] == "conditional_required"
        assert first["involved_keys"] == ["tls.enabled", "tls.cert_path"]
        d = item.to_dict()
        assert d["constraint_violations"][0]["constraint_id"] == \
            "http_ssl_cert_required"


# ---------------------------------------------------------------------------
# b. Scenario B — server.port 8080->99999
# ---------------------------------------------------------------------------

class TestScenarioB:
    def test_json_first_violation(self):
        items, summary = _diff({"server": {"port": 8080}},
                               {"server": {"port": 99999}},
                               constraints=BUILTIN_CONSTRAINTS)
        item = items[0]
        assert item.severity == Severity.CRITICAL
        cv = item.constraint_violations[0]
        assert cv["constraint_id"] == "http_port_range"
        assert cv["type"] == "range"
        assert summary.max_severity == Severity.CRITICAL

    def test_terminal_renders_range_line(self):
        items, summary = _diff({"server": {"port": 8080}},
                               {"server": {"port": 99999}},
                               constraints=BUILTIN_CONSTRAINTS)
        report = Report(None, None, "2026-08-04T00:00:00+00:00", "manual",
                        summary, items)
        text = Reporter().render_terminal(report, color=False, masker=None)
        assert "constraint http_port_range [range]" in text
        assert "server.port 必须在 [1, 65535] 范围内" in text


# ---------------------------------------------------------------------------
# c. Scenario C — custom constraint takes effect on next diff
# ---------------------------------------------------------------------------

class TestScenarioC:
    def test_custom_conditional_required_immediate(self, tmp_path):
        home = tmp_path / "home"
        path = str(home / "constraints.yaml")
        ConstraintConfig.add_rule(
            path,
            Constraint.from_dict({
                "id": "app_env_log",
                "type": "conditional_required",
                "when": {"key": "app.env", "value": "prod"},
                "then": {"require": ["log.level"]},
                "message": "{key} 缺失（app.env=prod 需要该字段）",
                "severity": "WARN",
            }, source="user"),
        )
        # resolve() must pick up the user file from <home>.
        rules = resolve_constraints(str(home), builtin_enabled=False)
        assert [c.id for c in rules] == ["app_env_log"]
        items, _ = _diff({"app": {"env": "dev"}},
                         {"app": {"env": "prod"}},
                         constraints=rules)
        by_key = {it.key_path: it for it in items}
        assert by_key["app.env"].constraint_violations
        assert by_key["app.env"].constraint_violations[0][
            "constraint_id"] == "app_env_log"

    def test_resolve_merge_order_last_wins(self, tmp_path):
        home = tmp_path / "home"
        ConstraintConfig.add_rule(
            str(home / "constraints.yaml"),
            Constraint.from_dict({
                "id": "http_port_range",  # same id as a builtin -> override
                "type": "range", "keys": ["server.port"],
                "min": 1, "max": 100000,
                "message": "overridden upper bound",
            }, source="user"),
        )
        rules = resolve_constraints(str(home), builtin_enabled=True)
        by_id = {c.id: c for c in rules}
        assert by_id["http_port_range"].message == "overridden upper bound"
        assert by_id["http_port_range"].max == 100000


# ---------------------------------------------------------------------------
# d. Zero-noise contract (D7)
# ---------------------------------------------------------------------------

class TestZeroNoise:
    def test_legal_port_change_no_violation(self):
        items, summary = _diff({"server": {"port": 8080}},
                               {"server": {"port": 9090}},
                               constraints=BUILTIN_CONSTRAINTS)
        assert items[0].constraint_violations == []
        assert items[0].severity == Severity.WARN  # unchanged
        assert summary.max_severity == Severity.WARN
        assert "constraint_violations" not in items[0].to_dict()

    def test_tls_true_to_false_does_not_trigger_when(self):
        # tls.enabled true -> false: the when-condition is not satisfied in
        # the NEW tree, so no violation even though cert_path is absent.
        # tls.cert_path is REMOVED (a drift item) but must stay clean too.
        items, _ = _diff({"tls": {"enabled": True, "cert_path": "/a"}},
                         {"tls": {"enabled": False}},
                         constraints=BUILTIN_CONSTRAINTS)
        by_key = {it.key_path: it for it in items}
        assert by_key["tls.enabled"].constraint_violations == []
        assert by_key["tls.cert_path"].constraint_violations == []

    def test_added_info_item_no_violation(self):
        # Adding a whole new subtree produces one top-level ADDED item
        # (v0.5.0 semantics: no recursive expansion of a brand-new key).
        items, summary = _diff({"server": {"port": 8080}},
                               {"server": {"port": 8080},
                                "logging": {"level": "info"}},
                               constraints=BUILTIN_CONSTRAINTS)
        item = items[0]
        assert item.key_path == "logging"
        assert item.change_type == ChangeType.ADDED
        assert item.severity == Severity.INFO  # builtin INFO, not upgraded
        assert item.constraint_violations == []
        assert "constraint_violations" not in item.to_dict()

    def test_plain_vs_constraint_path_byte_identical(self):
        # Passing constraints=BUILTIN_CONSTRAINTS over a LEGAL change must
        # produce byte-identical output to passing None.
        differ = SemanticDiffer()
        old = {"app.json": {"server": {"port": 8080}}}
        new = {"app.json": {"server": {"port": 9090}}}
        items_plain, sum_plain = differ.diff_snapshot(old, new)
        items_c, sum_c = differ.diff_snapshot(
            old, new, constraints=BUILTIN_CONSTRAINTS)
        assert [it.to_dict() for it in items_plain] == \
            [it.to_dict() for it in items_c]
        assert sum_plain.to_dict() == sum_c.to_dict()

    def test_to_dict_no_key_when_empty(self):
        item = _mk_item()
        d = item.to_dict()
        assert "constraint_violations" not in d


# ---------------------------------------------------------------------------
# e. Five constraint types — hit / boundary / skip semantics
# ---------------------------------------------------------------------------

class TestRangeBoundaries:
    def test_min_max_inclusive(self):
        c = Constraint(id="r", type="range", message="m", keys=["x"],
                       min=1, max=10)
        assert ConstraintEngine.check_one(c, {"x": 1}) == []   # == min ok
        assert ConstraintEngine.check_one(c, {"x": 10}) == []  # == max ok
        assert len(ConstraintEngine.check_one(c, {"x": 0})) == 1   # < min
        assert len(ConstraintEngine.check_one(c, {"x": 11})) == 1  # > max

    def test_min_only_and_max_only(self):
        c1 = Constraint(id="r1", type="range", message="m", keys=["x"], min=5)
        assert ConstraintEngine.check_one(c1, {"x": 5}) == []
        assert len(ConstraintEngine.check_one(c1, {"x": 4})) == 1
        c2 = Constraint(id="r2", type="range", message="m", keys=["x"], max=5)
        assert ConstraintEngine.check_one(c2, {"x": 5}) == []
        assert len(ConstraintEngine.check_one(c2, {"x": 6})) == 1

    def test_non_numeric_skipped(self):
        c = Constraint(id="r", type="range", message="m", keys=["x"],
                       min=1, max=10)
        for value in ("abc", True, None, [1], {"a": 1}):
            assert ConstraintEngine.check_one(c, {"x": value}) == []

    def test_missing_key_skipped(self):
        c = Constraint(id="r", type="range", message="m", keys=["a.b.c"],
                       min=1, max=10)
        assert ConstraintEngine.check_one(c, {"a": {"b": {}}}) == []


class TestEnum:
    def test_valid_and_invalid(self):
        c = Constraint(id="e", type="enum", message="m", keys=["level"],
                       allowed=["debug", "info", "warn", "error"])
        for v in ("debug", "info", "warn", "error"):
            assert ConstraintEngine.check_one(c, {"level": v}) == []
        assert len(ConstraintEngine.check_one(c, {"level": "verbose"})) == 1

    def test_missing_skipped(self):
        c = Constraint(id="e", type="enum", message="m", keys=["level"],
                       allowed=["a", "b"])
        assert ConstraintEngine.check_one(c, {}) == []


class TestConditionalRequired:
    C = Constraint(id="cr", type="conditional_required", message="{key} 缺失",
                   when={"key": "tls.enabled", "value": True},
                   then={"require": ["tls.cert_path", "tls.key_path"]})

    def test_when_met_missing_keys(self):
        vs = ConstraintEngine.check_one(self.C, {"tls": {"enabled": True}})
        assert len(vs) == 2
        assert {v.message for v in vs} == {"tls.cert_path 缺失",
                                           "tls.key_path 缺失"}
        assert all(v.involved_keys == ["tls.enabled", "tls.cert_path"] or
                   v.involved_keys == ["tls.enabled", "tls.key_path"]
                   for v in vs)

    def test_when_not_met(self):
        assert ConstraintEngine.check_one(
            self.C, {"tls": {"enabled": False}}) == []
        assert ConstraintEngine.check_one(self.C, {"tls": {}}) == []

    def test_all_required_present(self):
        assert ConstraintEngine.check_one(
            self.C, {"tls": {"enabled": True, "cert_path": "a",
                             "key_path": "b"}}) == []


class TestCorrelationOps:
    def _run(self, op, actual, expected):
        c = Constraint(id="co", type="correlation", message="m",
                       when={"key": "mode", "value": "cluster"},
                       then=[{"key": "x", "op": op, "value": expected}])
        return ConstraintEngine.check_one(
            c, {"mode": "cluster", "x": actual})

    def test_ge(self):
        assert self._run(">=", 10, 10) == []   # equal ok
        assert self._run(">=", 11, 10) == []
        assert len(self._run(">=", 9, 10)) == 1  # below -> violation

    def test_gt(self):
        assert self._run(">", 11, 10) == []
        assert len(self._run(">", 10, 10)) == 1  # equal violates >

    def test_le(self):
        assert self._run("<=", 10, 10) == []
        assert self._run("<=", 9, 10) == []
        assert len(self._run("<=", 11, 10)) == 1

    def test_lt(self):
        assert self._run("<", 9, 10) == []
        assert len(self._run("<", 10, 10)) == 1

    def test_eq(self):
        assert self._run("==", 7, 7) == []
        assert len(self._run("==", 8, 7)) == 1

    def test_ne(self):
        assert self._run("!=", 8, 7) == []
        assert len(self._run("!=", 7, 7)) == 1

    def test_missing_target_skipped(self):
        c = Constraint(id="co", type="correlation", message="m",
                       when={"key": "mode", "value": "cluster"},
                       then=[{"key": "x", "op": ">=", "value": 3}])
        assert ConstraintEngine.check_one(c, {"mode": "cluster"}) == []


class TestMutualExclusion:
    def test_default_coexist_conflict(self):
        c = Constraint(id="me", type="mutual_exclusion", message="conflict",
                       keys=["protocol", "ssl"])
        vs = ConstraintEngine.check_one(
            c, {"protocol": "http", "ssl": "off"})
        assert len(vs) == 1
        assert vs[0].involved_keys == ["protocol", "ssl"]
        # one key present -> no conflict
        assert ConstraintEngine.check_one(c, {"protocol": "http"}) == []

    def test_forbid_pair_hit(self):
        c = Constraint(id="me", type="mutual_exclusion", message="conflict",
                       keys=["protocol", "ssl"], forbid=[["http", "on"]])
        assert len(ConstraintEngine.check_one(
            c, {"protocol": "http", "ssl": "on"})) == 1
        assert ConstraintEngine.check_one(
            c, {"protocol": "https", "ssl": "on"}) == []
        assert ConstraintEngine.check_one(
            c, {"protocol": "http", "ssl": "off"}) == []

    def test_missing_key_skipped(self):
        c = Constraint(id="me", type="mutual_exclusion", message="conflict",
                       keys=["protocol", "ssl"], forbid=[["http", "on"]])
        assert ConstraintEngine.check_one(c, {"protocol": "http"}) == []


# ---------------------------------------------------------------------------
# f. Severity upgrade formula (Q1 / 6.4)
# ---------------------------------------------------------------------------

class TestUpgradeFormula:
    def _apply(self, item_sev, constraint_sevs, item_key="a"):
        item = _mk_item(item_key, old=0, new=99, severity=item_sev)
        constraints = [
            Constraint(id="c%d" % i, type="range", message="m", keys=[item_key],
                       min=1, max=5, severity=sev)
            for i, sev in enumerate(constraint_sevs)
        ]
        ConstraintEngine.apply({item.file: {item_key: 99}}, [item], constraints)
        return item

    def test_warn_item_warn_constraint_critical(self):
        item = self._apply(Severity.WARN, [Severity.WARN])
        assert item.severity == Severity.CRITICAL  # min(3, max(2+1, 2))

    def test_info_item_warn_constraint_warn(self):
        item = self._apply(Severity.INFO, [Severity.WARN])
        assert item.severity == Severity.WARN  # min(3, max(1+1, 2))

    def test_critical_item_unchanged(self):
        item = self._apply(Severity.CRITICAL, [Severity.CRITICAL])
        assert item.severity == Severity.CRITICAL  # capped

    def test_multi_violation_takes_max(self):
        item = self._apply(Severity.WARN, [Severity.INFO, Severity.CRITICAL])
        assert item.severity == Severity.CRITICAL  # max(2+1, 1, 3)

    def test_upgrade_once_not_compounding(self):
        # INFO(1) + two WARN(2) constraints.  One-shot:
        # min(3, max(1+1, 2)) = 2 -> WARN.  Compounding would reach
        # CRITICAL (second pass over the already-WARN item), so WARN is the
        # discriminating assertion.
        item = self._apply(Severity.INFO, [Severity.WARN, Severity.WARN])
        assert item.severity == Severity.WARN
        assert len(item.constraint_violations) == 2


# ---------------------------------------------------------------------------
# g. Association (D5) + Q2 (pre-existing violations not reported)
# ---------------------------------------------------------------------------

class TestAssociation:
    def test_attach_to_all_involved_drift_items(self):
        items = [
            _mk_item("tls.enabled", old=False, new=True),
            DriftItem(key_path="tls.cert_path",
                      change_type=ChangeType.REMOVED,
                      severity=Severity.CRITICAL, file="app.json",
                      old_value="/a", new_value=None,
                      old_type="str", new_type=None),
            _mk_item("server.port", old=8080, new=9090),
        ]
        ConstraintEngine.apply(
            {"app.json": {"tls": {"enabled": True}}},
            items,
            [BUILTIN_CONSTRAINTS_BY_ID["http_ssl_cert_required"]],
        )
        by_key = {it.key_path: it for it in items}
        assert by_key["tls.enabled"].constraint_violations
        assert by_key["tls.cert_path"].constraint_violations
        assert by_key["tls.enabled"].severity == Severity.CRITICAL
        assert by_key["tls.cert_path"].severity == Severity.CRITICAL
        assert not by_key["server.port"].constraint_violations
        assert by_key["server.port"].severity == Severity.WARN

    def test_pre_existing_violation_not_reported_q2(self):
        # baseline already violates (tls.enabled=true, no cert_path); only an
        # unrelated key drifts -> the pre-existing break must NOT surface.
        old = {"nginx.conf": {"tls": {"enabled": True},
                              "server": {"port": 8080}}}
        new = {"nginx.conf": {"tls": {"enabled": True},
                              "server": {"port": 9090}}}
        differ = SemanticDiffer()
        items, summary = differ.diff_snapshot(
            old, new, constraints=BUILTIN_CONSTRAINTS)
        assert [it.key_path for it in items] == ["server.port"]
        assert items[0].constraint_violations == []
        assert items[0].severity == Severity.WARN
        assert summary.total == 1

    def test_no_drift_no_violation_even_when_tree_breaks(self):
        old = {"nginx.conf": {"tls": {"enabled": True}}}
        new = {"nginx.conf": {"tls": {"enabled": True}}}
        _, summary = SemanticDiffer().diff_snapshot(
            old, new, constraints=BUILTIN_CONSTRAINTS)
        assert summary.total == 0


# ---------------------------------------------------------------------------
# h. CLI constraint management
# ---------------------------------------------------------------------------

CLI_RULE = json.dumps({
    "id": "qa_rule", "type": "range", "keys": ["server.port"],
    "min": 1, "max": 65535, "message": "qa rule message",
})


class TestCliConstraints:
    def test_add_valid_exit0_and_file(self, tmp_path):
        home = tmp_path / "home"
        r = _run_cli(home, ["constraint", "add", "--rule", CLI_RULE])
        assert r.returncode == 0, r.stderr
        assert "constraint 'qa_rule' added" in r.stdout
        rules = ConstraintConfig.list_rules(str(home / "constraints.yaml"))
        assert [x.id for x in rules] == ["qa_rule"]
        assert rules[0].source == "user" and rules[0].enabled is True

    def test_add_invalid_json_exit2(self, tmp_path):
        r = _run_cli(tmp_path / "home",
                     ["constraint", "add", "--rule", "{bad json"])
        assert r.returncode == 2
        assert "invalid --rule JSON" in r.stderr

    def test_add_missing_message_exit2(self, tmp_path):
        bad = json.dumps({"id": "x", "type": "range", "keys": ["a"],
                          "min": 1})  # no message
        r = _run_cli(tmp_path / "home", ["constraint", "add", "--rule", bad])
        assert r.returncode == 2
        assert "message" in r.stderr

    def test_add_bad_type_exit2(self, tmp_path):
        bad = json.dumps({"id": "x", "type": "bogus", "message": "m"})
        r = _run_cli(tmp_path / "home", ["constraint", "add", "--rule", bad])
        assert r.returncode == 2

    def test_add_duplicate_exit2(self, tmp_path):
        home = tmp_path / "home"
        assert _run_cli(home, ["constraint", "add", "--rule",
                               CLI_RULE]).returncode == 0
        r = _run_cli(home, ["constraint", "add", "--rule", CLI_RULE])
        assert r.returncode == 2
        assert "already exists" in r.stderr

    def test_list_shows_source_and_enabled(self, tmp_path):
        home = tmp_path / "home"
        _run_cli(home, ["constraint", "add", "--rule", CLI_RULE])
        _run_cli(home, ["constraint", "disable", "qa_rule"])
        r = _run_cli(home, ["constraint", "list", "--source", "user"])
        assert r.returncode == 0
        assert "qa_rule" in r.stdout
        assert "enabled=no" in r.stdout
        assert "source=user" in r.stdout
        r = _run_cli(home, ["constraint", "list", "--source", "builtin"])
        assert r.stdout.count("source=builtin") == len(BUILTIN_CONSTRAINTS)

    def test_remove_enable_disable(self, tmp_path):
        home = tmp_path / "home"
        _run_cli(home, ["constraint", "add", "--rule", CLI_RULE])
        path = str(home / "constraints.yaml")
        assert _run_cli(home, ["constraint", "disable",
                               "qa_rule"]).returncode == 0
        assert ConstraintConfig.list_rules(path)[0].enabled is False
        assert _run_cli(home, ["constraint", "enable",
                               "qa_rule"]).returncode == 0
        assert ConstraintConfig.list_rules(path)[0].enabled is True
        assert _run_cli(home, ["constraint", "remove",
                               "qa_rule"]).returncode == 0
        assert ConstraintConfig.list_rules(path) == []

    def test_diff_builtin_off(self, tmp_path):
        home = tmp_path / "home"
        store_path = tmp_path / "db" / "cfgdrift.db"
        conf = tmp_path / "conf"
        _write(str(conf / "app.json"),
               '{"server": {"port": 8080}}\n')
        _run_cli(home, ["baseline", "create", "prod", "--scan-root",
                        str(conf)], store=store_path)
        _write(str(conf / "app.json"),
               '{"server": {"port": 99999}}\n')
        r = _run_cli(home, ["diff", str(conf), "--baseline", "prod",
                            "--no-builtin"], store=store_path)
        assert r.returncode == 1, r.stderr
        assert "constraint" not in r.stdout  # built-in library disabled
        assert "[WARN]" in r.stdout  # builtin severity kept

    def test_constraints_extra_file_missing_exit2(self, tmp_path):
        home = tmp_path / "home"
        store_path = tmp_path / "db" / "cfgdrift.db"
        conf = tmp_path / "conf"
        _write(str(conf / "app.json"), '{"server": {"port": 8080}}\n')
        _run_cli(home, ["baseline", "create", "prod", "--scan-root",
                        str(conf)], store=store_path)
        r = _run_cli(home, ["diff", str(conf), "--baseline", "prod",
                            "--constraints", str(tmp_path / "nope.yaml")],
                     store=store_path)
        assert r.returncode == 2

    def test_resolve_raises_on_missing_extra(self, tmp_path):
        with pytest.raises(ValueError):
            resolve_constraints(str(tmp_path / "home"),
                                extra_paths=[str(tmp_path / "nope.yaml")])


# ---------------------------------------------------------------------------
# i. daemon worker — argparse (round-7 review found a real bug here)
# ---------------------------------------------------------------------------

class TestDaemonWorkerArgparse:
    """The worker CLI is the single entry point used by build_worker_command
    and by ``daemon start``.  A regression test asserts that the produced
    flags parse successfully (not just that --help displays them)."""

    def _run_main_with_flags(self, flags):
        called = {}
        orig = worker_mod.run_with_opts
        worker_mod.run_with_opts = lambda opts: (called.update(opts=opts), 0)[1]
        try:
            return worker_mod.main(
                ["--home", "h", "--store", "s", "--baseline", "b",
                 "--path", "p"] + flags
            ), called
        finally:
            worker_mod.run_with_opts = orig

    def test_worker_main_accepts_no_builtin(self):
        # build_worker_command emits --no-builtin when builtin is off; the
        # worker must accept it.  Currently the parser registers the flags as
        # ONE value-taking option "--builtin/--no-builtin", so argparse exits
        # 2 with "unrecognized arguments: --no-builtin" -> daemon startup
        # with --no-builtin is broken.
        code, called = self._run_main_with_flags(["--no-builtin"])
        assert code == 0
        assert called["opts"]["builtin"] is False

    def test_worker_main_accepts_builtin(self):
        code, called = self._run_main_with_flags(["--builtin"])
        assert code == 0
        assert called["opts"]["builtin"] is True

    def test_worker_main_passes_constraint_paths(self):
        code, called = self._run_main_with_flags(
            ["--constraints", "extra1.yaml", "--constraints", "extra2.yaml"])
        assert code == 0
        assert called["opts"]["constraint_paths"] == ["extra1.yaml",
                                                      "extra2.yaml"]


class TestBuildWorkerCommand:
    def test_no_builtin_emitted_when_off(self):
        cmd = worker_mod.build_worker_command(
            "C:/home", {"store": "s", "baseline": "b", "targets": ["/x"],
                        "builtin": False})
        assert "--no-builtin" in cmd

    def test_no_flags_by_default(self):
        cmd = worker_mod.build_worker_command(
            "C:/home", {"store": "s", "baseline": "b", "targets": ["/x"]})
        assert "--no-builtin" not in cmd
        assert "--constraints" not in cmd

    def test_constraints_passthrough(self):
        cmd = worker_mod.build_worker_command(
            "C:/home", {"store": "s", "baseline": "b", "targets": ["/x"],
                        "constraint_paths": ["a.yaml", "b.yaml"]})
        assert cmd.count("--constraints") == 2
        i = cmd.index("--constraints")
        assert cmd[i + 1] == "a.yaml" and cmd[i + 3] == "b.yaml"


# ---------------------------------------------------------------------------
# j. Presentation — five exits
# ---------------------------------------------------------------------------

def _scenario_a_report():
    differ = SemanticDiffer()
    items, summary = differ.diff_snapshot(
        {"nginx.conf": {"tls": {"enabled": False}, "server": {"port": 8080}}},
        {"nginx.conf": {"tls": {"enabled": True}, "server": {"port": 8080}}},
        constraints=BUILTIN_CONSTRAINTS,
    )
    return Report(None, None, "2026-08-04T00:00:00+00:00", "manual",
                  summary, items)


class TestPresentation:
    def test_terminal_constraint_line(self):
        text = Reporter().render_terminal(_scenario_a_report(),
                                          color=False, masker=None)
        assert "constraint http_ssl_cert_required" in text
        assert "[conditional_required]" in text

    def test_json_via_report_to_json(self):
        doc = json.loads(_scenario_a_report().to_json())
        item = doc["data"]["items"][0]
        assert item["constraint_violations"][0]["constraint_id"] == \
            "http_ssl_cert_required"

    def test_html_column_and_dash(self):
        from cfgdrift.core.htmlreport import HtmlReporter
        items = [{
            "key_path": "server.port", "change_type": "modified",
            "severity": "WARN", "file": "app.json",
            "old_value": 8080, "new_value": 9090,
            "rule_id": None, "line": None, "masked": False,
        }]
        html = HtmlReporter._items_table(items)
        assert "<td>-</td>" in html
        full = HtmlReporter.render_html({"items": items}, title="t")
        assert "约束违反" in full

    def test_html_renders_violation(self):
        from cfgdrift.core.htmlreport import HtmlReporter
        items, _ = _diff({"server": {"port": 8080}},
                         {"server": {"port": 99999}},
                         constraints=BUILTIN_CONSTRAINTS)
        html = HtmlReporter._items_table([it.to_dict() for it in items])
        assert "http_port_range" in html
        assert "[range]" in html

    def test_store_payload_carries_violations_for_web(self, tmp_path):
        # Web /api/reports/{id} reads report_json straight from the store;
        # assert the persisted payload (the Web data source) carries the
        # violations (app.py is unchanged by design).
        from cfgdrift.storage.store import Store
        home = tmp_path / "home"
        store_path = tmp_path / "db" / "cfgdrift.db"
        conf = tmp_path / "conf"
        _write(str(conf / "app.json"), '{"server": {"port": 8080}}\n')
        _run_cli(home, ["baseline", "create", "prod", "--scan-root",
                        str(conf)], store=store_path)
        _write(str(conf / "app.json"), '{"server": {"port": 99999}}\n')
        r = _run_cli(home, ["diff", str(conf), "--baseline", "prod"],
                     store=store_path)
        assert r.returncode == 1, r.stderr
        store = Store(str(store_path))
        try:
            payload = store.get_scan(
                store.list_scans(limit=1)[0]["scan_id"])
            item = payload["data"]["items"][0]
            assert item["constraint_violations"][0]["constraint_id"] == \
                "http_port_range"
        finally:
            store.close()

    def test_alert_payload_constraint_only_on_violated_items(self):
        from cfgdrift.alert.models import build_drift_payload
        clean = DriftItem(
            key_path="server.port", change_type=ChangeType.MODIFIED,
            severity=Severity.WARN, file="app.json",
            old_value=8080, new_value=9090, old_type="int", new_type="int",
        )
        violated = DriftItem(
            key_path="server.port", change_type=ChangeType.MODIFIED,
            severity=Severity.CRITICAL, file="app.json",
            old_value=8080, new_value=99999, old_type="int", new_type="int",
        )
        violated.constraint_violations = [{
            "constraint_id": "http_port_range", "type": "range",
            "message": "server.port 必须在 [1, 65535] 范围内",
            "involved_keys": ["server.port"],
        }]
        summary = ScanSummary()
        summary.max_severity = Severity.CRITICAL
        report = Report(None, None, "2026-08-04T00:00:00+00:00", "manual",
                        summary, [clean, violated])
        payload = build_drift_payload(report, "b", "t", "0.6.0")
        items = payload["drift_items"]
        assert "constraint" not in items[0]
        assert items[1]["constraint"]["id"] == "http_port_range"


# ---------------------------------------------------------------------------
# k. Performance — 10k keys, incremental < 10 ms
# ---------------------------------------------------------------------------

class TestPerformance:
    def test_10k_keys_incremental_under_10ms(self):
        tree = {"k%06d" % i: i for i in range(10000)}
        tree["server"] = {"port": 99999}
        snapshot = {"big.json": tree}
        items = [_mk_item("server.port", old=8080, new=99999, file="big.json")]
        start = time.perf_counter()
        ConstraintEngine.apply(snapshot, items, BUILTIN_CONSTRAINTS)
        elapsed_ms = (time.perf_counter() - start) * 1000
        assert items[0].constraint_violations
        assert elapsed_ms < 10.0, "constraint check took %.2f ms" % elapsed_ms


# ---------------------------------------------------------------------------
# l. Regression — v0.5.0 semantics
# ---------------------------------------------------------------------------

class TestRegression:
    def test_plain_diff_semantics_unchanged(self):
        # MODIFIED -> WARN, ADDED -> INFO, REMOVED/TYPE_CHANGED -> CRITICAL.
        differ = SemanticDiffer()
        old = {"app.json": {"a": 1, "b": "x", "c": {"d": 1}}}
        new = {"app.json": {"a": 2, "b": 5, "e": 1}}
        items, summary = differ.diff_snapshot(old, new)
        by_key = {it.key_path: it for it in items}
        assert by_key["a"].severity == Severity.WARN
        assert by_key["b"].severity == Severity.CRITICAL  # type_changed
        assert by_key["e"].severity == Severity.INFO  # added
        assert by_key["c"].severity == Severity.CRITICAL  # removed (whole tree)
        assert summary.total == 4
        assert summary.max_severity == Severity.CRITICAL
