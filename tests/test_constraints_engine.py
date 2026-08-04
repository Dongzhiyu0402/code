"""Engine unit tests for cfgdrift v0.6.0 — consistency constraint engine (T01/T02).

Covers:

1. ``Constraint`` model validation per type (corrupt -> ValueError) and
   ``to_dict`` / ``from_dict`` round-trips.
2. ``ConstraintEngine.check_one`` for all five types: hit / skip / missing-key
   / non-numeric / unsatisfied-when semantics.
3. ``ConstraintEngine.apply``: per-file association (involved_keys ∩
   drift_keys), violation attachment to all involved drift items, one-shot
   severity upgrade (Q1 formula), zero-noise (no constraints -> no field).
4. 10k-key performance micro-benchmark (incremental check < 10 ms).
"""

from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cfgdrift.core.constraints import (  # noqa: E402
    BUILTIN_CONSTRAINTS,
    BUILTIN_CONSTRAINTS_BY_ID,
    ConstraintEngine,
    apply_constraints,
)
from cfgdrift.core.differ import SemanticDiffer  # noqa: E402
from cfgdrift.core.model import (  # noqa: E402
    ChangeType,
    Constraint,
    ConstraintViolation,
    DriftItem,
    Severity,
)


def _make_item(key_path="server.port", change=ChangeType.MODIFIED,
               severity=Severity.WARN, file="app.json", old=8080, new=9090):
    return DriftItem(
        key_path=key_path,
        change_type=change,
        severity=severity,
        file=file,
        old_value=old,
        new_value=new,
        old_type="int",
        new_type="int",
    )


# ---------------------------------------------------------------------------
# 1. Constraint model validation
# ---------------------------------------------------------------------------

