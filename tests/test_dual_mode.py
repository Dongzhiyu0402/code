"""Dual-mode consistency tests (v0.2.0).

Runs the same corpus through both parsing backends — the C extension
(``cfgdrift._cfgdrift``) and the pure-Python parsers
(``cfgdrift.core.pure_parsers``) — and asserts:

- legal input -> strictly equivalent semantic trees (type-sensitive; dict key
  order is ignored, list order counts);
- illegal input -> both backends raise ``ValueError`` whose message starts
  with ``"parse error at line L, column C"`` (1-based L/C; the exact L/C and
  the text after the colon may differ — exemptions D1-D4, see
  ``docs/system_design.md``).

Exemptions exercised here:

- D1 — stdlib JSON error wording for non-shim errors may differ from C.
- D2 — unpaired surrogate escapes are accepted by the pure backend (stdlib
  ``json.loads``) while the C backend rejects them; not aligned (P2 optional).
- D3 — TOML error wording / exact L/C may differ (tomli vs C).
- D4 — INI error wording / exact L/C may differ (configparser vs C).

Known, documented differences NOT in the corpus:

- naive TOML datetimes with fractional seconds render as
  ``"1979-05-27T07:32:00.5"`` (C, literal token) vs
  ``"1979-05-27T07:32:00.500000"`` (pure, ``isoformat()``) — the harness
  compares tz-aware datetimes by instant but falls back to exact string
  comparison for naive ones, so those inputs are excluded (see README);
- ``07:32:00Z`` (local time with a UTC offset) is invalid per the TOML v1.0
  grammar (offsets are only allowed on full datetimes): the pure backend
  rejects it while the C backend accepts it as a literal token;
- INI section headers with trailing content (``[s] junk``) and section names
  with surrounding spaces (``[ s ]``) are normalized differently by
  configparser vs the C parser.

The C side is skipped automatically when the extension is not available.
"""

from __future__ import annotations

import calendar
import json
import math
import os
import re
import subprocess
import sys
from datetime import datetime

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

try:
    from cfgdrift import _cfgdrift
    HAVE_C = True
except ImportError:  # pragma: no cover - pure-Python installs
    HAVE_C = False

from cfgdrift.core import parser as parser_mod  # noqa: E402
from cfgdrift.core import pure_parsers  # noqa: E402

requires_c = pytest.mark.skipif(
    not HAVE_C, reason="C extension not available (cfgdrift._cfgdrift)"
)


# ---------------------------------------------------------------------------
# Backend dispatch
# ---------------------------------------------------------------------------

_C_PARSERS = {
    "json": lambda t: _cfgdrift.parse_json(t),
    "toml": lambda t: _cfgdrift.parse_toml(t),
    "ini": lambda t: _cfgdrift.parse_ini(t),
}

_PURE_PARSERS = {
    "json": pure_parsers.parse_json_pure,
    "toml": pure_parsers.parse_toml_pure,
    "ini": pure_parsers.parse_ini_pure,
}


# ---------------------------------------------------------------------------
# Semantic tree equivalence (type-sensitive, key order ignored)
# ---------------------------------------------------------------------------

_DT_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[Tt ](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?"
    r"(Z|[+-]\d{2}:\d{2})?$"
)
_TIME_RE = re.compile(
    r"^(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})?$"
)


def _utc_us(text: str):
    """UTC microsecond key for a tz-aware ISO datetime/time literal.

    Returns ``None`` when the string is not a tz-aware datetime/time (naive
    datetimes, dates, or non-parseable text fall back to exact comparison).
    """
    m = _DT_RE.match(text)
    if m:
        y, mo, d, h, mi, s, frac, tz = m.groups()
        frac_us = int(((frac or "") + "000000")[:6])
        base_us = (
            calendar.timegm(
                (int(y), int(mo), int(d), int(h), int(mi), int(s), 0, 0, 0)
            )
            * 1_000_000
            + frac_us
        )
    else:
        m = _TIME_RE.match(text)
        if not m:
            return None
        h, mi, s, frac, tz = m.groups()
        frac_us = int(((frac or "") + "000000")[:6])
        base_us = (int(h) * 3600 + int(mi) * 60 + int(s)) * 1_000_000 + frac_us
    if tz is None:
        return None  # naive -> exact string comparison
    if tz == "Z":
        offset_us = 0
    else:
        sign = 1 if tz[0] == "+" else -1
        offset_us = sign * (int(tz[1:3]) * 3600 + int(tz[4:6]) * 60) * 1_000_000
    return base_us - offset_us


