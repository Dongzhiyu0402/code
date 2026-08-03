"""QA round-3 supplementary tests for cfgdrift v0.2.0 (pure-Python fallback).

Written independently by the QA engineer (严过关) — do NOT modify the
engineer's test files.  Verification focus for v0.2.0 (see
``docs/system_design.md`` appendix A):

  A. Backend environment & selection contract
     (HAVE_C / PARSER_BACKEND / CFGDRIFT_BACKEND / set_backend / version)
  B. Dual-mode valid-input tree equivalence (my own corpus additions:
     TOML datetime Z suffix, INI quote stripping, big integers, inf/nan,
     nested structures, hex-with-capital-E regression)
  C. Dual-mode invalid-input ValueError prefix contract
  D. Auto-degradation (simulated missing C) and forced-c error semantics
  E. Python 3.8 syntax + tomli fallback spot check (subprocess)
  F. End-to-end pure backend CLI pipeline (init -> baseline -> diff)
  G. Wheel verification (dist-pure wheel has no .pyd; pure backend works)
  H. Spec-compliance evidence for C TOML integer syntax (fixed in round-3:
     the C parser now validates sign / leading zeros / underscore placement)

The ``set_backend()`` test hook switches the active backend at runtime so the
dual-mode comparisons below exercise the real dispatch layer in
``cfgdrift.core.parser`` (not just the raw parser functions).
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
PY = sys.executable

sys.path.insert(0, SRC)

try:  # C extension is optional since v0.2.0 (pure-Python fallback exists)
    from cfgdrift import _cfgdrift  # noqa: E402
    HAVE_C = True
except ImportError:  # pragma: no cover - pure-Python installs
    HAVE_C = False

requires_c = pytest.mark.skipif(
    not HAVE_C, reason="C extension not available (cfgdrift._cfgdrift)"
)

from cfgdrift.core import parser as parser_mod  # noqa: E402
from cfgdrift.core import pure_parsers  # noqa: E402

_PY38 = r"C:/Users/20713/AppData/Local/Programs/Python/Python38/python.exe"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_py(args, env=None, cwd=None, timeout=150):
    """Run a subprocess with the current interpreter."""
    full_env = os.environ.copy()
    full_env["PYTHONPATH"] = SRC + os.pathsep + full_env.get("PYTHONPATH", "")
    if env:
        full_env.update(env)
    return subprocess.run(
        [PY] + args, capture_output=True, text=True, env=full_env, cwd=cwd or ROOT,
        timeout=timeout,
    )


def _tree_equal(a, b) -> bool:
    """Type-sensitive structural equality; dict key order ignored."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        if set(a.keys()) != set(b.keys()):
            return False
        return all(_tree_equal(a[k], b[k]) for k in a)
    if isinstance(a, list):
        if len(a) != len(b):
            return False
        return all(_tree_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, float):
        if math.isnan(a) and math.isnan(b):
            return True
        return a == b
    return a == b


def _both_backends_parse(fmt, text):
    """Return (c_tree, pure_tree) via the dispatch layer (set_backend)."""
    original = parser_mod.PARSER_BACKEND
    try:
        parser_mod.set_backend("c")
        c_tree = parser_mod.parse_text(text, fmt)
        parser_mod.set_backend("pure")
        pure_tree = parser_mod.parse_text(text, fmt)
        return c_tree, pure_tree
    finally:
        parser_mod.set_backend(original)


def _both_backends_reject(fmt, text):
    """Assert both backends raise ValueError with the shared prefix."""
    original = parser_mod.PARSER_BACKEND
    try:
        for backend in ("c", "pure"):
            parser_mod.set_backend(backend)
            with pytest.raises(ValueError) as exc:
                parser_mod.parse_text(text, fmt)
            msg = str(exc.value)
            assert msg.startswith("parse error at line "), (
                "%s %s error missing prefix: %r" % (backend, fmt, msg)
            )
            assert "column" in msg, msg
    finally:
        parser_mod.set_backend(original)


