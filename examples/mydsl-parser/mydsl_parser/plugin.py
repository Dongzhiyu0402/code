"""mydsl — an example cfgdrift parser plugin (nginx-like DSL).

This module implements the v0.5.0 plugin protocol from
``cfgdrift.core.plugins``:

- :func:`parse` returns a **raw** tree (dict / list / scalar).  cfgdrift
  normalizes it through the same ``_normalize`` / ``_wrap_top_level`` path
  used by the built-in JSON / YAML / TOML / INI backends, so semantic-tree
  contracts are preserved (keys become ``str``, top-level non-dict values are
  wrapped as ``{"$": ...}``, etc.).
- :func:`build_line_map` returns ``{key_path: 1-based line}`` using cfgdrift's
  key-path convention (:func:`cfgdrift.core.model.join_path`): dictionary
  segments joined with ``.``, list indices appended as ``[i]``, and segments
  containing ``.`` / ``[`` / ``]`` / ``\\`` backslash-escaped.

DSL subset (documented in ``examples/mydsl-parser/README.md``):

- ``#`` starts a comment (whole-line or trailing).
- ``key value1 value2 ...;`` — a statement.  The first token is the key; the
  remaining tokens form the value (whitespace-split with single/double quoted
  spans kept together):
  - one token   -> scalar (int/float when numeric; quoted strings are
    unquoted; otherwise the raw string),
  - many tokens -> list of scalars (e.g. ``server_name a.com b.com;``),
  - no tokens   -> ``None`` (null).
- ``type name? { ... }`` — a block:
  - nameless (``server {``): stored under ``type`` and **always** a list of
    dicts (nginx ``server`` semantics), so ``server[0]`` paths are stable;
  - named (``location /api {``): grouped under ``type`` as
    ``{name: {...}}``; repeated type+name becomes a list of dicts.
- Statements must fit on one line and ``{`` / ``}`` must be on their own
  lines; unbalanced braces raise ``ValueError`` naming the offending line.
- Scalar statements with a duplicated key are last-wins (matching JSON / INI
  duplicate-key behavior); overwriting a block with a scalar raises.

Both registration paths are supported:

- **Entry point** (``pyproject.toml``): ``cfgdrift.parsers`` exposes the
  module-level :data:`plugin` instance, discovered automatically when
  ``cfgdrift`` imports.
- **Decorator** (import time): the :func:`register_plugin` decorator below
  registers the same ``mydsl`` name into the shared default registry, so
  merely ``import mydsl_parser`` makes ``--format mydsl`` available
  in-process without installing the entry point.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from cfgdrift.core.model import join_path
from cfgdrift.core.plugins import ParserPlugin, register_plugin

# ---------------------------------------------------------------------------
# Scalar coercion
# ---------------------------------------------------------------------------


def _to_scalar(token: str) -> Any:
    """Coerce one raw token to int / float / unquoted string."""
    token = token.strip()
    if not token:
        return ""
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
        return token[1:-1]
    try:
        return int(token)
    except ValueError:
        pass
    try:
        return float(token)
    except ValueError:
        pass
    return token


def _split_tokens(raw: str) -> List[str]:
    """Split ``raw`` on whitespace, keeping quoted spans together."""
    tokens: List[str] = []
    buf: List[str] = []
    quote: Optional[str] = None
    for ch in raw:
        if quote is not None:
            buf.append(ch)
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            buf.append(ch)
        elif ch.isspace():
            if buf:
                tokens.append("".join(buf))
                buf = []
        else:
            buf.append(ch)
    if buf:
        tokens.append("".join(buf))
    return tokens


def _parse_value(raw: str) -> Any:
    """Parse the value part of a statement into a scalar or list of scalars."""
    raw = raw.strip()
    if not raw:
        return None
    tokens = _split_tokens(raw)
    if len(tokens) == 1:
        return _to_scalar(tokens[0])
    return [_to_scalar(tok) for tok in tokens]


def _strip_comment(line: str) -> str:
    """Strip a trailing ``#`` comment (naive; no escaping support)."""
    idx = line.find("#")
    return line[:idx] if idx >= 0 else line