def _str_equal_datetime(a: str, b: str) -> bool:
    """Compare two strings that may be TOML datetime literals.

    When both parse as tz-aware datetimes they are compared by the instant
    they denote (so ``Z`` and ``+00:00`` are equivalent); otherwise the
    strings must be exactly equal.
    """
    if a == b:
        return True
    ia = _utc_us(a)
    ib = _utc_us(b)
    if ia is None or ib is None:
        return False
    return ia == ib


def _tree_equal(a, b) -> bool:
    """Strict type-sensitive structural equality (dict key order ignored)."""
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
    if isinstance(a, str):
        return _str_equal_datetime(a, b)
    return a == b


# ---------------------------------------------------------------------------
# Corpus (legal inputs must produce equivalent trees; illegal inputs must
# raise ValueError with the shared "parse error at line L, column C" prefix)
# ---------------------------------------------------------------------------

VALID_CORPUS = [
    # --- JSON ---
    ("json", '{"a": 1, "b": [true, false, null], "c": {"d": 1.5}, "e": "héllo"}'),
    ("json", r'{"s": "\u4e2d\u6587", "emoji": "\ud83d\ude00"}'),
    ("json", '{"i": -17, "f": 3.14, "e": 1e3, "E": -2.5E-2}'),
    ("json", '{"a": 1, "a": 2}'),  # duplicate key -> last-wins
    ("json", '{"n": ' + "9" * 200 + "}"),  # big integer (arbitrary precision)
    ("json", "[1, 2, 3]"),
    ("json", "42"),
    ("json", '"hi"'),
    ("json", '{"a": "it\'s"}'),  # single quote inside a string is legal
    ("json", '{"nested": {"deep": {"deeper": [1, {"x": null}]}}}'),
    # --- TOML ---
    (
        "toml",
        'title = "TOML Example"\n'
        "num = 42\nneg = -17\nfloat = 3.14\nbool = true\n"
        "arr = [1, 2, 3]\ninline = { x = 1, y = \"two\" }\n"
        "date = 1979-05-27\ntime = 07:32:00\n",
    ),
    ("toml", "a = 1_000\nb = 0xDEADBEEF\nc = 0o755\nd = 0b1010\n"),
    (
        "toml",
        'a = "basic\\nstring"\n'
        "b = 'literal\\nno'\n"
        'c = """\nmulti\nline\n"""\n'
        "d = '''\nraw\n'''\n",
    ),
    (
        "toml",
        '[server]\nhost = "localhost"\nport = 8080\n\n'
        "[server.tls]\nenabled = true\n\n"
        "[[products]]\nname = \"Hammer\"\n[[products]]\nname = \"Nail\"\n",
    ),
    ("toml", 'server.tls.enabled = true\nname = "root"\n'),
    ("toml", "a = 0x1F\nb = 0o17\nc = 0b101\n"),
    ("toml", "a = 1_000_000\n"),
    ("toml", "a = -3.5e2\nb = +17\n"),
    ("toml", "a = inf\nb = -inf\nc = nan\n"),
    ("toml", "a = +nan\nb = -nan\n"),
    ("toml", "a = 1979-05-27T07:32:00Z\n"),  # datetime, Z suffix
    ("toml", "a = 1979-05-27T07:32:00\n"),  # naive datetime
    ("toml", "a = 1979-05-27T07:32:00+07:00\n"),  # offset datetime
    ("toml", "a = 1979-05-27T07:32:00.5Z\n"),  # fractional seconds + Z
    ("toml", "a = 1979-05-27\n"),  # date
    ("toml", "a = 07:32:00\n"),  # time
    ("toml", "a = { x = 1, y = [1, 2] }\n"),  # inline table
    ("toml", '"a.b" = 1\na.b = 2\n'),  # quoted dot key vs dotted key
    ("toml", 'a = """line1\nline2"""\n'),
    ("toml", 'a = """one\\\n   two"""\n'),  # multiline line continuation
    ("toml", "a = 1979-05-27T07:32:00.999999Z\n"),  # 6-digit fraction + Z
    # --- INI ---
    (
        "ini",
        "top = value\n[section]\nkey1 = hello\nkey2: world\n"
        'quoted = "  trimmed  "\nclean = "hello"\nCaseKey = kept\n',
    ),
    ("ini", "a = 1\nb = 2\n"),  # no section -> top level
    ("ini", "a = 1\na = 2\n"),  # duplicate key -> last-wins
    ("ini", "a = \"hello\"\nb = 'world'\n"),  # quote stripping
    ("ini", "url = http://x?a=b\n"),  # '=' inside value
    ("ini", "# c1\n; c2\na = 1\n"),  # full-line comments
    ("ini", "[s]\na = 1\n[s]\nb = 2\n"),  # duplicate section -> merge
    ("ini", "[DEFAULT]\nx = 1\n[s]\ny = 2\n"),  # DEFAULT is a regular section
    ("ini", 'empty = ""\n'),  # empty quoted value
    ("ini", "empty =\n"),  # empty value
    ("ini", "[empty]\n"),  # empty section
    ("ini", "[s]\nkey = value with   spaces\n"),  # internal whitespace kept
]

