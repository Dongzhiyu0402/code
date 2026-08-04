"""Tests for the mydsl-parser example cfgdrift plugin.

Run from the project root::

    C:/Users/20713/.workbuddy/binaries/python/versions/3.13.12/python.exe -m pytest examples/mydsl-parser/tests/ -q

Covers:

1. Parsing correctness: nested blocks, named-block groups, block arrays,
   scalar coercion, comments, duplicate-key behavior, error handling.
2. Line-map correctness: ``{key_path: 1-based line}`` following cfgdrift's
   ``join_path`` convention (``.`` segments, ``[i]`` indices).
3. cfgdrift integration: ``parse_text`` / ``parse_text_lines`` dispatch and
   diff line-number attachment through the plugin registry.
4. Both registration paths: the import-time decorator and the entry-point
   value shapes documented in the README.
"""

from __future__ import annotations

import os
import sys

import pytest

# Make both cfgdrift (repo src) and this package importable regardless of
# whether mydsl-parser has been pip-installed.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
sys.path.insert(0, os.path.join(_ROOT, "src"))
sys.path.insert(0, os.path.abspath(os.path.join(_HERE, "..")))

from cfgdrift.core import parser as parser_mod  # noqa: E402
from cfgdrift.core import plugins as plugins_mod  # noqa: E402
from cfgdrift.core.differ import SemanticDiffer  # noqa: E402
from cfgdrift.core.plugins import ParserPlugin  # noqa: E402

# Importing the package runs the register_plugin decorator (import-time
# registration side effect), so the shared registry sees ``mydsl`` for the
# whole session.
import mydsl_parser  # noqa: E402
from mydsl_parser.plugin import build_line_map, parse  # noqa: E402

DEMO = os.path.abspath(os.path.join(_ROOT, "examples", "demo", "nginx-like.dsl"))


@pytest.fixture(scope="session", autouse=True)
def _session_cleanup():
    """Leave the shared registry as we found it after the whole session."""
    yield
    parser_mod._PLUGIN_REGISTRY._plugins.pop("mydsl", None)
    parser_mod._PLUGIN_REGISTRY._ext_index.pop(".dsl", None)


@pytest.fixture()
def mydsl_registered():
    """Register the shipped plugin instance (idempotent, replace=True)."""
    parser_mod._PLUGIN_REGISTRY.register(mydsl_parser.plugin, replace=True)
    yield mydsl_parser.plugin


# ---------------------------------------------------------------------------
# Parsing correctness
# ---------------------------------------------------------------------------


def test_parse_demo_file():
    with open(DEMO, encoding="utf-8") as fh:
        text = fh.read()
    tree = parse(text)
    assert tree == {
        "server": [
            {
                "listen": 8080,
                "server_name": ["example.com", "www.example.com"],
                "root": "/var/www/example",
                "location": {
                    "/api": {
                        "proxy_pass": "http://backend:9000",
                        "proxy_read_timeout": "30s",
                    },
                    "/static": {"alias": "/var/www/static"},
                },
            },
            {"listen": 9090, "server_name": "admin.example.com"},
        ]
    }


def test_scalar_coercion():
    tree = parse(
        "port 8080;\n"
        "ratio 1.5;\n"
        'name "my server";\n'
        "mode on;\n"
        "empty;\n"
    )
    assert tree == {
        "port": 8080,
        "ratio": 1.5,
        "name": "my server",
        "mode": "on",
        "empty": None,
    }


def test_comments_ignored():
    tree = parse(
        "# top comment\n"
        "server { # inline comment\n"
        "    listen 80; # trailing comment\n"
        "}\n"
    )
    assert tree == {"server": [{"listen": 80}]}


def test_block_arrays_and_named_block_groups():
    tree = parse(
        "server {\n"
        "    listen 80;\n"
        "}\n"
        "server {\n"
        "    listen 81;\n"
        "}\n"
        "location /a {\n"
        "    root /x;\n"
        "}\n"
        "location /b {\n"
        "    root /y;\n"
        "}\n"
    )
    assert tree == {
        "server": [{"listen": 80}, {"listen": 81}],
        "location": {"/a": {"root": "/x"}, "/b": {"root": "/y"}},
    }


def test_duplicate_named_block_becomes_array():
    tree = parse(
        "location /a {\n"
        "    root /x;\n"
        "}\n"
        "location /a {\n"
        "    root /y;\n"
        "}\n"
    )
    assert tree == {"location": {"/a": [{"root": "/x"}, {"root": "/y"}]}}


def test_duplicate_scalar_last_wins():
    tree = parse("port 80;\nport 81;\n")
    assert tree == {"port": 81}


def test_unbalanced_braces_raise_with_line():
    with pytest.raises(ValueError) as exc:
        parse("server {\n    listen 80;\n")
    assert "line 1" in str(exc.value)

    with pytest.raises(ValueError) as exc:
        parse("}\n")
    assert "line 1" in str(exc.value)


def test_malformed_statement_raises():
    with pytest.raises(ValueError) as exc:
        parse("listen { 80;\n")
    assert "line 1" in str(exc.value)


# ---------------------------------------------------------------------------
# Line-map correctness
# ---------------------------------------------------------------------------