# ---------------------------------------------------------------------------
# Line bookkeeping (mirrors the parsed tree so the line map stays consistent)
# ---------------------------------------------------------------------------


class _LineInfo:
    """Line bookkeeping for one node: statement/header line + child lines.

    ``children`` mirrors the parsed tree:

    - ``None`` for scalar / scalar-list leaves,
    - ``dict[str, _LineInfo]`` for dict blocks (including named-block groups
      like ``location``),
    - ``list[_LineInfo]`` for block arrays (e.g. repeated ``server``).
    """

    __slots__ = ("line", "children")

    def __init__(self, line: int, children=None) -> None:
        self.line = line
        self.children = children


def _insert(
    current: Dict[str, Any],
    lines: Dict[str, _LineInfo],
    key: str,
    value: Any,
    info: _LineInfo,
    line_no: int,
) -> None:
    """Insert a scalar / scalar-list / named-block value under ``key``.

    Scalar / scalar-list values are last-wins (JSON / INI duplicate-key
    behavior).  Dict values (named blocks) are converted to an array when the
    same key is opened again.  Overwriting an existing block with a scalar
    raises ``ValueError``.
    """
    if key not in current:
        current[key] = value
        lines[key] = info
        return
    existing = current[key]
    if isinstance(value, dict):
        if isinstance(existing, dict):
            current[key] = [existing, value]
            lines[key] = [lines[key], info]
        elif (
            isinstance(existing, list)
            and existing
            and isinstance(existing[-1], dict)
        ):
            current[key].append(value)  # type: ignore[union-attr]
            lines[key].append(info)  # type: ignore[union-attr]
        else:
            raise ValueError(
                "cannot open block %r: a scalar value exists (line %d)"
                % (key, line_no)
            )
    else:
        if isinstance(existing, (dict, list)):
            raise ValueError(
                "cannot overwrite block %r with a scalar value (line %d)"
                % (key, line_no)
            )
        current[key] = value
        lines[key] = info


def _insert_nameless_block(
    current: Dict[str, Any],
    lines: Dict[str, _LineInfo],
    key: str,
    node: Dict[str, Any],
    info: _LineInfo,
    line_no: int,
) -> None:
    """Insert a nameless block; the value is **always** a list of dicts.

    ``server { ... }`` -> ``current["server"] = [{...}]``, and a second
    ``server { ... }`` appends, so key paths like ``server[0].listen`` are
    stable whether the config has one or many blocks.
    """
    if key not in current:
        current[key] = [node]
        lines[key] = [info]
        return
    existing = current[key]
    if (
        isinstance(existing, list)
        and existing
        and isinstance(existing[-1], dict)
    ):
        current[key].append(node)  # type: ignore[union-attr]
        lines[key].append(info)  # type: ignore[union-attr]
    else:
        raise ValueError(
            "cannot open block %r: a scalar value exists (line %d)"
            % (key, line_no)
        )


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def parse(text: str) -> Dict[str, Any]:
    """Parse nginx-like DSL text into a raw semantic tree.

    Raises ``ValueError`` with a 1-based line number on unbalanced braces or
    malformed statements — the CLI reports the message and exits with code 2.
    """
    tree, _ = _parse_with_lines(text)
    return tree


def build_line_map(text: str) -> Dict[str, int]:
    """Return ``{key_path: 1-based line}`` for ``text``.

    Key paths follow :func:`cfgdrift.core.model.join_path` so the differ can
    resolve ``file:line`` locations for every emitted key.
    """
    _, line_map = _parse_with_lines(text)
    return line_map