# ===========================================================================
# A. Backend environment & selection contract
# ===========================================================================


class TestBackendEnvironment:
    def test_default_backend_is_c_when_available(self):
        """auto (default) must prefer the C extension when present."""
        # Explicitly neutralise CFGDRIFT_BACKEND so the outer test env (which
        # may be CFGDRIFT_BACKEND=pure) cannot leak into the subprocess.
        p = _run_py(
            ["-c", "from cfgdrift.core import parser; "
                   "print(parser.HAVE_C, parser.PARSER_BACKEND)"],
            env={"CFGDRIFT_BACKEND": ""},
        )
        assert p.returncode == 0, p.stderr
        have_c, backend = p.stdout.strip().split()
        assert have_c == ("True" if HAVE_C else "False")
        assert backend == ("c" if HAVE_C else "pure")

    def test_force_pure_backend(self):
        p = _run_py(
            ["-c", "from cfgdrift.core import parser; "
                   "print(parser.HAVE_C, parser.PARSER_BACKEND)"],
            env={"CFGDRIFT_BACKEND": "pure"},
        )
        assert p.returncode == 0, p.stderr
        have_c, backend = p.stdout.strip().split()
        assert have_c == ("True" if HAVE_C else "False")
        assert backend == "pure"

    @requires_c
    def test_force_c_backend(self):
        p = _run_py(
            ["-c", "from cfgdrift.core import parser; "
                   "print(parser.PARSER_BACKEND)"],
            env={"CFGDRIFT_BACKEND": "c"},
        )
        assert p.returncode == 0, p.stderr
        assert p.stdout.strip() == "c"

    def test_invalid_backend_env_raises_runtime_error(self):
        p = _run_py(
            ["-c", "from cfgdrift.core import parser"],
            env={"CFGDRIFT_BACKEND": "bogus"},
        )
        assert p.returncode != 0
        assert "invalid CFGDRIFT_BACKEND" in p.stderr

    def test_version_strings(self):
        import cfgdrift

        assert cfgdrift.__version__ == "0.5.0"
        if HAVE_C:
            assert _cfgdrift.version() == "0.5.0-c"

    def test_set_backend_runtime_switch_roundtrip(self):
        original = parser_mod.PARSER_BACKEND
        try:
            assert parser_mod.set_backend("pure") == "pure"
            assert parser_mod.PARSER_BACKEND == "pure"
            assert parser_mod.parse_text('{"a": 1}', "json") == {"a": 1}
            assert parser_mod.set_backend("c") == ("c" if HAVE_C else "pure")
            assert parser_mod.PARSER_BACKEND == ("c" if HAVE_C else "pure")
            assert parser_mod.parse_text('{"a": 1}', "json") == {"a": 1}
            with pytest.raises(RuntimeError):
                parser_mod.set_backend("nonsense")
        finally:
            parser_mod.set_backend(original)

    def test_debug_logger_exists(self):
        assert parser_mod.logger.name == "cfgdrift.core.parser"
        assert parser_mod.logger.isEnabledFor(10) is False  # DEBUG off by default


# ===========================================================================
# B. Dual-mode valid-input tree equivalence (dispatch layer)
# ===========================================================================

