"""Engineer unit tests for cfgdrift v0.5.0 — plugin parser interface (T02).

Covers the ``cfgdrift.core.plugins`` protocol:

1. ``ParserPlugin`` + ``register_plugin`` decorator (bare / with args).
2. ``PluginRegistry`` lookup: get / by_extension / names / custom_names.
3. Parser dispatch: ``--format <plugin>`` through ``parse_text`` /
   ``parse_text_lines`` (optional line map, D10).
4. ``detect_format`` resolves plugin extensions after built-ins.
5. ``validate_format`` error includes registration guidance (D8).
6. Entry point discovery: success / per-plugin failure isolation /
   entry-point-over-decorator precedence (Q5).
7. Built-in four formats remain unchanged.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cfgdrift.core import parser as parser_mod  # noqa: E402
from cfgdrift.core import plugins as plugins_mod  # noqa: E402


# ---------------------------------------------------------------------------
# ParserPlugin / register_plugin
# ---------------------------------------------------------------------------

class TestParserPlugin:
    def test_requires_name_and_parse(self):
        with pytest.raises(ValueError):
            plugins_mod.ParserPlugin(name="", parse=lambda t: {})
        with pytest.raises(ValueError):
            plugins_mod.ParserPlugin(name="x", parse=None)

    def test_extensions_normalized_lowercase(self):
        p = plugins_mod.ParserPlugin(
            name="DSL", extensions=(".DSL", ".Dsl"), parse=lambda t: {}
        )
        assert p.extensions == (".dsl", ".dsl")

    def test_build_line_map_default_empty(self):
        p = plugins_mod.ParserPlugin(name="x", parse=lambda t: {})
        assert p.build_line_map("anything") == {}

    def test_build_line_map_broken_falls_back_empty(self):
        def boom(text):
            raise RuntimeError("nope")

        p = plugins_mod.ParserPlugin(name="x", parse=lambda t: {}, build_line_map=boom)
        assert p.build_line_map("a") == {}


class TestRegisterDecorator:
    def test_bare_decorator(self):
        reg = plugins_mod.PluginRegistry()

        @plugins_mod.register_plugin(registry=reg)
        def bare_parser(text):
            return {"bare": text}

        assert reg.get("bare_parser") is not None
        assert reg.get("bare_parser").parse("hi") == {"bare": "hi"}

    def test_decorator_with_args_and_line_map(self):
        reg = plugins_mod.PluginRegistry()

        def line_map(text):
            return {"a": 1}

        @plugins_mod.register_plugin(
            "kvdsl", extensions=(".kv",), line_map=line_map, registry=reg
        )
        def parse_kv(text):
            out = {}
            for line in text.splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip()
            return out

        plugin = reg.get("kvdsl")
        assert plugin is not None
        assert plugin.extensions == (".kv",)
        assert plugin.parse("a=1") == {"a": "1"}
        assert plugin.build_line_map("a=1") == {"a": 1}

    def test_duplicate_registration_raises(self):
        reg = plugins_mod.PluginRegistry()
        reg.register(plugins_mod.ParserPlugin(name="dup", parse=lambda t: {}))
        with pytest.raises(ValueError):
            reg.register(plugins_mod.ParserPlugin(name="dup", parse=lambda t: {}))

    def test_register_replace_allowed(self):
        reg = plugins_mod.PluginRegistry()
        reg.register(plugins_mod.ParserPlugin(name="dup", parse=lambda t: {"a": 1}))
        reg.register(
            plugins_mod.ParserPlugin(name="dup", parse=lambda t: {"b": 2}),
            replace=True,
        )
        assert reg.get("dup").parse("x") == {"b": 2}


# ---------------------------------------------------------------------------
# Registry lookups
# ---------------------------------------------------------------------------

class TestRegistryLookups:
    def test_by_extension_and_names(self):
        reg = plugins_mod.PluginRegistry()
        reg.register(
            plugins_mod.ParserPlugin(
                name="myfmt", extensions=(".m1", ".m2"), parse=lambda t: {}
            )
        )
        assert reg.by_extension(".m1") == "myfmt"
        assert reg.by_extension(".M1") == "myfmt"  # normalized
        assert reg.by_extension(".unknown") is None
        assert "myfmt" in reg.names()

    def test_custom_names_excludes_builtins(self):
        reg = plugins_mod.PluginRegistry()
        reg.register(
            plugins_mod.ParserPlugin(name="json", parse=lambda t: {}),
            replace=True,
        )
        reg.register(
            plugins_mod.ParserPlugin(name="custom1", parse=lambda t: {})
        )
        assert reg.custom_names() == ["custom1"]


# ---------------------------------------------------------------------------
# Parser dispatch through parser.parse_text / parse_text_lines
# ---------------------------------------------------------------------------

class TestParserDispatch:
    def test_plugin_parse_via_parse_text(self):
        reg = plugins_mod.PluginRegistry()

        @plugins_mod.register_plugin("kvdsl", registry=reg)
        def parse_kv(text):
            out = {}
            for line in text.splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip()
            return out

        parser_mod._PLUGIN_REGISTRY.register(
            reg.get("kvdsl"), replace=True
        )
        try:
            tree = parser_mod.parse_text("a=1\nb=hello", "kvdsl")
            assert tree == {"a": "1", "b": "hello"}
        finally:
            # Restore the shared registry to a clean state for other tests.
            parser_mod._register_builtin_plugins()
            parser_mod._PLUGIN_REGISTRY._plugins.pop("kvdsl", None)

    def test_plugin_line_map_when_provided(self):
        reg = plugins_mod.PluginRegistry()

        def line_map(text):
            return {
                line.split("=")[0].strip(): i + 1
                for i, line in enumerate(text.splitlines())
                if "=" in line
            }

        @plugins_mod.register_plugin("lmdsl", line_map=line_map, registry=reg)
        def parse_lm(text):
            return {
                line.split("=")[0].strip(): line.split("=", 1)[1].strip()
                for line in text.splitlines()
                if "=" in line
            }

        parser_mod._PLUGIN_REGISTRY.register(reg.get("lmdsl"), replace=True)
        try:
            tree, line_map = parser_mod.parse_text_lines("a=1\nb=2\nc=3", "lmdsl")
            assert tree == {"a": "1", "b": "2", "c": "3"}
            assert line_map == {"a": 1, "b": 2, "c": 3}
        finally:
            parser_mod._register_builtin_plugins()
            parser_mod._PLUGIN_REGISTRY._plugins.pop("lmdsl", None)

    def test_plugin_line_map_missing_returns_empty(self):
        # A plugin without build_line_map -> {} (D10).
        reg = plugins_mod.PluginRegistry()

        @plugins_mod.register_plugin("nolm", registry=reg)
        def parse_nolm(text):
            return {"x": 1}

        parser_mod._PLUGIN_REGISTRY.register(reg.get("nolm"), replace=True)
        try:
            _, line_map = parser_mod.parse_text_lines("x", "nolm")
            assert line_map == {}
        finally:
            parser_mod._register_builtin_plugins()
            parser_mod._PLUGIN_REGISTRY._plugins.pop("nolm", None)

    def test_plugin_top_level_scalar_wrapped(self):
        reg = plugins_mod.PluginRegistry()

        @plugins_mod.register_plugin("scalar", registry=reg)
        def parse_scalar(text):
            return int(text.strip())

        parser_mod._PLUGIN_REGISTRY.register(reg.get("scalar"), replace=True)
        try:
            tree = parser_mod.parse_text("42", "scalar")
            assert tree == {"$": 42}
        finally:
            parser_mod._register_builtin_plugins()
            parser_mod._PLUGIN_REGISTRY._plugins.pop("scalar", None)

    def test_detect_format_plugin_extension(self):
        reg = plugins_mod.PluginRegistry()
        reg.register(
            plugins_mod.ParserPlugin(name="pd", extensions=(".pd",), parse=lambda t: {})
        )
        parser_mod._PLUGIN_REGISTRY.register(reg.get("pd"), replace=True)
        try:
            assert parser_mod.detect_format("a.pd") == "pd"
            # Built-in extensions still win over plugins.
            assert parser_mod.detect_format("a.json") == "json"
        finally:
            parser_mod._register_builtin_plugins()
            parser_mod._PLUGIN_REGISTRY._plugins.pop("pd", None)

    def test_unregistered_format_error_has_guidance(self):
        with pytest.raises(ValueError) as exc_info:
            parser_mod.validate_format("not_registered")
        message = str(exc_info.value)
        assert "invalid format" in message
        assert "expected one of" in message
        assert "cfgdrift.parsers" in message
        assert "register_plugin" in message

    def test_builtin_formats_unchanged(self):
        import json as _json

        text = _json.dumps({"server": {"port": 8080}})
        tree = parser_mod.parse_text(text, "json")
        assert tree == {"server": {"port": 8080}}
        tree2, line_map = parser_mod.parse_text_lines(text, "json")
        assert tree2 == tree
        assert "server.port" in line_map


# ---------------------------------------------------------------------------
# Entry point discovery
# ---------------------------------------------------------------------------

class _FakeEntryPoints(dict):
    """dict-like entry point collection with .select (3.10 style)."""

    def select(self, group=None):
        return list(self.get(group, []))


class _FakeEP:
    def __init__(self, name, value):
        self.name = name
        self._value = value

    def load(self):
        return self._value


class _FailingEP:
    """An entry point whose load() raises (per-plugin failure isolation)."""

    def __init__(self, name):
        self.name = name

    def load(self):
        raise RuntimeError("boom")


class TestEntryPoints:
    def test_load_success_and_failure_isolation(self, monkeypatch):
        def good_parse(text):
            return {"from_ep": text}

        fake = _FakeEntryPoints()
        fake["cfgdrift.parsers"] = [
            _FakeEP("epdsl", (good_parse, {"extensions": [".ep"]})),
            _FailingEP("epbad"),
        ]
        monkeypatch.setattr("importlib.metadata.entry_points", lambda: fake)

        reg = plugins_mod.PluginRegistry()
        loaded = reg.load_entry_points("cfgdrift.parsers")
        assert loaded == 1  # only the good plugin loaded
        assert reg.get("epdsl") is not None
        assert reg.get("epbad") is None
        assert reg.by_extension(".ep") == "epdsl"
        assert reg.get("epdsl").parse("x") == {"from_ep": "x"}

    def test_entry_point_overrides_decorator(self, monkeypatch):
        reg = plugins_mod.PluginRegistry()

        @plugins_mod.register_plugin("wins", registry=reg)
        def parse_decorator(text):
            return {"source": "decorator"}

        def parse_entry(text):
            return {"source": "entry_point"}

        fake = _FakeEntryPoints()
        fake["cfgdrift.parsers"] = [_FakeEP("wins", parse_entry)]
        monkeypatch.setattr("importlib.metadata.entry_points", lambda: fake)

        reg.load_entry_points("cfgdrift.parsers")
        assert reg.get("wins").parse("x") == {"source": "entry_point"}

    def test_parser_module_discovery_is_best_effort(self, monkeypatch):
        # A globally broken entry_points() must not break parser import/use.
        def broken():
            raise RuntimeError("metadata unavailable")

        monkeypatch.setattr("importlib.metadata.entry_points", broken)
        reg = plugins_mod.PluginRegistry()
        assert reg.load_entry_points("cfgdrift.parsers") == 0
        # Built-ins still parse.
        assert parser_mod.parse_text("{}", "json") == {}