def _parse_with_lines(
    text: str,
) -> Tuple[Dict[str, Any], Dict[str, int]]:
    """Parse ``text`` into ``(tree, line_map)`` in a single pass."""
    tree: Dict[str, Any] = {}
    lines: Dict[str, _LineInfo] = {}
    # Stack frames: (container dict, mirror lines dict, opening line).
    stack: List[Tuple[Dict[str, Any], Dict[str, _LineInfo], int]] = []
    current = tree
    current_lines = lines

    for line_no, raw in enumerate(text.splitlines(), 1):
        line = _strip_comment(raw).strip()
        if not line:
            continue
        if line.endswith("{"):
            header = line[:-1].strip()
            words = header.split()
            if not words:
                raise ValueError("empty block header at line %d" % line_no)
            block_type = words[0]
            block_name = " ".join(words[1:]) if len(words) > 1 else None
            node: Dict[str, Any] = {}
            info = _LineInfo(line_no, {})
            if block_name is not None:
                # Named block -> group under current[type][name].
                if block_type not in current:
                    current[block_type] = {}
                    current_lines[block_type] = _LineInfo(line_no, {})
                elif not isinstance(current[block_type], dict):
                    raise ValueError(
                        "cannot open named block %r: %r is not a block group "
                        "at line %d" % (header, block_type, line_no)
                    )
                group = current[block_type]
                group_lines = current_lines[block_type].children
                _insert(group, group_lines, block_name, node, info, line_no)
                stack.append((current, current_lines, line_no))
                current = node
                current_lines = info.children
            else:
                # Nameless block -> stored directly under current[type].
                _insert_nameless_block(
                    current, current_lines, block_type, node, info, line_no
                )
                stack.append((current, current_lines, line_no))
                current = node
                current_lines = info.children
        elif line == "}":
            if not stack:
                raise ValueError("unbalanced '}' at line %d" % line_no)
            current, current_lines, _ = stack.pop()
        else:
            stmt = line[:-1].strip() if line.endswith(";") else line
            if not stmt:
                continue
            if "{" in stmt or "}" in stmt:
                raise ValueError("malformed statement at line %d" % line_no)
            parts = stmt.split(None, 1)
            key = parts[0]
            raw_val = parts[1].strip() if len(parts) > 1 else ""
            value = _parse_value(raw_val)
            _insert(current, current_lines, key, value, _LineInfo(line_no), line_no)

    if stack:
        raise ValueError(
            "unbalanced '{' at line %d" % stack[-1][2]
        )

    line_map = _emit_line_map(tree, lines, [], {})
    return tree, line_map


def _emit_line_map(
    tree: Dict[str, Any],
    lines_node: Dict[str, _LineInfo],
    path: List[Tuple[str, Any]],
    line_map: Dict[str, int],
) -> Dict[str, int]:
    """Walk the parsed tree + mirror and emit ``{key_path: line}`` entries."""
    for key, value in tree.items():
        info = lines_node[key]
        key_path = path + [("key", key)]
        if isinstance(value, list) and value and isinstance(value[0], dict):
            # Block array: ``info`` mirrors the list of blocks.
            for i, item in enumerate(value):
                idx_path = key_path + [("index", i)]
                item_info = info[i]
                line_map[join_path(idx_path)] = item_info.line
                _emit_line_map(item, item_info.children, idx_path, line_map)
        elif isinstance(value, dict):
            line_map[join_path(key_path)] = info.line
            _emit_line_map(value, info.children, key_path, line_map)
        elif isinstance(value, list):
            # Scalar list: the key and every index map to the statement line.
            line_map[join_path(key_path)] = info.line
            for i in range(len(value)):
                line_map[join_path(key_path + [("index", i)])] = info.line
        else:
            line_map[join_path(key_path)] = info.line
    return line_map


# ---------------------------------------------------------------------------
# Plugin instance + both registration paths
# ---------------------------------------------------------------------------

#: Shared plugin instance — the value exposed by the ``cfgdrift.parsers``
#: entry point in ``pyproject.toml`` (``mydsl = "mydsl_parser:plugin"``).
plugin = ParserPlugin(
    name="mydsl",
    extensions=(".dsl",),
    parse=parse,
    build_line_map=build_line_map,
)


@register_plugin("mydsl", extensions=(".dsl",), line_map=build_line_map)
def _decorator_registered_parse(text: str) -> Dict[str, Any]:
    """Import-time registration hook (decorator path).

    Importing this module registers the ``mydsl`` name into cfgdrift's shared
    default registry, so the plugin works without the entry point.  When both
    paths are active the entry point wins (``replace=True``) — harmless here
    because they expose identical behavior.
    """
    return parse(text)
