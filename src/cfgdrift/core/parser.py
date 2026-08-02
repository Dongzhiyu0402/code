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


def detect_format(path: str) -> Optional[str]:
    """Detect the config format from a file extension.

    Returns ``"json"`` / ``"yaml"`` / ``"toml"`` / ``"ini"`` or ``None`` if
    the extension is unknown.
    """
    _, ext = os.path.splitext(path)
    return _EXTENSION_FORMATS.get(ext.lower())


def validate_format(fmt: str) -> str:
    """Validate a ``--format`` value; returns the normalized value."""
    if fmt not in _VALID_FORMATS:
        raise ValueError(
            "invalid format %r (expected one of: %s)"
            % (fmt, ", ".join(_VALID_FORMATS))
        )
    return fmt


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
    """Parse text of a given format into a normalized semantic tree (dict)."""
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
    else:  # pragma: no cover - guarded by validate_format
        raise ValueError("unsupported format %r" % fmt)
    if data is None:
        data = {}
    return _wrap_top_level(_normalize(data))


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