class TestConstraintModel:
    def test_range_valid_and_invalid(self):
        c = Constraint(id="r", type="range", message="m", keys=["a.b"], min=1, max=5)
        assert c.severity == Severity.WARN and c.source == "builtin"
        with pytest.raises(ValueError):
            Constraint(id="r", type="range", message="m", keys=[])  # no key
        with pytest.raises(ValueError):
            Constraint(id="r", type="range", message="m", keys=["a", "b"])  # 2 keys
        with pytest.raises(ValueError):
            Constraint(id="r", type="range", message="m", keys=["a"])  # no bounds
        with pytest.raises(ValueError):
            Constraint(id="r", type="range", message="m", keys=["a"], min="x")  # non-num

    def test_enum_valid_and_invalid(self):
        c = Constraint(id="e", type="enum", message="m", keys=["x"],
                       allowed=["a", "b"])
        assert c.allowed == ["a", "b"]
        with pytest.raises(ValueError):
            Constraint(id="e", type="enum", message="m", keys=["x"], allowed=[])
        with pytest.raises(ValueError):
            Constraint(id="e", type="enum", message="m", keys=["x"])

    def test_conditional_required_validation(self):
        c = Constraint(id="cr", type="conditional_required", message="{key} 缺失",
                       when={"key": "tls.enabled", "value": True},
                       then={"require": ["tls.cert_path"]})
        assert c.then == {"require": ["tls.cert_path"]}
        with pytest.raises(ValueError):
            Constraint(id="cr", type="conditional_required", message="m",
                       when={"key": "tls.enabled", "value": True}, then={})
        with pytest.raises(ValueError):
            Constraint(id="cr", type="conditional_required", message="m",
                       when={}, then={"require": ["a"]})

    def test_correlation_normalizes_then_to_list(self):
        c = Constraint(id="co", type="correlation", message="m",
                       when={"key": "mode", "value": "cluster"},
                       then={"key": "replicas", "op": ">=", "value": 3})
        assert c.then == [{"key": "replicas", "op": ">=", "value": 3}]
        with pytest.raises(ValueError):
            Constraint(id="co", type="correlation", message="m",
                       when={"key": "mode", "value": "cluster"},
                       then=[{"key": "replicas", "op": "??", "value": 3}])

    def test_mutual_exclusion_validation(self):
        c = Constraint(id="me", type="mutual_exclusion", message="m",
                       keys=["a", "b"], forbid=[["x", "y"]])
        assert c.forbid == [["x", "y"]]
        with pytest.raises(ValueError):
            Constraint(id="me", type="mutual_exclusion", message="m", keys=["a"])
        with pytest.raises(ValueError):
            Constraint(id="me", type="mutual_exclusion", message="m",
                       keys=["a", "b"], forbid=[["x"]])  # not a pair

    def test_common_validation_and_severity_coercion(self):
        with pytest.raises(ValueError):
            Constraint(id="", type="range", message="m", keys=["a"], min=1)
        with pytest.raises(ValueError):
            Constraint(id="x", type="bogus", message="m")
        with pytest.raises(ValueError):
            Constraint(id="x", type="range", message="", keys=["a"], min=1)
        c = Constraint(id="x", type="range", message="m", keys=["a"],
                       min=1, severity="CRITICAL")
        assert c.severity == Severity.CRITICAL
        with pytest.raises(ValueError):
            Constraint(id="x", type="range", message="m", keys=["a"],
                       min=1, source="nope")

    def test_from_dict_to_dict_roundtrip(self):
        raw = {
            "id": "my_port",
            "type": "range",
            "message": "port out of range",
            "severity": "WARN",
            "keys": ["server.port"],
            "min": 1,
            "max": 65535,
            "enabled": False,
        }
        c = Constraint.from_dict(raw, source="user")
        assert c.source == "user" and c.enabled is False
        d = c.to_dict()
        assert d["id"] == "my_port" and d["type"] == "range"
        assert d["severity"] == "WARN" and d["source"] == "user"
        # reload from to_dict
        c2 = Constraint.from_dict(d, source="user")
        assert c2.id == c.id and c2.min == 1 and c2.max == 65535

    def test_from_dict_missing_fields_raise(self):
        with pytest.raises(ValueError):
            Constraint.from_dict({"type": "range"}, source="user")
        with pytest.raises(ValueError):
            Constraint.from_dict({"id": "x", "type": "range", "message": "m",
                                  "keys": []}, source="user")
        with pytest.raises(ValueError):
            Constraint.from_dict({"id": "x", "type": "range", "message": "m",
                                  "keys": ["a"], "severity": "BOGUS"},
                                 source="user")


# ---------------------------------------------------------------------------
# 2. check_one — five types, hit / skip semantics
# ---------------------------------------------------------------------------

