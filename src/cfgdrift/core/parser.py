"""Format detection and parsing dispatch.

JSON / TOML / INI are parsed by the C extension ``cfgdrift._cfgdrift`` when it
is available (compiled at install time) and fall back to the pure-Python
parsers in :mod:`cfgdrift.core.pure_parsers` otherwise.  YAML is parsed by
PyYAML and normalized into the same semantic tree.

Backend selection (v0.2.0):

- ``CFGDRIFT_BACKEND`` environment variable: ``auto`` (default) | ``pure`` |
  ``c``.  ``auto`` uses the C extension when importable and silently degrades
  to pure Python otherwise; ``pure`` forces the pure parsers; ``c`` forces the
  C extension and raises ``RuntimeError`` at import time when it is missing.
- :func:`set_backend` switches the backend at runtime (test hook, not a
  stable public API).

Both backends share the same normalization path (``_normalize`` /
``_wrap_top_level``) so they produce equivalent semantic trees (see the
v0.2.0 dual-mode design in ``docs/system_design.md``).
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import date, datetime, time
from typing import Any, Dict, Optional, Tuple

try:  # pragma: no cover - exercised via import
    from .. import _cfgdrift  # C extension (compiled at install time)
    HAVE_C = True
except ImportError:  # pragma: no cover - fallback path when not compiled
    _cfgdrift = None  # type: ignore[assignment]
    HAVE_C = False

try:
    import yaml
    _HAVE_YAML = True
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]
    _HAVE_YAML = False

from . import pure_parsers  # noqa: E402  (pure-Python fallback backend)
from .lines import build_line_map as _lines_build_line_map  # noqa: E402
from . import plugins as _plugins_mod  # noqa: E402  (parser plugin registry)

logger = logging.getLogger("cfgdrift.core.parser")


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------

_VALID_BACKENDS = ("auto", "pure", "c")


def _resolve_backend(choice: str) -> str:
    """Resolve a normalized backend choice to an active backend name.

    ``choice`` is one of ``"auto"`` / ``"pure"`` / ``"c"``.  ``"c"`` without
    the extension raises ``RuntimeError``.
    """
    if choice == "pure":
        return "pure"
    if choice == "c":
        if not HAVE_C:
            raise RuntimeError(
                "backend 'c' requested but the C extension cfgdrift._cfgdrift "
                "is not available (install the compiled wheel or drop "
                "CFGDRIFT_BACKEND=c)"
            )
        return "c"
    # auto (default): prefer the C extension, silently degrade to pure.
    return "c" if HAVE_C else "pure"


def _select_backend(env_value: Optional[str]) -> str:
    """Resolve the active backend from a ``CFGDRIFT_BACKEND`` value."""
    choice = (env_value or "auto").strip().lower()
    if choice not in _VALID_BACKENDS:
        raise RuntimeError(
            "invalid CFGDRIFT_BACKEND %r (expected one of: %s)"
            % (env_value, ", ".join(_VALID_BACKENDS))
        )
    return _resolve_backend(choice)


PARSER_BACKEND: str = _select_backend(os.environ.get("CFGDRIFT_BACKEND"))
logger.debug("parser backend: %s", PARSER_BACKEND)


def set_backend(name: str) -> str:
    """Switch the active parsing backend at runtime.

    Test hook (not a stable public API): ``"c"`` selects the C extension and
    ``"pure"`` the pure-Python parsers; ``"auto"`` re-runs auto-selection.
    Selecting ``"c"`` without the extension raises ``RuntimeError``.
    Returns the active backend name.
    """
    global PARSER_BACKEND
    choice = (name or "auto").strip().lower()
    if choice not in _VALID_BACKENDS:
        raise RuntimeError(
            "invalid backend %r (expected one of: %s)"
            % (name, ", ".join(_VALID_BACKENDS))
        )
    PARSER_BACKEND = _resolve_backend(choice)
    logger.debug("parser backend: %s", PARSER_BACKEND)
    return PARSER_BACKEND


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------

_EXTENSION_FORMATS = {
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".ini": "ini",
    ".cfg": "ini",
    ".conf": "ini",
}

_VALID_FORMATS = ("auto", "json", "yaml", "toml", "ini")

_FORMAT_EXTENSIONS = {
    "json": (".json",),
    "yaml": (".yaml", ".yml"),
    "toml": (".toml",),
    "ini": (".ini", ".cfg", ".conf"),
}

#: Plugin registry shared by detect_format / validate_format / parse_text.
#: The four built-in formats are registered as built-in plugins at import
#: time (bottom of this module); external plugins are discovered from the
#: ``cfgdrift.parsers`` entry point group afterwards.
_PLUGIN_REGISTRY = _plugins_mod.default_registry


def detect_format(path: str) -> Optional[str]:
    """Detect the config format from a file extension.

    Built-in extensions are checked first (``.json`` / ``.yaml`` / ``.yml`` /
    ``.toml`` / ``.ini`` / ``.cfg`` / ``.conf``); unknown extensions fall
    back to registered parser plugins (v0.5.0).  Returns ``None`` when no
    format/plugin owns the extension.
    """
    _, ext = os.path.splitext(path)
    ext = ext.lower()
    builtin = _EXTENSION_FORMATS.get(ext)
    if builtin is not None:
        return builtin
    return _PLUGIN_REGISTRY.by_extension(ext)


def validate_format(fmt: str) -> str:
    """Validate a ``--format`` value; returns the normalized value.

    Legal values are ``auto`` / ``json`` / ``yaml`` / ``toml`` / ``ini``
    plus every registered custom plugin name.  With no custom plugins the
    error message is byte-for-byte identical to v0.4.0 (the plugin names are
    only appended when custom plugins exist); unregistered names additionally
    receive the registration guidance (v0.5.0).
    """
    if fmt in _VALID_FORMATS:
        return fmt
    if _PLUGIN_REGISTRY.get(fmt) is not None:
        return fmt
    custom = _PLUGIN_REGISTRY.custom_names()
    expected = list(_VALID_FORMATS)
    if custom:
        expected = expected + custom
    message = "invalid format %r (expected one of: %s)" % (
        fmt,
        ", ".join(expected),
    )
    if fmt not in _VALID_FORMATS:
        message += (
            "\nregister a parser plugin via the 'cfgdrift.parsers' entry point group"
            "\n(pyproject: [project.entry-points.\"cfgdrift.parsers\"] mydsl = \"pkg:plugin\")"
            "\nor in-process via cfgdrift.core.plugins.register_plugin, then retry."
        )
    raise ValueError(message)


# ---------------------------------------------------------------------------
# Text reading with encoding fallback (section 7.7)
# ---------------------------------------------------------------------------

def _read_text(path: str) -> Tuple[str, str]:
    """Read a config file as text.

    Strategy: UTF-8 strict -> GBK strict -> UTF-8 with replacement.  Returns
    ``(text, encoding)`` where ``encoding`` is one of ``"utf-8"``, ``"gbk"``
    or ``"utf-8-replace"`` so callers can warn on fallback.
    """
    with open(path, "rb") as fh:
        raw = fh.read()
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("gbk"), "gbk"
    except UnicodeDecodeError:
        pass
    return raw.decode("utf-8", errors="replace"), "utf-8-replace"


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def _normalize(data: Any) -> dict:
    """Normalize a parsed value into a pure semantic tree.

    - Top-level non-dict values are wrapped as ``{"$": value}``.
    - ``datetime`` / ``date`` / ``time`` objects (e.g. from PyYAML) become
      ISO-8601 strings.
    - Keys are converted to ``str``.
    """
    if isinstance(data, dict):
        return {str(k): _normalize(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_normalize(v) for v in data]
    if isinstance(data, (datetime, date, time)):
        return data.isoformat()
    if data is None or isinstance(data, (str, int, float, bool)):
        return data
    # Fallback: convert anything exotic to a string.
    return str(data)


def _wrap_top_level(data: Any) -> dict:
    """Normalize a parsed value into a semantic tree.

    Top-level non-dict values are wrapped as ``{"$": value}``; dicts are
    recursively normalized (keys -> str, datetime/date/time -> ISO strings).
    """
    return _normalize(data) if isinstance(data, dict) else {"$": _normalize(data)}


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _parse_json(text: str) -> Any:
    if PARSER_BACKEND == "c":
        return _cfgdrift.parse_json(text)
    return pure_parsers.parse_json_pure(text)


def _parse_toml(text: str) -> Any:
    if PARSER_BACKEND == "c":
        return _cfgdrift.parse_toml(text)
    return pure_parsers.parse_toml_pure(text)


def _parse_ini(text: str) -> Any:
    if PARSER_BACKEND == "c":
        return _cfgdrift.parse_ini(text)
    return pure_parsers.parse_ini_pure(text)


def _parse_yaml(text: str) -> Any:
    if not _HAVE_YAML:
        raise RuntimeError("PyYAML is required to parse YAML files (pip install PyYAML).")
    try:
        docs = list(yaml.safe_load_all(text))
    except yaml.YAMLError as exc:
        raise ValueError("YAML parse error: %s" % str(exc).strip()) from exc
    if len(docs) > 1:
        raise ValueError("multi-document YAML not supported")
    return docs[0] if docs else None


def parse_text(text: str, fmt: str) -> dict:
    """Parse text of a given format into a normalized semantic tree (dict).

    Built-in formats behave exactly as in v0.4.0.  A custom plugin format is
    dispatched to ``plugin.parse`` and then normalized through the same
    ``_normalize`` / ``_wrap_top_level`` path as the built-ins.
    """
    fmt = validate_format(fmt)
    if fmt == "auto":
        raise ValueError("parse_text requires an explicit format (not 'auto')")
    if fmt == "json":
        data = _parse_json(text)
    elif fmt == "yaml":
        data = _parse_yaml(text)
    elif fmt == "toml":
        data = _parse_toml(text)
    elif fmt == "ini":
        data = _parse_ini(text)
    else:
        plugin = _PLUGIN_REGISTRY.get(fmt)
        if plugin is None:  # pragma: no cover - guarded by validate_format
            raise ValueError("unsupported format %r" % fmt)
        data = plugin.parse(text)
    if data is None:
        data = {}
    return _wrap_top_level(_normalize(data))


def parse_text_lines(text: str, fmt: str) -> Tuple[dict, dict]:
    """Parse text and build ``{key_path: line}`` for the same text.

    Returns ``(tree, line_map)``.  For built-in formats the line map is
    produced by the lightweight text scan (:mod:`cfgdrift.core.lines`) that is
    independent of the parsing backend.  For custom plugin formats the
    plugin's optional ``build_line_map`` is used (``{}`` when not provided,
    D10).
    """
    fmt = validate_format(fmt)
    if fmt in ("json", "yaml", "toml", "ini"):
        tree = parse_text(text, fmt)
        line_map = _lines_build_line_map(text, fmt)
        return tree, line_map
    plugin = _PLUGIN_REGISTRY.get(fmt)
    if plugin is None:  # pragma: no cover - guarded by validate_format
        raise ValueError("unsupported format %r" % fmt)
    raw = plugin.parse(text)
    tree = _wrap_top_level(_normalize(raw))
    line_map = plugin.build_line_map(text)
    return tree, line_map


def parse_file(path: str, fmt: str = "auto", warn: bool = True) -> Dict[str, Any]:
    """Read and parse a config file into a normalized semantic tree (dict).

    ``fmt`` may be ``"auto"`` (detected from the extension).  When the file is
    read with a fallback encoding a warning is printed to stderr (unless
    ``warn=False``).
    """
    fmt = validate_format(fmt)
    if fmt == "auto":
        fmt = detect_format(path)
        if fmt is None:
            raise ValueError(
                "cannot auto-detect format for %r (use --format json|yaml|toml|ini)"
                % path
            )
    text, encoding = _read_text(path)
    if warn and encoding != "utf-8":
        print(
            "warning: %s: decoded as %s (not strict UTF-8)" % (path, encoding),
            file=sys.stderr,
        )
    return parse_text(text, fmt)


def parse_file_lines(
    path: str, fmt: str = "auto", warn: bool = True
) -> Tuple[Dict[str, Any], Dict[str, int]]:
    """Read and parse a config file, returning ``(tree, line_map)``.

    ``line_map`` maps semantic key paths to 1-based lines in the raw text
    (see :func:`parse_text_lines`).  Errors follow :func:`parse_file`.
    """
    fmt = validate_format(fmt)
    if fmt == "auto":
        fmt = detect_format(path)
        if fmt is None:
            raise ValueError(
                "cannot auto-detect format for %r (use --format json|yaml|toml|ini)"
                % path
            )
    text, encoding = _read_text(path)
    if warn and encoding != "utf-8":
        print(
            "warning: %s: decoded as %s (not strict UTF-8)" % (path, encoding),
            file=sys.stderr,
        )
    return parse_text_lines(text, fmt)


# ---------------------------------------------------------------------------
# Built-in plugins + external discovery (v0.5.0)
# ---------------------------------------------------------------------------

def _register_builtin_plugins() -> None:
    """Register the four built-in formats as built-in plugins.

    The built-in branches in :func:`parse_text` remain the primary dispatch
    path; registering them as plugins makes ``custom_names()`` accurate and
    lets ``detect_format`` reuse one registry for custom extensions.  Parse
    and line-map behavior is identical to v0.4.0 by construction (the same
    backend functions / ``lines.build_line_map`` are reused).
    """

    def _make_parse(fmt: str):
        def parse(text: str) -> Any:
            if fmt == "json":
                return _parse_json(text)
            if fmt == "yaml":
                return _parse_yaml(text)
            if fmt == "toml":
                return _parse_toml(text)
            return _parse_ini(text)

        return parse

    def _make_line_map(fmt: str):
        def build(text: str) -> Dict[str, int]:
            return _lines_build_line_map(text, fmt)

        return build

    for fmt in ("json", "yaml", "toml", "ini"):
        _PLUGIN_REGISTRY.register(
            _plugins_mod.ParserPlugin(
                name=fmt,
                extensions=_FORMAT_EXTENSIONS[fmt],
                parse=_make_parse(fmt),
                build_line_map=_make_line_map(fmt),
            ),
            replace=True,
        )


_register_builtin_plugins()

# Discover external parser plugins (entry point group "cfgdrift.parsers").
# Best-effort: individual failures are logged as warnings and never affect
# the built-in parsers.
_plugins_mod.discover_entry_points()