VALID_CORPUS_V020 = [
    # --- JSON ---
    ("json", '{"n": ' + "9" * 200 + "}"),  # big integer
    ("json", '{"a": "it\'s", "nested": {"deep": [1, {"x": null}]}}'),
    ("json", "[1, 2.5, -3, true, false, null, \"s\"]"),
    # --- TOML ---
    ("toml", "a = 0xDEADBEEF\nb = 0x1e\nc = 0x2A\nd = 0x10E\ne = 0xFF\n"),  # hex E regression
    ("toml", "a = " + "9" * 200 + "\n"),  # arbitrary precision TOML int
    ("toml", "a = 1979-05-27T07:32:00Z\n"),  # datetime Z suffix
    ("toml", "a = 1979-05-27T07:32:00+07:00\n"),  # offset datetime
    ("toml", "a = inf\nb = -inf\nc = nan\nd = +nan\ne = -nan\n"),
    ("toml", "a = { x = 1, y = [1, 2, { z = \"three\" }] }\n"),
    (
        "toml",
        "[server]\nhost = \"localhost\"\nport = 8080\n\n"
        "[[products]]\nname = \"Hammer\"\n[[products]]\nname = \"Nail\"\n",
    ),
    ("toml", '"a.b" = 1\na.b = 2\n'),  # quoted dot key vs dotted key
    # --- INI ---
    ("ini", 'a = "  trimmed  "\nb = \'world\'\nempty = ""\n'),
    ("ini", "[DEFAULT]\nx = 1\n[s]\ny = 2\n"),  # DEFAULT is a regular section
    ("ini", "url = http://x?a=b\nCaseKey = kept\n"),
    ("ini", "[s]\na = 1\n[s]\nb = 2\n"),  # duplicate section -> merge
]


@pytest.mark.parametrize("fmt,text", VALID_CORPUS_V020, ids=lambda v: str(v)[:22])
def test_valid_corpus_equivalent(fmt, text):
    if not HAVE_C:
        pytest.skip("dual-mode comparison needs both backends")
    c_tree, pure_tree = _both_backends_parse(fmt, text)
    assert _tree_equal(c_tree, pure_tree), (
        "C/pure semantic trees differ for %s input:\nC:    %r\npure: %r"
        % (fmt, c_tree, pure_tree)
    )


def test_toml_hex_types_are_int_not_float():
    """v0.2.0 hex fix: 0xDEADBEEF (capital E) must be an int in BOTH backends."""
    texts = {
        "0xDEADBEEF": 0xDEADBEEF,
        "0x1e": 0x1E,
        "0x2A": 0x2A,
        "0x10E": 0x10E,
        "0xFF": 0xFF,
    }
    for lit, expected in texts.items():
        for fn in (lambda t: pure_parsers.parse_toml_pure(t),):
            got = fn("a = %s\n" % lit)["a"]
            assert type(got) is int, "%s -> %r (%s)" % (lit, got, type(got).__name__)
            assert got == expected
        if HAVE_C:
            got = _cfgdrift.parse_toml("a = %s\n" % lit)["a"]
            assert type(got) is int, "%s -> %r (%s)" % (lit, got, type(got).__name__)
            assert got == expected


def test_toml_datetime_z_suffix_literal_equivalence():
    """1979-05-27T07:32:00Z must be the literal string in both backends."""
    for backend in ("pure", "c"):
        if backend == "c" and not HAVE_C:
            continue
        original = parser_mod.PARSER_BACKEND
        try:
            parser_mod.set_backend(backend)
            tree = parser_mod.parse_text("a = 1979-05-27T07:32:00Z\n", "toml")
            assert tree["a"] == "1979-05-27T07:32:00Z", (backend, tree)
        finally:
            parser_mod.set_backend(original)


def test_ini_quote_stripping_both_backends():
    expected = {"a": "  trimmed  ", "b": "world", "empty": ""}
    for backend in ("pure", "c"):
        if backend == "c" and not HAVE_C:
            continue
        original = parser_mod.PARSER_BACKEND
        try:
            parser_mod.set_backend(backend)
            tree = parser_mod.parse_text(
                'a = "  trimmed  "\nb = \'world\'\nempty = ""\n', "ini"
            )
            assert tree == expected, (backend, tree)
        finally:
            parser_mod.set_backend(original)


# ===========================================================================
# C. Dual-mode invalid-input ValueError prefix contract
# ===========================================================================