class TestCheckOne:
    def test_range_hit_and_skip(self):
        c = Constraint(id="r", type="range", message="range msg",
                       keys=["server.port"], min=1, max=65535)
        # hit: above max
        vs = ConstraintEngine.check_one(c, {"server": {"port": 99999}})
        assert len(vs) == 1 and vs[0].constraint_id == "r"
        assert vs[0].involved_keys == ["server.port"]
        # below min
        vs = ConstraintEngine.check_one(c, {"server": {"port": 0}})
        assert len(vs) == 1
        # in range -> none
        assert ConstraintEngine.check_one(c, {"server": {"port": 9090}}) == []
        # missing key -> skip
        assert ConstraintEngine.check_one(c, {"server": {}}) == []
        # non-numeric -> skip
        assert ConstraintEngine.check_one(c, {"server": {"port": "abc"}}) == []

    def test_enum_hit_and_skip(self):
        c = Constraint(id="e", type="enum", message="enum msg",
                       keys=["logging.level"],
                       allowed=["debug", "info", "warn", "error"])
        assert ConstraintEngine.check_one(c, {"logging": {"level": "warn"}}) == []
        vs = ConstraintEngine.check_one(c, {"logging": {"level": "verbose"}})
        assert len(vs) == 1 and vs[0].type == "enum"
        assert ConstraintEngine.check_one(c, {"logging": {}}) == []  # missing

    def test_conditional_required_hit_and_skip(self):
        c = Constraint(id="cr", type="conditional_required", message="{key} 缺失",
                       when={"key": "tls.enabled", "value": True},
                       then={"require": ["tls.cert_path", "tls.key_path"]})
        vs = ConstraintEngine.check_one(
            c, {"tls": {"enabled": True}})
        assert len(vs) == 2  # one per missing required key
        assert vs[0].involved_keys == ["tls.enabled", "tls.cert_path"]
        assert vs[0].message == "tls.cert_path 缺失"
        # when not satisfied -> skip
        assert ConstraintEngine.check_one(
            c, {"tls": {"enabled": False}}) == []
        # when key missing -> skip
        assert ConstraintEngine.check_one(c, {}) == []
        # satisfied -> none
        assert ConstraintEngine.check_one(
            c, {"tls": {"enabled": True, "cert_path": "a", "key_path": "b"}}) == []

    def test_correlation_hit_and_skip(self):
        c = Constraint(id="co", type="correlation", message="replicas",
                       when={"key": "mode", "value": "cluster"},
                       then=[{"key": "replicas", "op": ">=", "value": 3}])
        assert ConstraintEngine.check_one(
            c, {"mode": "cluster", "replicas": 3}) == []
        vs = ConstraintEngine.check_one(
            c, {"mode": "cluster", "replicas": 2})
        assert len(vs) == 1 and vs[0].involved_keys == ["mode", "replicas"]
        # target key missing -> skip (no violation)
        assert ConstraintEngine.check_one(
            c, {"mode": "cluster"}) == []
        # when not satisfied -> skip
        assert ConstraintEngine.check_one(
            c, {"mode": "single", "replicas": 1}) == []

    def test_mutual_exclusion_default_and_forbid(self):
        c = Constraint(id="me", type="mutual_exclusion", message="conflict",
                       keys=["protocol", "ssl"])
        # default (no forbid): both keys present -> conflict
        vs = ConstraintEngine.check_one(
            c, {"protocol": "http", "ssl": "off"})
        assert len(vs) == 1 and set(vs[0].involved_keys) == {"protocol", "ssl"}
        # only one key present -> no conflict
        assert ConstraintEngine.check_one(c, {"protocol": "http"}) == []

        cf = Constraint(id="me2", type="mutual_exclusion", message="conflict",
                        keys=["protocol", "ssl"], forbid=[["http", "on"]])
        assert ConstraintEngine.check_one(
            cf, {"protocol": "http", "ssl": "on"}) != []
        assert ConstraintEngine.check_one(
            cf, {"protocol": "https", "ssl": "on"}) == []

    def test_disabled_constraint_skipped(self):
        c = Constraint(id="d", type="range", message="m", keys=["a"],
                       min=1, max=5, enabled=False)
        assert ConstraintEngine.check_one(c, {"a": 99}) == []

    def test_unknown_type_skipped(self):
        c = Constraint(id="u", type="range", message="m", keys=["a"], min=1)
        # force an unknown type through the validator table lookup
        c.type = "bogus"
        assert ConstraintEngine.check_one(c, {"a": 99}) == []


# ---------------------------------------------------------------------------
# 3. apply — association + attachment + upgrade
# ---------------------------------------------------------------------------