INVALID_CORPUS = [
    # --- JSON ---
    ("json", '{"a": }'),  # D1
    ("json", "[1, 2,]"),  # trailing comma in array (pure shim matches C)
    ("json", '{"a": 1,}'),  # trailing comma in object (pure shim matches C)
    ("json", "{'a': 1}"),  # bare single quote (pure shim matches C)
    ("json", '{"a": \'x\'}'),  # bare single quote in value (pure shim)
    ("json", '{\n"a": 1,\n}'),  # trailing comma, multiline
    ("json", '{"n": 01}'),  # leading zero
    ("json", '{"n": -}'),  # bare minus
    ("json", "not json at all"),  # D1
    ("json", '{"a": tru}'),  # invalid literal
    ("json", ""),  # empty input
    ("json", r'{"s": "\uZZZZ"}'),  # invalid \u escape (D1 wording)
    ("json", '{"unterminated": "abc'),  # unterminated string
    # --- TOML ---
    ("toml", "a = 1\na = 2\n"),  # duplicate key (D3 wording)
    ("toml", "[a]\nx = 1\n[a]\ny = 2\n"),  # duplicate table header (D3)
    ("toml", "a = "),  # missing value (D3)
    ("toml", "[a\nx = 1\n"),  # unterminated table header (D3)
    ("toml", 'a = "unterminated\n'),  # unterminated string (D3)
    ("toml", "x = [1, 2"),  # unterminated array (D3)
    ("toml", "a = 0x\n"),  # invalid hex integer (D3)
    ("toml", "[a]\nx = 1\n[[a]]\ny = 2\n"),  # table vs array-of-tables (D3)
    # --- INI ---
    ("ini", "[sec\na = 1\n"),  # unterminated section header (D4)
    ("ini", "this is not an ini line\n"),  # no delimiter (D4)
    ("ini", "= no key\n"),  # empty key (D4)
]


def _corpus_id(val):
    """pytest parametrize id helper (called once per parameter value)."""
    if val in ("json", "toml", "ini"):
        return val
    return str(val)[:24].replace("\n", "\\n")


# ---------------------------------------------------------------------------
# Legal inputs: strict tree equivalence
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fmt,text", VALID_CORPUS, ids=_corpus_id)
def test_valid_corpus_equivalent(fmt, text):
    if not HAVE_C:
        pytest.skip("C extension not available; dual-mode comparison needs both backends")
    c_tree = _C_PARSERS[fmt](text)
    pure_tree = _PURE_PARSERS[fmt](text)
    assert _tree_equal(c_tree, pure_tree), (
        "C/pure semantic trees differ for %s input:\nC:    %r\npure: %r"
        % (fmt, c_tree, pure_tree)
    )


# ---------------------------------------------------------------------------
# Illegal inputs: both backends raise ValueError with the shared prefix
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fmt,text", INVALID_CORPUS, ids=_corpus_id)
def test_invalid_corpus_error_prefix(fmt, text):
    with pytest.raises(ValueError) as pure_exc:
        _PURE_PARSERS[fmt](text)
    pure_msg = str(pure_exc.value)
    assert pure_msg.startswith("parse error at line "), (
        "pure %s error missing prefix: %r" % (fmt, pure_msg)
    )
    assert "column" in pure_msg, pure_msg
    if HAVE_C:
        with pytest.raises(ValueError) as c_exc:
            _C_PARSERS[fmt](text)
        c_msg = str(c_exc.value)
        assert c_msg.startswith("parse error at line "), (
            "C %s error missing prefix: %r" % (fmt, c_msg)
        )
        assert "column" in c_msg, c_msg


