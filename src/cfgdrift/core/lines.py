"""Text-based key-path -> line extraction (v0.4.0).

``build_line_map(text, fmt)`` scans the *raw* config text with lightweight
per-format scanners and returns ``{key_path: line}`` where ``line`` is
1-based.  The scan is independent of the parsing backend (C extension vs
pure-Python fallback), so line maps are consistent across both modes **by
construction** — the C parsers are never touched.

Key-path convention (section 7.2 of the system design): segments joined with
``.``, array indices appended as ``[i]``, segments escaped via
:func:`cfgdrift.core.model.join_path`.  Top-level non-dict documents are
mapped under the ``$`` pseudo-key (matching the ``{"$": ...}`` semantic-tree
wrapping), so ``$[0]`` etc. line up with the keys the differ emits.

Format coverage:

- JSON: a lightweight tokenizer + structural cursor.  Strings / escapes /
  ``{}[]:,''`` are handled; duplicate keys are last-wins (the later line
  overwrites the earlier one); each key maps to the line where its *value*
  starts.
- TOML: line-oriented scan — ``[a.b]`` / ``[[a.b]]`` table headers, dotted
  keys, inline tables on the same line, and multi-line arrays with one entry
  per ``[i]``.
- INI: line-oriented — ``[section]`` switches the table, ``key=value`` /
  ``key:value`` record the key line; duplicate keys are last-wins.
- YAML: the ``yaml.parse`` event stream (``MappingStart`` /
  ``SequenceStart`` / ``Scalar`` with ``start_mark.line``, 0-based -> +1);
  sequences are indexed ``[i]``; multi-line block scalars keep the key line.

If scanning fails for any reason the caller still gets an empty map — line
numbers are enrichment, never a blocker.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from .model import join_path

logger = logging.getLogger("cfgdrift.core.lines")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def build_line_map(text: str, fmt: str) -> Dict[str, int]:
    """Return ``{key_path: 1-based line}`` for ``text`` in ``fmt``.

    ``fmt`` is one of ``"json"`` / ``"yaml"`` / ``"toml"`` / ``"ini"``.
    """
    try:
        if fmt == "json":
            return _build_json_line_map(text or "")
        if fmt == "yaml":
            return _build_yaml_line_map(text or "")
        if fmt == "toml":
            return _build_toml_line_map(text or "")
        if fmt == "ini":
            return _build_ini_line_map(text or "")
    except Exception as exc:  # noqa: BLE001 - line maps must never block
        logger.warning("line map extraction failed for %s: %s", fmt, exc)
        return {}
    return {}


# ---------------------------------------------------------------------------
# JSON: tokenizer + structural cursor
# ---------------------------------------------------------------------------

_TOKEN_LBRACE = "lbrace"
_TOKEN_RBRACE = "rbrace"
_TOKEN_LBRACKET = "lbracket"
_TOKEN_RBRACKET = "rbracket"
_TOKEN_COLON = "colon"
_TOKEN_COMMA = "comma"
_TOKEN_STRING = "string"
_TOKEN_NUMBER = "number"
_TOKEN_LITERAL = "literal"  # true / false / null
_TOKEN_EOF = "eof"


def _json_tokens(text: str) -> List[Tuple[str, str, int]]:
    """Tokenize JSON text into ``(type, raw, line)`` tuples (1-based line).

    Lightweight and tolerant: it only needs enough structure to track paths,
    not to validate the document (parsing itself is done elsewhere).
    """
    tokens: List[Tuple[str, str, int]] = []
    i = 0
    n = len(text)
    line = 1
    while i < n:
        ch = text[i]
        if ch == "\n":
            line += 1
            i += 1
            continue
        if ch in " \t\r":
            i += 1
            continue
        if ch == "{":
            tokens.append((_TOKEN_LBRACE, ch, line))
            i += 1
            continue
        if ch == "}":
            tokens.append((_TOKEN_RBRACE, ch, line))
            i += 1
            continue
        if ch == "[":
            tokens.append((_TOKEN_LBRACKET, ch, line))
            i += 1
            continue
        if ch == "]":
            tokens.append((_TOKEN_RBRACKET, ch, line))
            i += 1
            continue
        if ch == ":":
            tokens.append((_TOKEN_COLON, ch, line))
            i += 1
            continue
        if ch == ",":
            tokens.append((_TOKEN_COMMA, ch, line))
            i += 1
            continue
        if ch == '"':
            start_line = line
            j = i + 1
            buf: List[str] = []
            while j < n:
                c = text[j]
                if c == "\\" and j + 1 < n:
                    nxt = text[j + 1]
                    if nxt == "u" and j + 5 < n:
                        # \uXXXX -> the decoded character (best effort).
                        try:
                            buf.append(chr(int(text[j + 2 : j + 6], 16)))
                        except ValueError:
                            buf.append("\\u" + text[j + 2 : j + 6])
                        j += 6
                        continue
                    _ESCAPES = {
                        "n": "\n",
                        "t": "\t",
                        "r": "\r",
                        "b": "\b",
                        "f": "\f",
                        '"': '"',
                        "\\": "\\",
                        "/": "/",
                    }
                    buf.append(_ESCAPES.get(nxt, nxt))
                    if nxt == "\n":
                        line += 1
                    j += 2
                    continue
                if c == "\n":
                    line += 1
                if c == '"':
                    j += 1
                    break
                buf.append(c)
                j += 1
            tokens.append((_TOKEN_STRING, "".join(buf), start_line))
            i = j
            continue
        # Numbers / literals run to the next structural character.
        start_line = line
        j = i
        while j < n and text[j] not in "{}[]:,\"\n \t\r":
            if text[j] == "\n":
                line += 1
            j += 1
        raw = text[i:j]
        if raw in ("true", "false", "null"):
            tokens.append((_TOKEN_LITERAL, raw, start_line))
        else:
            tokens.append((_TOKEN_NUMBER, raw, start_line))
        i = j
    tokens.append((_TOKEN_EOF, "", line))
    return tokens


class _Frame:
    __slots__ = ("kind", "path", "pending_key", "expects_value", "index")

    def __init__(self, kind: str, path: List[Tuple[str, Any]]) -> None:
        self.kind = kind  # "dict" | "list"
        self.path = path
        self.pending_key: Optional[str] = None
        self.expects_value = False
        self.index = 0


def _build_json_line_map(text: str) -> Dict[str, int]:
    tokens = _json_tokens(text)
    line_map: Dict[str, int] = {}
    stack: List[_Frame] = []
    root_is_list = False
    i = 0
    n = len(tokens)

    def consume_value(line: int) -> None:
        """Record the current pending value (dict key or list index)."""
        nonlocal stack
        if not stack:
            # Top-level scalar / bare array element -> the "$" pseudo-key.
            line_map["$"] = line
            return
        frame = stack[-1]
        if frame.kind == "dict" and frame.expects_value and frame.pending_key is not None:
            line_map[join_path(frame.path + [("key", frame.pending_key)])] = line
            frame.pending_key = None
            frame.expects_value = False
        elif frame.kind == "list":
            line_map[join_path(frame.path + [("index", frame.index)])] = line
            frame.index += 1

    def push_frame(kind: str, line: int) -> None:
        """Push a dict/list frame, consuming the parent's pending value.

        The new frame's own path includes the key/index it is the value of;
        this is done *here* (not in consume_value) so the parent's consumed
        key is still available when the nested path is built.
        """
        nonlocal stack, root_is_list
        if not stack:
            if kind == "list":
                root_is_list = True
                stack.append(_Frame("list", [("key", "$")]))
            else:
                stack.append(_Frame("dict", []))
            return
        parent = stack[-1]
        if parent.kind == "dict" and parent.expects_value and parent.pending_key is not None:
            key = parent.pending_key
            path = parent.path + [("key", key)]
            line_map[join_path(path)] = line
            parent.pending_key = None
            parent.expects_value = False
            stack.append(_Frame(kind, path))
        elif parent.kind == "list":
            path = parent.path + [("index", parent.index)]
            line_map[join_path(path)] = line
            parent.index += 1
            stack.append(_Frame(kind, path))
        else:
            stack.append(_Frame(kind, parent.path))

    while i < n:
        tok_type, tok_raw, tok_line = tokens[i]
        if tok_type == _TOKEN_STRING:
            # A string is a key when the *next* non-EOF token is a colon.
            nxt = tokens[i + 1][0] if i + 1 < n else _TOKEN_EOF
            if nxt == _TOKEN_COLON and stack and stack[-1].kind == "dict":
                stack[-1].pending_key = tok_raw
                stack[-1].expects_value = True
            else:
                consume_value(tok_line)
        elif tok_type in (_TOKEN_NUMBER, _TOKEN_LITERAL):
            consume_value(tok_line)
        elif tok_type == _TOKEN_LBRACE:
            push_frame("dict", tok_line)
        elif tok_type == _TOKEN_LBRACKET:
            push_frame("list", tok_line)
        elif tok_type == _TOKEN_RBRACE:
            if stack:
                stack.pop()
        elif tok_type == _TOKEN_RBRACKET:
            if stack:
                stack.pop()
        elif tok_type == _TOKEN_COMMA:
            # Nothing to do: list indices advance when the next value lands.
            pass
        elif tok_type == _TOKEN_COLON:
            pass
        i += 1
    return line_map


# ---------------------------------------------------------------------------
# INI: line-oriented
# ---------------------------------------------------------------------------

def _build_ini_line_map(text: str) -> Dict[str, int]:
    line_map: Dict[str, int] = {}
    section: List[Tuple[str, Any]] = []
    for i, raw in enumerate(text.split("\n")):
        line_no = i + 1
        stripped = raw.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        if stripped.startswith("["):
            end = stripped.find("]")
            if end > 0:
                name = stripped[1:end].strip()
                section = [("key", name)]
            continue
        m = re.match(r"^([^=:]+?)\s*[=:]\s*(.*)$", stripped)
        if m:
            key = m.group(1).strip()
            if key:
                line_map[join_path(section + [("key", key)])] = line_no
    return line_map


# ---------------------------------------------------------------------------
# TOML: line-oriented (table headers, dotted keys, inline tables, arrays)
# ---------------------------------------------------------------------------

def _split_toml_key(key_part: str) -> List[str]:
    """Split a TOML key (dotted or quoted) into unquoted segments."""
    segments: List[str] = []
    buf: List[str] = []
    i = 0
    n = len(key_part)
    quote: Optional[str] = None
    while i < n:
        ch = key_part[i]
        if quote:
            if ch == "\\" and quote == '"' and i + 1 < n:
                buf.append(key_part[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
                i += 1
                continue
            buf.append(ch)
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        if ch == ".":
            segments.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    if buf or not segments:
        segments.append("".join(buf))
    return [s for s in segments if s != ""]


def _toml_top_level_split(text: str, sep: str = ",") -> List[str]:
    """Split TOML text on ``sep`` at the top nesting level.

    Skips separators inside single/double quoted strings (including triple
    quotes) and inside ``[]`` / ``{}`` brackets.
    """
    parts: List[str] = []
    buf: List[str] = []
    quote: Optional[str] = None
    triple = False
    depth = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if quote:
            if triple and text.startswith(quote * 3, i):
                buf.append(quote * 3)
                i += 3
                quote = None
                triple = False
                continue
            if not triple and ch == "\\" and quote == '"' and i + 1 < n:
                buf.append(text[i : i + 2])
                i += 2
                continue
            if not triple and ch == quote:
                buf.append(ch)
                quote = None
                i += 1
                continue
            buf.append(ch)
            i += 1
            continue
        if ch in ("'", '"'):
            if text.startswith(ch * 3, i):
                quote = ch
                triple = True
                buf.append(ch * 3)
                i += 3
            else:
                quote = ch
                buf.append(ch)
                i += 1
            continue
        if ch in "[{":
            depth += 1
            buf.append(ch)
            i += 1
            continue
        if ch in "]}":
            depth -= 1
            buf.append(ch)
            i += 1
            continue
        if ch == sep and depth == 0:
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return parts


def _record_toml_value(
    line_map: Dict[str, int],
    parts: List[Tuple[str, Any]],
    value_text: str,
    value_line: int,
    lines: List[str],
) -> None:
    """Record line numbers for a TOML value.

    ``parts`` is the path of the key the value belongs to.  ``value_line`` is
    the line where the value's first token starts.  Recurses into inline
    tables and arrays (single- and multi-line).
    """
    stripped = value_text.strip()
    if not stripped:
        return
    if stripped.startswith("{"):
        _record_toml_inline_table(line_map, parts, stripped, value_line)
        return
    if stripped.startswith("["):
        # Locate the matching closing bracket across the physical lines.
        collected = stripped
        li = value_line - 1
        while not _brackets_balanced(collected) and li + 1 < len(lines):
            li += 1
            collected += "\n" + lines[li]
        _record_toml_array(line_map, parts, collected, value_line)
        return


def _brackets_balanced(text: str) -> bool:
    """Cheap balance check for ``[]`` / ``{}`` ignoring strings."""
    depth = 0
    quote: Optional[str] = None
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if quote:
            if ch == "\\" and quote == '"' and i + 1 < n:
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            if text.startswith(ch * 3, i):
                i += 3
            else:
                quote = ch
                i += 1
            continue
        if ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        i += 1
    return depth <= 0


def _record_toml_inline_table(
    line_map: Dict[str, int],
    parts: List[Tuple[str, Any]],
    table_text: str,
    table_line: int,
) -> None:
    """Record keys of an inline table ``{ k1 = v1, k2 = v2 }``."""
    inner = table_text.strip()
    if inner.startswith("{") and inner.endswith("}"):
        inner = inner[1:-1]
    for pair in _toml_top_level_split(inner):
        pair = pair.strip()
        if not pair:
            continue
        eq = _find_toml_eq(pair)
        if eq is None:
            continue
        key_part = pair[:eq].strip()
        value_part = pair[eq + 1 :].strip()
        key_segments = _split_toml_key(key_part)
        full = parts + [("key", k) for k in key_segments]
        line_map[join_path(full)] = table_line
        _record_toml_value(line_map, full, value_part, table_line, [])


def _record_toml_array(
    line_map: Dict[str, int],
    parts: List[Tuple[str, Any]],
    array_text: str,
    array_line: int,
) -> None:
    """Record elements of a (possibly multi-line) TOML array.

    Each element maps to ``parts[i]`` at the line where its first token
    starts; nested arrays / inline tables recurse with the extended path.
    """
    inner = array_text.strip()
    if inner.startswith("[") and inner.endswith("]"):
        inner = inner[1:-1]
    if not inner.strip():
        return
    lines = inner.split("\n")
    # Compute the 1-based physical line of each top-level element.
    element_texts = _toml_top_level_split(inner)
    consumed = 0
    for elem_raw in element_texts:
        elem = elem_raw.strip()
        if not elem:
            consumed = _find_nth_non_ws(inner, consumed) + len(elem_raw)
            continue
        # Find where this element actually starts inside ``inner``.
        start_in_inner = _find_nth_non_ws(inner, consumed)
        elem_line = _line_of_offset(lines, start_in_inner, array_line)
        idx = _current_index(line_map, parts)
        full = parts + [("index", idx)]
        line_map[join_path(full)] = elem_line
        if elem.startswith("{"):
            _record_toml_inline_table(line_map, full, elem, elem_line)
        elif elem.startswith("["):
            _record_toml_array(line_map, full, elem, elem_line)
        # Advance past the element (raw length) and any separating comma so
        # the next element starts at its first non-whitespace character.
        consumed = start_in_inner + len(elem_raw)
        while consumed < len(inner) and inner[consumed] in " \t\r\n,":
            consumed += 1
    # The key itself was already recorded by the caller; nothing else to do.


def _find_nth_non_ws(text: str, offset: int) -> int:
    """Offset of the next non-whitespace char at/after ``offset`` in ``text``."""
    i = offset
    n = len(text)
    while i < n and text[i] in " \t\r\n":
        i += 1
    return i


def _line_of_offset(lines: List[str], offset: int, base_line: int) -> int:
    """Map a character offset inside ``lines`` to a 1-based physical line."""
    remaining = offset
    for li, ln in enumerate(lines):
        if remaining <= len(ln):
            return base_line + li
        remaining -= len(ln) + 1  # +1 for the '\n' that split the lines
    return base_line + max(0, len(lines) - 1)


def _current_index(line_map: Dict[str, int], parts: List[Tuple[str, Any]]) -> int:
    """Next unused array index for ``parts`` (monotonic per path)."""
    idx = 0
    while True:
        probe = join_path(parts + [("index", idx)])
        if probe not in line_map:
            return idx
        idx += 1


def _find_toml_eq(text: str) -> Optional[int]:
    """Index of the first top-level ``=`` in a TOML key/value pair."""
    quote: Optional[str] = None
    depth = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if quote:
            if ch == "\\" and quote == '"' and i + 1 < n:
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            if text.startswith(ch * 3, i):
                i += 3
            else:
                quote = ch
                i += 1
            continue
        if ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == "=" and depth == 0:
            return i
        i += 1
    return None


def _build_toml_line_map(text: str) -> Dict[str, int]:
    line_map: Dict[str, int] = {}
    lines = text.split("\n")
    table_parts: List[Tuple[str, Any]] = []
    # TOML forbids duplicate tables; [[a.b]] produces list[dict], so keep the
    # count per table path to emit ``a.b[i].x`` keys that match the tree.
    array_counts: Dict[str, int] = {}

    for i, raw in enumerate(lines):
        line_no = i + 1
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("[["):
            end = stripped.find("]]")
            if end < 0:
                continue
            name = stripped[2:end].strip()
            segments = _split_toml_key(name)
            key = ".".join(segments)
            idx = array_counts.get(key, 0)
            array_counts[key] = idx + 1
            table_parts = [("key", s) for s in segments] + [("index", idx)]
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            name = stripped[1:-1].strip()
            table_parts = [("key", s) for s in _split_toml_key(name)]
            continue
        eq = _find_toml_eq(stripped)
        if eq is None:
            continue
        key_part = stripped[:eq].strip()
        value_part = stripped[eq + 1 :].strip()
        key_segments = _split_toml_key(key_part)
        full = table_parts + [("key", k) for k in key_segments]
        line_map[join_path(full)] = line_no
        _record_toml_value(line_map, full, value_part, line_no, lines)
    return line_map


# ---------------------------------------------------------------------------
# YAML: yaml.parse event stream
# ---------------------------------------------------------------------------

def _build_yaml_line_map(text: str) -> Dict[str, int]:
    try:
        import yaml
    except ImportError:  # pragma: no cover - YAML is a hard dependency
        return {}

    line_map: Dict[str, int] = {}
    # Frames: ("map", path, pending_key) | ("seq", path, index)
    stack: List[Tuple[str, List[Tuple[str, Any]], Any]] = []

    def consume_value(line: int) -> None:
        """Record a scalar value for the current map key / seq index."""
        nonlocal stack
        if not stack:
            line_map["$"] = line
            return
        kind, path, state = stack[-1]
        if kind == "map" and state is not None:
            line_map[join_path(path + [("key", state)])] = line
            stack[-1] = (kind, path, None)
        elif kind == "seq":
            line_map[join_path(path + [("index", state)])] = line
            stack[-1] = (kind, path, state + 1)

    def push_frame(kind: str, line: int) -> None:
        """Push a map/seq frame, consuming the parent's pending value.

        The consumed key/index is used to build the new frame's own path
        *here* (before the parent state is cleared), mirroring the JSON
        builder.
        """
        nonlocal stack
        if not stack:
            if kind == "seq":
                stack.append(("seq", [("key", "$")], 0))
            else:
                stack.append(("map", [], None))
            return
        parent_kind, parent_path, parent_state = stack[-1]
        if parent_kind == "map" and parent_state is not None:
            path = parent_path + [("key", parent_state)]
            line_map[join_path(path)] = line
            stack[-1] = (parent_kind, parent_path, None)
            stack.append((kind, path, 0 if kind == "seq" else None))
        elif parent_kind == "seq":
            path = parent_path + [("index", parent_state)]
            line_map[join_path(path)] = line
            stack[-1] = (parent_kind, parent_path, parent_state + 1)
            stack.append((kind, path, 0 if kind == "seq" else None))
        else:
            stack.append((kind, parent_path, 0 if kind == "seq" else None))

    try:
        events = list(yaml.parse(text))
    except yaml.YAMLError as exc:  # pragma: no cover - parse already validated
        logger.warning("yaml.parse failed in line map: %s", exc)
        return {}

    for event in events:
        name = type(event).__name__
        if name == "MappingStartEvent":
            line = event.start_mark.line + 1 if event.start_mark else 1
            push_frame("map", line)
        elif name == "SequenceStartEvent":
            line = event.start_mark.line + 1 if event.start_mark else 1
            push_frame("seq", line)
        elif name == "MappingEndEvent" or name == "SequenceEndEvent":
            if stack:
                stack.pop()
        elif name == "ScalarEvent":
            line = event.start_mark.line + 1 if event.start_mark else 1
            if stack:
                kind, path, state = stack[-1]
                if kind == "map":
                    if state is None:
                        # This scalar is a key.
                        stack[-1] = ("map", path, event.value)
                    else:
                        consume_value(line)
                else:  # seq
                    consume_value(line)
            else:
                # Top-level scalar -> the "$" pseudo-key.
                line_map["$"] = line
        # AliasEvent / other events carry no new structural info.
    return line_map