INVALID_CORPUS_V020 = [
    ("json", "[1, 2,]"),  # trailing comma in array (shim wording)
    ("json", '{"a": 1,}'),  # trailing comma in object (shim wording)
    ("json", "{'a': 1}"),  # bare single quote in key (shim wording)
    ("json", '{"a": \'x\'}'),  # bare single quote in value (shim wording)
    ("json", '{\n"a": 1,\n}'),  # trailing comma multiline
    ("json", '{"n": 01}'),  # leading zero
    ("json", '{"n": -}'),  # bare minus
    ("json", '{"unterminated": "abc'),  # unterminated string
    ("toml", "a = 1\na = 2\n"),  # duplicate key
    ("toml", "[a]\nx = 1\n[a]\ny = 2\n"),  # duplicate table header
    ("toml", "a = "),  # missing value
    ("toml", 'a = "unterminated\n'),  # unterminated string
    ("toml", "a = 0x\n"),  # empty hex
    ("toml", "a = 0x1G\n"),  # invalid hex digit
    ("ini", "[sec\na = 1\n"),  # unterminated section header
    ("ini", "= no key\n"),  # empty key
    ("ini", "this is not an ini line\n"),  # no delimiter
]


@pytest.mark.parametrize("fmt,text", INVALID_CORPUS_V020, ids=lambda v: str(v)[:22])
def test_invalid_corpus_error_prefix(fmt, text):
    _both_backends_reject(fmt, text)


def test_json_shim_wording_matches_c():
    """The pure shims must match the C wording for trailing comma / bare quote."""
    for text, needle in [
        ("[1, 2,]", "trailing comma"),
        ('{"a": 1,}', "trailing comma"),
        ("{'a': 1}", "single quote"),
    ]:
        with pytest.raises(ValueError) as exc:
            pure_parsers.parse_json_pure(text)
        assert needle in str(exc.value), str(exc.value)
        if HAVE_C:
            with pytest.raises(ValueError) as c_exc:
                _cfgdrift.parse_json(text)
            assert needle in str(c_exc.value), str(c_exc.value)


def test_bare_single_quote_inside_string_is_legal_both():
    """A single quote inside a double-quoted JSON string must NOT be flagged."""
    for backend in ("pure", "c"):
        if backend == "c" and not HAVE_C:
            continue
        original = parser_mod.PARSER_BACKEND
        try:
            parser_mod.set_backend(backend)
            assert parser_mod.parse_text('{"a": "it\'s"}', "json") == {"a": "it's"}
        finally:
            parser_mod.set_backend(original)


# ===========================================================================
# D. Auto-degradation and forced-c error semantics
# ===========================================================================

_BLOCKER_SNIPPET = (
    "import sys, importlib.abc\n"
    "class _Blocker(importlib.abc.MetaPathFinder):\n"
    "    def find_spec(self, fullname, path=None, target=None):\n"
    "        if fullname == 'cfgdrift._cfgdrift':\n"
    "            raise ImportError('blocked for QA test')\n"
    "        return None\n"
    "sys.meta_path.insert(0, _Blocker())\n"
)


def test_auto_degrades_when_c_missing():
    """Blocking _cfgdrift import must silently degrade to pure, 100% usable."""
    code = (
        _BLOCKER_SNIPPET
        + "from cfgdrift.core import parser\n"
        + "print('HAVE_C', parser.HAVE_C, 'BACKEND', parser.PARSER_BACKEND)\n"
        + "print(parser.parse_text('{\"a\": 1}', 'json'))\n"
        + "print(parser.parse_text('a = 1979-05-27T07:32:00Z\\n', 'toml'))\n"
        + "print(parser.parse_text('a = \"hello\"\\n', 'ini'))\n"
    )
    p = _run_py(["-c", code])
    assert p.returncode == 0, p.stderr
    assert "HAVE_C False BACKEND pure" in p.stdout, p.stdout
    assert "{'a': 1}" in p.stdout
    assert "1979-05-27T07:32:00Z" in p.stdout
    assert "{'a': 'hello'}" in p.stdout