# ---------------------------------------------------------------------------
# Exemption D2: unpaired surrogates (pure accepts, C rejects)
# ---------------------------------------------------------------------------

def test_d2_unpaired_surrogate_pure_accepts():
    data = pure_parsers.parse_json_pure(r'{"s": "\ud83d"}')
    assert isinstance(data, dict)
    assert data["s"] == "\ud83d"
    data = pure_parsers.parse_json_pure(r'{"s": "\ude00"}')
    assert data["s"] == "\ude00"


@requires_c
def test_d2_unpaired_surrogate_c_rejects():
    with pytest.raises(ValueError, match="surrogate"):
        _cfgdrift.parse_json(r'{"s": "\ud83d"}')


# ---------------------------------------------------------------------------
# C-mode-only error wording (kept C-specific per the design)
# ---------------------------------------------------------------------------

@requires_c
def test_c_specific_error_wording():
    with pytest.raises(ValueError, match="duplicate key"):
        _cfgdrift.parse_toml("a = 1\na = 2\n")
    with pytest.raises(ValueError, match="duplicate table"):
        _cfgdrift.parse_toml("[a]\nx = 1\n[a]\ny = 2\n")
    with pytest.raises(ValueError, match="trailing comma"):
        _cfgdrift.parse_json("[1, 2,]")
    with pytest.raises(ValueError, match="single quote"):
        _cfgdrift.parse_json("{'a': 1}")


# ---------------------------------------------------------------------------
# Backend selection / set_backend test hook
# ---------------------------------------------------------------------------

def test_set_backend_switches_and_restores():
    original = parser_mod.PARSER_BACKEND
    try:
        assert parser_mod.set_backend("pure") == "pure"
        assert parser_mod.PARSER_BACKEND == "pure"
        # parse_text now dispatches to the pure backend.
        assert parser_mod.parse_text("[1, 2]", "json") == {"$": [1, 2]}
        assert parser_mod.parse_text('a = "1979-05-27T07:32:00Z"\n', "toml") == {
            "a": "1979-05-27T07:32:00Z"
        }
        if HAVE_C:
            assert parser_mod.set_backend("c") == "c"
            assert parser_mod.PARSER_BACKEND == "c"
            assert parser_mod.set_backend("auto") in ("c", "pure")
        with pytest.raises(RuntimeError):
            parser_mod.set_backend("bogus")
    finally:
        parser_mod.set_backend(original)


def test_c_backend_forced_without_c_raises(monkeypatch):
    # Simulate an environment without the C extension by monkeypatching.
    monkeypatch.setattr(parser_mod, "HAVE_C", False)
    with pytest.raises(RuntimeError, match="CFGDRIFT_BACKEND"):
        parser_mod._select_backend("c")
    with pytest.raises(RuntimeError):
        parser_mod.set_backend("c")


# ---------------------------------------------------------------------------
# End-to-end: GBK file parsed identically by both backends
# ---------------------------------------------------------------------------

def test_gbk_file_dual_mode(tmp_path):
    f = tmp_path / "gbk.ini"
    f.write_bytes("title = 配置项\n".encode("gbk"))
    original = parser_mod.PARSER_BACKEND
    try:
        parser_mod.set_backend("pure")
        pure_tree = parser_mod.parse_file(str(f), "ini", warn=False)
        if HAVE_C:
            parser_mod.set_backend("c")
            c_tree = parser_mod.parse_file(str(f), "ini", warn=False)
            assert _tree_equal(c_tree, pure_tree), (c_tree, pure_tree)
    finally:
        parser_mod.set_backend(original)


# ---------------------------------------------------------------------------
# Regression: top-level INI options (no section header) in pure mode
# ---------------------------------------------------------------------------
#
# The C backend always accepted key-value pairs before the first ``[section]``
# header and exposed them as top-level keys.  The pure backend used
# ``configparser`` which only gained ``allow_unnamed_section=True`` in Python
# 3.13; on 3.8-3.12 the same input raised ``MissingSectionHeaderError``,
# breaking CI (e.g. test_ini_quote_stripping_both_backends on ubuntu).  The
# parser now re-reads such texts wrapped in the sentinel default section, so
# this must pass on every supported Python version.


