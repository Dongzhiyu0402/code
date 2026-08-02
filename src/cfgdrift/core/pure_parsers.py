"""Pure-Python fallback parsers for cfgdrift (v0.2.0).

These module-level functions mirror the C extension (``cfgdrift._cfgdrift``)
parsers for JSON / TOML / INI and return *raw* ``dict`` / ``list`` / scalar
values.  Normalization to the final semantic tree is performed by
``cfgdrift.core.parser._normalize`` / ``_wrap_top_level`` so that both
backends share one normalization path.

Error contract (shared with the C backend): every parse error raises
``ValueError`` whose message starts with ``"parse error at line L, column C: "``
with 1-based ``L``/``C``.  Per the v0.2.0 dual-mode design the text after the
colon may differ between backends (exemptions D1-D4, see
``docs/system_design.md``).

Python 3.8 compatibility is preserved via ``from __future__ import
annotations`` and the ``tomli`` fallback for ``tomllib``.
"""

from __future__ import annotations

import configparser
import json
import re
from datetime import date, datetime, time, timedelta
from typing import Any, Optional, Tuple

try:  # Python 3.11+
    import tomllib
    _TOMLDecodeError = tomllib.TOMLDecodeError
except ModuleNotFoundError:  # Python 3.8-3.10
    import tomli as tomllib  # type: ignore[no-redef]
    _TOMLDecodeError = tomllib.TOMLDecodeError

# Python 3.13+ tomllib renders position info only inside the message
# ("... (at line L, column C)") and no longer exposes lineno/colno/msg
# attributes; tomli (3.8-3.10) exposes them as attributes.
_TOML_LINECOL_RE = re.compile(r"\(at line (\d+), column (\d+)\)\s*$")


def _toml_error_line_col(exc: Exception) -> Tuple[int, int, str]:
    """Extract ``(line, col, msg)`` from a ``TOMLDecodeError``.

    Prefers the ``lineno`` / ``colno`` / ``msg`` attributes (tomli and
    tomllib <= 3.12) and falls back to parsing the trailing
    ``"(at line L, column C)"`` out of the formatted message (Python 3.13+).
    """
    lineno = getattr(exc, "lineno", None)
    colno = getattr(exc, "colno", None)
    msg = getattr(exc, "msg", None)
    if isinstance(lineno, int) and isinstance(colno, int):
        return lineno, colno, (msg if msg else str(exc))
    text = str(exc)
    m = _TOML_LINECOL_RE.search(text)
    if m:
        return int(m.group(1)), int(m.group(2)), text[: m.start()].rstrip()
    return 1, 1, text


# ---------------------------------------------------------------------------
# Shared error construction
# ---------------------------------------------------------------------------

def _parse_error(line: int, col: int, msg: str) -> ValueError:
    """Build the shared parse-error ``ValueError`` (see module docstring)."""
    return ValueError(
        "parse error at line %d, column %d: %s" % (line, col, msg)
    )


def _line_col_at(text: str, index: int) -> Tuple[int, int]:
    """Compute the 1-based ``(line, column)`` of ``text[index]``."""
    line = 1
    col = 1
    for ch in text[:index]:
        if ch == "\n":
            line += 1
            col = 1
        else:
            col += 1
    return line, col


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------

# Matches a trailing comma at the end of an object/array (whitespace allowed
# between the comma and the closing brace/bracket, plus any trailing
# whitespace after the closer).
_JSON_TRAILING_COMMA_RE = re.compile(r",\s*[}\]]\s*$")


def _find_bare_single_quote(text: str) -> Optional[int]:
    """Return the index of the first ``'`` outside a double-quoted string.

    Tracks double-quote string state (including backslash escapes) so quotes
    inside strings (e.g. ``{"a": "it's"}``) are ignored; any other ``'`` is a
    JSON syntax error and is reported with the C backend's wording.
    """
    in_string = False
    escaped = False
    for index, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "'":
                return index
    return None


def parse_json_pure(text: str) -> Any:
    """Parse JSON text with the stdlib ``json`` module (raw tree).

    Error alignment shims on top of ``json.loads``:

    - a bare single quote outside any double-quoted string raises
      ``"bare single quotes are not allowed in JSON"`` (same wording as the C
      backend);
    - a trailing comma (``,}`` / ``,]`` at the end) raises
      ``"trailing comma in object"`` / ``"trailing comma in array"`` keeping
      the stdlib exception's line/column;
    - every other error reuses the stdlib message (exemption D1); unpaired
      surrogate escapes are accepted by the stdlib (exemption D2).
    """
    quote_index = _find_bare_single_quote(text)
    if quote_index is not None:
        line, col = _line_col_at(text, quote_index)
        raise _parse_error(line, col, "bare single quotes are not allowed in JSON")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        if _JSON_TRAILING_COMMA_RE.search(text):
            if text.rstrip().endswith("}"):
                msg = "trailing comma in object"
            else:
                msg = "trailing comma in array"
            raise _parse_error(exc.lineno, exc.colno, msg) from exc
        raise _parse_error(exc.lineno, exc.colno, exc.msg) from exc


# ---------------------------------------------------------------------------
# TOML
# ---------------------------------------------------------------------------

def _replace_utc_suffix(text: str) -> str:
    """Replace the ``+00:00`` suffix produced by ``isoformat()`` with ``Z``."""
    if text.endswith("+00:00"):
        return text[:-6] + "Z"
    return text