def test_build_line_map_demo_file():
    with open(DEMO, encoding="utf-8") as fh:
        text = fh.read()
    line_map = build_line_map(text)
    assert line_map == {
        "server[0]": 3,
        "server[0].listen": 4,
        "server[0].server_name": 5,
        "server[0].server_name[0]": 5,
        "server[0].server_name[1]": 5,
        "server[0].root": 6,
        "server[0].location": 8,
        "server[0].location./api": 8,
        "server[0].location./api.proxy_pass": 9,
        "server[0].location./api.proxy_read_timeout": 10,
        "server[0].location./static": 13,
        "server[0].location./static.alias": 14,
        "server[1]": 18,
        "server[1].listen": 19,
        "server[1].server_name": 20,
    }


def test_line_map_array_indices_use_join_path():
    line_map = build_line_map(
        "server {\n    server_name a.com b.com;\n}\n"
    )
    assert line_map == {
        "server[0]": 1,
        "server[0].server_name": 2,
        "server[0].server_name[0]": 2,
        "server[0].server_name[1]": 2,
    }


def test_line_map_matches_join_path_convention():
    from cfgdrift.core.model import join_path

    line_map = build_line_map(
        "server {\n    server_name a.com b.com;\n}\n"
    )
    # The keys must be exactly what join_path produces for the same parts.
    assert "server[0].server_name[1]" == join_path(
        [("key", "server"), ("index", 0), ("key", "server_name"), ("index", 1)]
    )
    assert line_map["server[0].server_name[1]"] == 2


# ---------------------------------------------------------------------------
# cfgdrift integration (registry dispatch + diff line numbers)
# ---------------------------------------------------------------------------


def test_parse_text_dispatch(mydsl_registered):
    tree = parser_mod.parse_text("server {\n    listen 8080;\n}\n", "mydsl")
    assert tree == {"server": [{"listen": 8080}]}


def test_parse_text_lines_via_plugin(mydsl_registered):
    text = "server {\n    listen 8080;\n    server_name a.com b.com;\n}\n"
    tree, line_map = parser_mod.parse_text_lines(text, "mydsl")
    assert tree == {
        "server": [{"listen": 8080, "server_name": ["a.com", "b.com"]}]
    }
    assert line_map["server[0].listen"] == 2
    assert line_map["server[0].server_name[1]"] == 3


def test_detect_format_via_plugin_extension(mydsl_registered):
    assert parser_mod.detect_format("nginx-like.dsl") == "mydsl"


def test_diff_reports_plugin_line_numbers(mydsl_registered):
    with open(DEMO, encoding="utf-8") as fh:
        old_text = fh.read()
    new_text = old_text.replace("listen 8080;", "listen 8081;")

    old_tree, old_lm = parser_mod.parse_text_lines(old_text, "mydsl")
    new_tree, new_lm = parser_mod.parse_text_lines(new_text, "mydsl")

    items, summary = SemanticDiffer().diff(
        old_tree,
        new_tree,
        file="nginx-like.dsl",
        old_lines={"nginx-like.dsl": old_lm},
        new_lines={"nginx-like.dsl": new_lm},
    )
    changed = [i for i in items if i.key_path == "server[0].listen"]
    assert len(changed) == 1
    assert changed[0].change_type.value == "modified"
    assert changed[0].old_value == 8080
    assert changed[0].new_value == 8081
    # Real 1-based line in examples/demo/nginx-like.dsl.
    assert changed[0].line == 4


# ---------------------------------------------------------------------------
# Registration paths
# ---------------------------------------------------------------------------


def test_decorator_registration_on_import():
    # The module-level register_plugin decorator ran when the package was
    # first imported, so --format mydsl works without an entry point.
    assert parser_mod._PLUGIN_REGISTRY.get("mydsl") is not None
    tree = parser_mod.parse_text("server {\n    listen 80;\n}\n", "mydsl")
    assert tree == {"server": [{"listen": 80}]}


def test_entry_point_value_shapes():
    # The README documents the entry-point value shapes accepted by
    # _coerce_entry_point; verify the shipped instance and the others.
    p = plugins_mod._coerce_entry_point(mydsl_parser.plugin, "mydsl")
    assert isinstance(p, ParserPlugin)
    assert p.name == "mydsl"
    assert p.extensions == (".dsl",)
    assert p.parse("server {\n    listen 1;\n}\n") == {"server": [{"listen": 1}]}

    def parse_fn(text):
        return {"from": "fn"}

    # (parse_fn, {"extensions": [...], "line_map": fn}) tuple
    p2 = plugins_mod._coerce_entry_point(
        (parse_fn, {"extensions": [".t1"], "line_map": lambda t: {"a": 1}}),
        "ep2",
    )
    assert p2.name == "ep2"
    assert p2.extensions == (".t1",)
    assert p2.parse("x") == {"from": "fn"}
    assert p2.build_line_map("x") == {"a": 1}

    # plain callable (name = entry point name)
    p3 = plugins_mod._coerce_entry_point(parse_fn, "ep3")
    assert p3.name == "ep3"
    assert p3.parse("x") == {"from": "fn"}

    # {"parse": fn, "extensions": [...], "line_map": fn} mapping
    p4 = plugins_mod._coerce_entry_point(
        {"parse": parse_fn, "extensions": [".t4"], "line_map": lambda t: {}},
        "ep4",
    )
    assert p4.name == "ep4"
    assert p4.extensions == (".t4",)