def test_forced_c_without_c_raises_at_import():
    code = (
        _BLOCKER_SNIPPET
        + "from cfgdrift.core import parser\n"
        + "print('unreachable')\n"
    )
    p = _run_py(["-c", code], env={"CFGDRIFT_BACKEND": "c"})
    assert p.returncode != 0
    assert "RuntimeError" in p.stderr
    assert "CFGDRIFT_BACKEND" in p.stderr


def test_set_backend_c_without_c_raises(monkeypatch):
    monkeypatch.setattr(parser_mod, "HAVE_C", False)
    with pytest.raises(RuntimeError, match="CFGDRIFT_BACKEND"):
        parser_mod.set_backend("c")
    with pytest.raises(RuntimeError):
        parser_mod._resolve_backend("c")


# ===========================================================================
# E. Python 3.8 syntax + tomli fallback spot check (subprocess)
# ===========================================================================


def _py38_available():
    return os.path.exists(_PY38)


requires_py38 = pytest.mark.skipif(
    not _py38_available(), reason="system Python 3.8 not found at %s" % _PY38
)


@requires_py38
def test_py38_syntax_compile_all_sources(tmp_path):
    """All package sources must be syntactically valid on Python 3.8.

    PYTHONPYCACHEPREFIX redirects the .pyc writes into an isolated scratch
    directory (3.8+), so concurrent pytest processes cannot race on the
    shared ``__pycache__`` under src/ (Windows file locks).
    """
    sources = [
        "src/cfgdrift/__init__.py",
        "src/cfgdrift/cli.py",
        "src/cfgdrift/core/__init__.py",
        "src/cfgdrift/core/parser.py",
        "src/cfgdrift/core/pure_parsers.py",
        "src/cfgdrift/core/model.py",
        "src/cfgdrift/core/differ.py",
        "src/cfgdrift/core/reporter.py",
        "src/cfgdrift/storage/store.py",
        "src/cfgdrift/scanner/scanner.py",
        "src/cfgdrift/rules/ignore.py",
        "src/cfgdrift/web/app.py",
    ]
    scratch = os.path.join(ROOT, "_qa_py38cache_%d" % os.getpid())
    os.makedirs(scratch, exist_ok=True)
    try:
        env = os.environ.copy()
        env["PYTHONPYCACHEPREFIX"] = scratch
        p = subprocess.run(
            [_PY38, "-m", "py_compile"] + [os.path.join(ROOT, s) for s in sources],
            capture_output=True,
            text=True,
            cwd=ROOT,
            env=env,
            timeout=120,
        )
        assert p.returncode == 0, p.stderr
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