def _toml_datetime_to_iso(value: Any) -> str:
    """Convert a TOML ``datetime`` / ``date`` / ``time`` to the C backend's
    canonical ISO-8601 string form.

    The C parser emits the literal token text, so ``1979-05-27T07:32:00Z``
    stays ``"1979-05-27T07:32:00Z"`` while ``datetime.isoformat()`` would
    render the same instant as ``"1979-05-27T07:32:00+00:00"``.  A zero UTC
    offset therefore becomes a ``Z`` suffix; any other offset keeps the
    ``±HH:MM`` form.  Fractional seconds are kept as ``isoformat()`` renders
    them (the dual-mode harness compares datetimes by instant, not text).
    """
    if isinstance(value, datetime):
        text = value.isoformat()
        if value.tzinfo is not None and value.utcoffset() == timedelta(0):
            return _replace_utc_suffix(text)
        return text
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, time):
        text = value.isoformat()
        if value.tzinfo is not None and value.utcoffset() == timedelta(0):
            return _replace_utc_suffix(text)
        return text
    return str(value)


def _toml_datetime_walk(data: Any) -> Any:
    """Recursively convert TOML datetime objects to canonical ISO strings."""
    if isinstance(data, dict):
        return {key: _toml_datetime_walk(value) for key, value in data.items()}
    if isinstance(data, list):
        return [_toml_datetime_walk(value) for value in data]
    if isinstance(data, (datetime, date, time)):
        return _toml_datetime_to_iso(data)
    return data


def parse_toml_pure(text: str) -> Any:
    """Parse TOML text with ``tomllib`` / ``tomli`` (raw tree).

    ``TOMLDecodeError`` carries 1-based ``lineno`` / ``colno`` which are
    wrapped into the shared ``ValueError`` prefix.  datetime / date / time
    objects are converted to the C backend's canonical ISO strings.
    """
    try:
        data = tomllib.loads(text)
    except _TOMLDecodeError as exc:
        line, col, msg = _toml_error_line_col(exc)
        raise _parse_error(line, col, msg) from exc
    return _toml_datetime_walk(data)


# ---------------------------------------------------------------------------
# INI
# ---------------------------------------------------------------------------

# Sentinel default section: a literal ``[DEFAULT]`` must behave like any other
# section (C semantics) and options before the first header become top-level
# keys instead of propagating into every section.
_CFGDRIFT_DEFAULT_SECTION = "__cfgdrift_never_default__"


def _make_ini_parser() -> configparser.ConfigParser:
    """Build a ``ConfigParser`` configured to mirror the C INI semantics.

    - ``strict=False``: duplicate keys are last-wins; duplicate sections merge.
    - ``optionxform=str``: key case is preserved.
    - ``interpolation=None``: ``%`` is not special.
    - ``inline_comment_prefixes=None``: no end-of-line comments (C limitation).
    - ``empty_lines_in_values=False``: blank lines never join multiline values.
    - ``default_section`` sentinel: see module constant docstring.
    - ``allow_unnamed_section=True`` (Python 3.13+ only): the refactored
      configparser otherwise rejects options before the first section header.
    """
    kwargs = dict(
        strict=False,
        interpolation=None,
        inline_comment_prefixes=None,
        empty_lines_in_values=False,
        delimiters=("=", ":"),
        default_section=_CFGDRIFT_DEFAULT_SECTION,
    )
    if hasattr(configparser, "UNNAMED_SECTION"):  # Python 3.13+
        kwargs["allow_unnamed_section"] = True
    cp = configparser.ConfigParser(**kwargs)
    cp.optionxform = str  # type: ignore[assignment]
    return cp


def _ini_trim_value(value: str) -> str:
    """Mirror the C ``ini_trim_value`` helper.

    Strip surrounding whitespace, then remove a matching pair of surrounding
    quotes (``"`` or ``'``) if present; whitespace *inside* the quotes is
    preserved.
    """
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        value = value[1:-1]
    return value


def _ini_collect(cp: configparser.ConfigParser) -> dict:
    """Collect a raw tree from a parsed ``ConfigParser``.

    Reads ``cp._sections`` (the internal per-section dicts, which do NOT
    include merged ``DEFAULT`` values) for section data and ``cp.defaults()``
    for top-level keys.  On Python 3.13+ top-level keys live in the unnamed
    section instead (because ``allow_unnamed_section=True``).  This internal
    API has been stable across CPython 3.8-3.13 and is intentionally used to
    avoid the DEFAULT-section merge that ``items()`` performs.
    """
    result: dict = {}

    top: dict = {}
    unnamed = getattr(configparser, "UNNAMED_SECTION", None)
    if unnamed is not None and unnamed in cp._sections:
        top = dict(cp._sections[unnamed])
    else:
        top = dict(cp.defaults())
    top.pop("__name__", None)
    for key, value in top.items():
        result[key] = _ini_trim_value(str(value))

    for section, options in cp._sections.items():
        if unnamed is not None and section == unnamed:
            continue
        sec: dict = {}
        for key, value in options.items():
            if key == "__name__":
                continue
            sec[key] = _ini_trim_value(str(value))
        result[str(section)] = sec
    return result


def parse_ini_pure(text: str) -> Any:
    """Parse INI text with ``configparser`` (raw tree).

    ``configparser.Error`` inherits from ``Exception`` (not ``ValueError``),
    so it is wrapped into the shared ``ValueError`` prefix; the line number is
    taken from ``ParsingError.errors[0][0]`` when available, else the
    exception's own ``lineno``, else 1.
    """
    cp = _make_ini_parser()
    try:
        cp.read_string(text)
    except configparser.Error as exc:
        line = 1
        lineno = getattr(exc, "lineno", None)
        if isinstance(lineno, int) and lineno > 0:
            line = lineno
        elif isinstance(exc, configparser.ParsingError) and exc.errors:
            line = exc.errors[0][0]
        raise _parse_error(line, 1, str(exc)) from exc
    return _ini_collect(cp)