_INI_TOP_LEVEL_CASES = [
    # (text, expected_tree)
    # The exact v0.2.0 QA regression: quote stripping at top level.
    (
        'a = "  trimmed  "\nb = \'world\'\nempty = ""\n',
        {"a": "  trimmed  ", "b": "world", "empty": ""},
    ),
    ("a = 1\nb = 2\n", {"a": "1", "b": "2"}),
    ("a: 1\n", {"a": "1"}),  # colon delimiter
    ("a = 1\na = 2\n", {"a": "2"}),  # duplicate key -> last-wins
    ('empty = ""\n', {"empty": ""}),  # empty quoted value
    ("url = http://x?a=b\n", {"url": "http://x?a=b"}),  # '=' inside value
    ("# c1\n; c2\na = 1\n", {"a": "1"}),  # full-line comments
    ("Key = 1\n", {"Key": "1"}),  # key case preserved
    # Top-level options followed by a real section header.
    ("a = 1\n[s]\nb = 2\n", {"a": "1", "s": {"b": "2"}}),
]


def test_ini_top_level_options_pure_regression():
    """Pure backend parses top-level INI options on every Python version.

    Before the fix this raised ``MissingSectionHeaderError`` (wrapped as
    ``ValueError``) on Python 3.8-3.12 while the C backend succeeded, so the
    dual-mode corpus diverged.  Assert the exact trees so a future
    configparser behavior change is caught.
    """
    for text, expected in _INI_TOP_LEVEL_CASES:
        got = pure_parsers.parse_ini_pure(text)
        assert _tree_equal(got, expected), (text, got, expected)


@requires_c
def test_ini_top_level_options_c_pure_equivalent():
    """C and pure backends agree on top-level INI options (dual-mode)."""
    for text, expected in _INI_TOP_LEVEL_CASES:
        c_tree = _C_PARSERS["ini"](text)
        pure_tree = _PURE_PARSERS["ini"](text)
        assert _tree_equal(c_tree, expected), (text, c_tree, expected)
        assert _tree_equal(pure_tree, expected), (text, pure_tree, expected)
        assert _tree_equal(c_tree, pure_tree), (text, c_tree, pure_tree)


def test_ini_top_level_options_dispatch_both_backends():
    """The dispatch layer (set_backend) parses top-level INI in both modes."""
    text = 'a = "  trimmed  "\nb = \'world\'\nempty = ""\n'
    expected = {"a": "  trimmed  ", "b": "world", "empty": ""}
    original = parser_mod.PARSER_BACKEND
    try:
        parser_mod.set_backend("pure")
        assert parser_mod.parse_text(text, "ini") == expected
        if HAVE_C:
            parser_mod.set_backend("c")
            assert parser_mod.parse_text(text, "ini") == expected
    finally:
        parser_mod.set_backend(original)


# ---------------------------------------------------------------------------
# End-to-end: pure backend CLI pipeline (scan -> baseline -> diff)
# ---------------------------------------------------------------------------

def _run_cli(args, env):
    return subprocess.run(
        [sys.executable, "-m", "cfgdrift.cli"] + args,
        capture_output=True,
        text=True,
        env=env,
        cwd=ROOT,
        timeout=90,
    )


def test_pure_backend_cli_end_to_end(tmp_path):
    """Pure backend full pipeline: scan -> baseline -> diff (exit codes)."""
    home = tmp_path / "home"
    home.mkdir()
    conf = tmp_path / "conf"
    conf.mkdir()
    app = conf / "app.json"
    app.write_text(
        json.dumps({"server": {"host": "localhost", "port": 8080}}),
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "PYTHONPATH": SRC,
        "CFGDRIFT_HOME": str(home),
        "CFGDRIFT_BACKEND": "pure",
    }
    assert _run_cli(["init"], env).returncode == 0
    p = _run_cli(["scan", str(conf), "--save-as-baseline", "prod"], env)
    assert p.returncode == 0, p.stdout + p.stderr

    app.write_text(
        json.dumps({"server": {"host": "changed", "port": 8080}}),
        encoding="utf-8",
    )
    p = _run_cli(["diff", str(conf), "--baseline", "prod"], env)
    assert p.returncode == 1, p.stdout + p.stderr
    assert "server.host" in p.stdout

    app.write_text(
        json.dumps({"server": {"host": "localhost", "port": 8080}}),
        encoding="utf-8",
    )
    p = _run_cli(["diff", str(conf), "--baseline", "prod"], env)
    assert p.returncode == 0, p.stdout + p.stderr