@requires_py38
def test_py38_tomli_fallback_import_and_error_wrapping(tmp_path):
    """On 3.8 tomllib is absent -> the tomli fallback branch must engage and
    TOMLDecodeError must be wrapped with the shared prefix."""
    scratch = os.path.join(ROOT, "_qa_py38_%d" % os.getpid())
    os.makedirs(os.path.join(scratch, "tomli"), exist_ok=True)
    try:
        stub = os.path.join(scratch, "tomli", "__init__.py")
        with open(stub, "w", encoding="utf-8") as fh:
            fh.write(
                "class TOMLDecodeError(ValueError):\n"
                "    def __init__(self, msg, doc, pos, lineno=None, colno=None):\n"
                "        self.msg = msg; self.doc = doc; self.pos = pos\n"
                "        self.lineno = lineno; self.colno = colno\n"
                "        super().__init__(msg)\n"
                "def loads(text):\n"
                "    if text.strip() == 'bad =':\n"
                "        raise TOMLDecodeError('Expected value after =', text, 6,"
                " lineno=1, colno=7)\n"
                "    if text.strip() == 'a = 1979-05-27T07:32:00Z':\n"
                "        from datetime import datetime, timezone\n"
                "        return {'a': datetime(1979, 5, 27, 7, 32, 0,"
                " tzinfo=timezone.utc)}\n"
                "    raise TOMLDecodeError('unexpected', text, 0, lineno=1, colno=1)\n"
            )
        runner = os.path.join(scratch, "run.py")
        with open(runner, "w", encoding="utf-8") as fh:
            fh.write(
                "import sys\n"
                "sys.path.insert(0, %r)\n" % scratch
                + "sys.path.insert(0, %r)\n" % SRC
                + "try:\n"
                "    import tomllib\n"
                "    print('have_tomllib True')\n"
                "except ModuleNotFoundError:\n"
                "    print('have_tomllib False')\n"
                "from cfgdrift.core import pure_parsers\n"
                "print('backend', pure_parsers.tomllib.__name__)\n"
                "try:\n"
                "    pure_parsers.parse_toml_pure('bad =')\n"
                "except ValueError as exc:\n"
                "    print('err', str(exc))\n"
                "    assert str(exc).startswith('parse error at line 1, column 7:'),"
                " str(exc)\n"
                "r = pure_parsers.parse_toml_pure('a = 1979-05-27T07:32:00Z')\n"
                "print('dt', r['a'])\n"
                "assert r['a'] == '1979-05-27T07:32:00Z', r['a']\n"
                "print('PY38_OK')\n"
            )
        p = subprocess.run(
            [_PY38, runner], capture_output=True, text=True, cwd=ROOT, timeout=120
        )
        assert p.returncode == 0, p.stderr
        assert "have_tomllib False" in p.stdout, p.stdout
        assert "backend tomli" in p.stdout, p.stdout
        assert "err parse error at line 1, column 7:" in p.stdout, p.stdout
        assert "dt 1979-05-27T07:32:00Z" in p.stdout, p.stdout
        assert "PY38_OK" in p.stdout, p.stdout
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


# ===========================================================================
# F. End-to-end pure backend CLI pipeline
# ===========================================================================


def test_pure_backend_cli_pipeline_end_to_end(tmp_path):
    """init -> scan --save-as-baseline prod -> modify -> diff(1) -> revert -> diff(0)."""
    home = tmp_path / "home"
    home.mkdir()
    conf = tmp_path / "conf"
    conf.mkdir()
    app = conf / "app.json"
    app.write_text(
        json.dumps({"server": {"host": "localhost", "port": 8080}}),
        encoding="utf-8",
    )
    env = {"CFGDRIFT_HOME": str(home), "CFGDRIFT_BACKEND": "pure"}
    assert _run_py(["-m", "cfgdrift.cli", "init"], env=env).returncode == 0
    p = _run_py(
        ["-m", "cfgdrift.cli", "scan", str(conf), "--save-as-baseline", "prod"],
        env=env,
    )
    assert p.returncode == 0, p.stdout + p.stderr

    app.write_text(
        json.dumps({"server": {"host": "changed", "port": 8080}}),
        encoding="utf-8",
    )
    p = _run_py(["-m", "cfgdrift.cli", "diff", str(conf), "--baseline", "prod"], env=env)
    assert p.returncode == 1, p.stdout + p.stderr
    assert "server.host" in p.stdout

    app.write_text(
        json.dumps({"server": {"host": "localhost", "port": 8080}}),
        encoding="utf-8",
    )
    p = _run_py(["-m", "cfgdrift.cli", "diff", str(conf), "--baseline", "prod"], env=env)
    assert p.returncode == 0, p.stdout + p.stderr


# ===========================================================================
# G. Wheel verification
# ===========================================================================

_WHEEL = os.path.join(ROOT, "dist-pure", "cfgdrift-0.2.0-py3-none-any.whl")