class TestApply:
    def test_association_and_attachment(self):
        items = [_make_item("tls.enabled", old=False, new=True)]
        new_tree = {"app.json": {"tls": {"enabled": True}}}
        constraints = [BUILTIN_CONSTRAINTS_BY_ID["http_ssl_cert_required"]]
        ConstraintEngine.apply(new_tree, items, constraints)
        assert len(items[0].constraint_violations) == 2
        assert items[0].severity == Severity.CRITICAL

    def test_attach_to_all_involved_drift_items(self):
        # tls.enabled modified AND tls.cert_path removed -> violations attach
        # to both drift items (their key_path ∈ involved_keys); server.port
        # is not involved.
        items = [
            _make_item("tls.enabled", old=False, new=True),
            DriftItem(
                key_path="tls.cert_path", change_type=ChangeType.REMOVED,
                severity=Severity.CRITICAL, file="app.json",
                old_value="/a", new_value=None,
                old_type="str", new_type=None,
            ),
            _make_item("server.port", old=8080, new=9090),
        ]
        new_tree = {"app.json": {"tls": {"enabled": True}}}
        constraints = [BUILTIN_CONSTRAINTS_BY_ID["http_ssl_cert_required"]]
        ConstraintEngine.apply(new_tree, items, constraints)
        by_key = {it.key_path: it for it in items}
        # tls.enabled and tls.cert_path are involved; server.port is not
        assert by_key["tls.enabled"].constraint_violations
        assert by_key["tls.cert_path"].constraint_violations
        assert not by_key["server.port"].constraint_violations
        assert by_key["tls.enabled"].severity == Severity.CRITICAL
        assert by_key["tls.cert_path"].severity == Severity.CRITICAL
        assert by_key["server.port"].severity == Severity.WARN

    def test_upgrade_formula_q1(self):
        # item WARN(2) + violated constraint INFO(1) -> max(2+1, 1) = 3 -> CRITICAL
        c_info = Constraint(id="ci", type="range", message="m", keys=["a"],
                            min=1, max=5, severity=Severity.INFO)
        items = [_make_item("a", old=0, new=99)]
        ConstraintEngine.apply({"app.json": {"a": 99}}, items, [c_info])
        assert items[0].severity == Severity.CRITICAL
        # item INFO(1) + constraint WARN(2) -> max(1+1, 2) = 2 -> WARN
        c_warn = Constraint(id="cw", type="range", message="m", keys=["a"],
                            min=1, max=5, severity=Severity.WARN)
        item2 = _make_item("a", old=0, new=99)
        item2.severity = Severity.INFO
        ConstraintEngine.apply({"app.json": {"a": 99}}, [item2], [c_warn])
        assert item2.severity == Severity.WARN
        # item CRITICAL(3) stays CRITICAL (min cap)
        c_crit = Constraint(id="cc", type="range", message="m", keys=["a"],
                            min=1, max=5, severity=Severity.CRITICAL)
        item3 = _make_item("a", old=0, new=99)
        item3.severity = Severity.CRITICAL
        ConstraintEngine.apply({"app.json": {"a": 99}}, [item3], [c_crit])
        assert item3.severity == Severity.CRITICAL

    def test_only_upgrade_once(self):
        # two violated constraints on the same item -> still one upgrade
        c1 = Constraint(id="c1", type="range", message="m", keys=["a"],
                        min=1, max=5, severity=Severity.WARN)
        c2 = Constraint(id="c2", type="range", message="m", keys=["a"],
                        min=10, max=20, severity=Severity.INFO)
        item = _make_item("a", old=0, new=99)
        ConstraintEngine.apply({"app.json": {"a": 99}}, [item], [c1, c2])
        assert len(item.constraint_violations) == 2
        assert item.severity == Severity.CRITICAL  # max(2+1, 2, 1) = 3

    def test_per_file_association(self):
        # violation in file A must not attach to drift in file B
        items = [
            _make_item("tls.enabled", old=False, new=True, file="a.json"),
            _make_item("server.port", old=8080, new=9090, file="b.json"),
        ]
        new_tree = {
            "a.json": {"tls": {"enabled": True}},
            "b.json": {"server": {"port": 9090}},
        }
        constraints = [BUILTIN_CONSTRAINTS_BY_ID["http_ssl_cert_required"]]
        ConstraintEngine.apply(new_tree, items, constraints)
        assert len(items[0].constraint_violations) == 2
        assert not items[1].constraint_violations

    def test_zero_noise_no_constraints(self):
        items = [_make_item("server.port", old=8080, new=9090)]
        ConstraintEngine.apply(None, items, None)
        assert items[0].constraint_violations == []
        assert items[0].severity == Severity.WARN
        d = items[0].to_dict()
        assert "constraint_violations" not in d

    def test_zero_noise_legal_change(self):
        items, summary = SemanticDiffer().diff_snapshot(
            {"app.json": {"server": {"port": 8080}}},
            {"app.json": {"server": {"port": 9090}}},
            constraints=BUILTIN_CONSTRAINTS,
        )
        assert not items[0].constraint_violations
        assert "constraint_violations" not in items[0].to_dict()
        assert summary.max_severity == Severity.WARN

    def test_disabled_constraints_not_applied(self):
        c = Constraint(id="dc", type="range", message="m", keys=["a"],
                       min=1, max=5, enabled=False)
        item = _make_item("a", old=0, new=99)
        ConstraintEngine.apply({"app.json": {"a": 99}}, [item], [c])
        assert item.constraint_violations == []
        assert item.severity == Severity.WARN

    def test_module_level_apply_constraints(self):
        item = _make_item("a", old=0, new=99)
        c = Constraint(id="m", type="range", message="m", keys=["a"],
                       min=1, max=5)
        apply_constraints({"app.json": {"a": 99}}, [item], [c])
        assert len(item.constraint_violations) == 1

    def test_single_file_diff_integration(self):
        differ = SemanticDiffer()
        items, summary = differ.diff(
            {"tls": {"enabled": False}, "server": {"port": 8080}},
            {"tls": {"enabled": True}, "server": {"port": 8080}},
            file="nginx.conf",
            constraints=BUILTIN_CONSTRAINTS,
        )
        by_key = {it.key_path: it for it in items}
        assert by_key["tls.enabled"].constraint_violations
        assert by_key["tls.enabled"].severity == Severity.CRITICAL
        assert summary.max_severity == Severity.CRITICAL


# ---------------------------------------------------------------------------
# 4. Built-in library inventory (§6.3)
# ---------------------------------------------------------------------------

class TestBuiltinLibrary:
    def test_count_and_domains(self):
        # Counts follow the authoritative §6.3 table: range 8 / enum 6 /
        # conditional_required 3 / correlation 2 / mutual_exclusion 1.
        # (The design summary line "range 7 / conditional_required 4" is
        # internally inconsistent with its own §6.3 table; the table wins.)
        assert len(BUILTIN_CONSTRAINTS) == 20
        by_type = {}
        for c in BUILTIN_CONSTRAINTS:
            by_type[c.type] = by_type.get(c.type, 0) + 1
        assert by_type == {
            "range": 8,
            "enum": 6,
            "conditional_required": 3,
            "correlation": 2,
            "mutual_exclusion": 1,
        }
        ids = [c.id for c in BUILTIN_CONSTRAINTS]
        assert len(ids) == len(set(ids))  # unique

    def test_all_builtin_constraints_validate(self):
        for c in BUILTIN_CONSTRAINTS:
            assert c.source == "builtin"
            assert c.enabled is True
            # to_dict round-trip must re-load without error
            Constraint.from_dict(c.to_dict(), source="user")


# ---------------------------------------------------------------------------
# 5. Performance micro-benchmark (10k keys, < 10 ms incremental)
# ---------------------------------------------------------------------------

class TestPerformance:
    def test_tenk_key_incremental_under_10ms(self):
        tree = {"k%06d" % i: i for i in range(10000)}
        tree["server"] = {"port": 99999}
        snapshot = {"big.json": tree}
        items = [_make_item("server.port", old=8080, new=99999, file="big.json")]
        start = time.perf_counter()
        ConstraintEngine.apply(snapshot, items, BUILTIN_CONSTRAINTS)
        elapsed_ms = (time.perf_counter() - start) * 1000
        assert items[0].constraint_violations, "expected http_port_range hit"
        assert elapsed_ms < 10.0, "constraint check took %.2f ms" % elapsed_ms