def test_pure_wheel_no_c_extension_and_works(tmp_path):
    if not os.path.exists(_WHEEL):
        pytest.skip("pure wheel not built: %s" % _WHEEL)
    target = os.path.join(ROOT, "_qa_wheel_%d" % os.getpid())
    if os.path.exists(target):
        shutil.rmtree(target, ignore_errors=True)
    os.makedirs(target, exist_ok=True)
    try:
        p = subprocess.run(
            [PY, "-m", "pip", "install", "--no-deps", "--target", target, _WHEEL],
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert p.returncode == 0, p.stdout + p.stderr

        # 1) no compiled extension in the wheel payload
        compiled = []
        for dirpath, _, filenames in os.walk(target):
            for fn in filenames:
                if fn.endswith((".pyd", ".so", ".dll")):
                    compiled.append(os.path.join(dirpath, fn))
        assert compiled == [], "compiled artifacts in pure wheel: %r" % compiled

        # 2) importing from the installed wheel sees pure backend only
        code = (
            "from cfgdrift.core import parser\n"
            "print('HAVE_C', parser.HAVE_C, 'BACKEND', parser.PARSER_BACKEND)\n"
            "print(parser.parse_text('{\"a\": 1}', 'json'))\n"
            "print(parser.parse_text('a = 1979-05-27T07:32:00Z\\n', 'toml'))\n"
            "print(parser.parse_text('a = \"hello\"\\n', 'ini'))\n"
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = target
        p2 = subprocess.run(
            [PY, "-c", code], capture_output=True, text=True, env=env, timeout=60
        )
        assert p2.returncode == 0, p2.stderr
        assert "HAVE_C False BACKEND pure" in p2.stdout, p2.stdout
        assert "{'a': 1}" in p2.stdout
        assert "1979-05-27T07:32:00Z" in p2.stdout
        assert "{'a': 'hello'}" in p2.stdout
    finally:
        shutil.rmtree(target, ignore_errors=True)


# ===========================================================================
# H. Spec-compliance evidence (fixed in v0.2.0 round-3)
#
# The C TOML parser previously over-accepted a few literals that violate the
# TOML v1.0 grammar and that the pure backend (tomllib/tomli, spec-compliant)
# rejects.  These were recorded as strict xfail during round-3 QA; the C
# parser now performs TOML v1.0 integer syntax validation (sign / leading
# zeros / underscore placement) so the cases below must reject like the pure
# backend.  See docs/system_design.md appendix A.
# ===========================================================================

_TOML_OVER_ACCEPTED = [
    ("-0x1F", "signed hex integer (sign not allowed on non-decimal)"),
    ("+0x2A", "signed hex integer (sign not allowed on non-decimal)"),
    ("-0b101", "signed binary integer (sign not allowed on non-decimal)"),
    ("-0o17", "signed octal integer (sign not allowed on non-decimal)"),
    ("07", "decimal leading zero"),
    ("+017", "decimal leading zero with sign"),
    ("007", "decimal leading zero (multi-digit)"),
    ("0x_1", "underscore immediately after base prefix"),
    ("1__0", "consecutive underscores"),
    ("1_", "trailing underscore"),
]


@requires_c
@pytest.mark.parametrize("literal,why", _TOML_OVER_ACCEPTED, ids=lambda v: str(v)[:16])
def test_evidence_c_rejects_spec_invalid_toml(literal, why):
    """TOML v1.0 forbids these; the C backend must reject like the pure one."""
    with pytest.raises(ValueError, match="parse error at line"):
        _cfgdrift.parse_toml("a = %s\n" % literal)


@requires_py38
def test_py38_pure_rejects_spec_invalid_toml():
    """Sanity: the pure backend (tomllib/tomli) rejects these on every version."""
    for literal, _ in _TOML_OVER_ACCEPTED:
        with pytest.raises(ValueError, match="parse error at line"):
            pure_parsers.parse_toml_pure("a = %s\n" % literal)
