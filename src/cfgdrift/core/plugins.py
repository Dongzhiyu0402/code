"""Parser plugin registry (v0.5.0).

Custom config formats are supported through a small plugin protocol:

- :class:`ParserPlugin` — one parser: ``name`` (the ``--format`` value),
  ``extensions`` (lower-case, dot-prefixed; used by ``detect_format``),
  ``parse(text) -> raw tree`` (any dict / list / scalar; normalization is
  still done by :mod:`cfgdrift.core.parser`), and an **optional**
  ``build_line_map(text) -> {key_path: line}`` (when omitted line numbers
  are ``None`` — D10).
- :class:`PluginRegistry` — registration / lookup by name or extension.
- :func:`register_plugin` — decorator for in-process registration (import
  time).
- :func:`discover_entry_points` — loads third-party plugins declared through
  the ``cfgdrift.parsers`` entry point group (Python 3.8+ stdlib
  ``importlib.metadata``, D11).  Entry points take precedence over
  decorator-registered plugins with the same name (``replace=True``, Q5).

All loading failures are isolated per plugin (warning + continue) so a broken
plugin never affects the built-in parsers.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("cfgdrift.core.plugins")

#: Names reserved for the four built-in formats.  ``custom_names()`` excludes
#: them so ``validate_format`` only appends genuinely custom plugin names.
BUILTIN_PLUGIN_NAMES: Tuple[str, ...] = ("json", "yaml", "toml", "ini")

_ENTRY_POINT_GROUP = "cfgdrift.parsers"


class ParserPlugin:
    """One custom config parser.

    ``parse`` returns a **raw** parsed value (dict / list / scalar).  The
    engine normalizes it through ``_normalize`` / ``_wrap_top_level`` exactly
    like the built-in backends, so semantic-tree contracts are preserved.
    ``build_line_map`` is optional; when absent line numbers are ``None``.
    """

    def __init__(
        self,
        name: str,
        extensions: Tuple[str, ...] = (),
        parse: Optional[Callable[[str], Any]] = None,
        build_line_map: Optional[Callable[[str], Dict[str, int]]] = None,
    ) -> None:
        if not name or not isinstance(name, str):
            raise ValueError("parser plugin name must be a non-empty string")
        if parse is None or not callable(parse):
            raise ValueError(
                "parser plugin %r requires a callable 'parse' function" % name
            )
        self.name = str(name)
        self.extensions: Tuple[str, ...] = tuple(
            str(ext).lower() for ext in (extensions or ())
        )
        self._parse_fn = parse
        self._line_map_fn = build_line_map

    def parse(self, text: str) -> Any:
        """Parse ``text`` into a raw value (no normalization)."""
        return self._parse_fn(text)

    def build_line_map(self, text: str) -> Dict[str, int]:
        """Return ``{key_path: line}``; ``{}`` when no mapping is provided.

        A broken mapping never blocks parsing: failures are downgraded to a
        warning and an empty map (line numbers are enrichment only).
        """
        if self._line_map_fn is None:
            return {}
        try:
            result = self._line_map_fn(text)
            if isinstance(result, dict):
                return {str(k): int(v) for k, v in result.items()}
            logger.warning(
                "plugin %s build_line_map returned %r (expected dict); "
                "using empty map",
                self.name,
                type(result).__name__,
            )
            return {}
        except Exception as exc:  # noqa: BLE001 - enrichment must not block
            logger.warning(
                "plugin %s build_line_map failed: %s; using empty map",
                self.name,
                exc,
            )
            return {}


class PluginRegistry:
    """Name/extension -> :class:`ParserPlugin` registry."""

    def __init__(self) -> None:
        self._plugins: Dict[str, ParserPlugin] = {}
        self._ext_index: Dict[str, str] = {}

    # -- registration ----------------------------------------------------

    def register(self, plugin: ParserPlugin, replace: bool = False) -> None:
        """Register a plugin.

        Duplicate names raise ``ValueError`` unless ``replace=True`` (used by
        entry-point loading so entry points override decorator registration).
        """
        if not isinstance(plugin, ParserPlugin):
            raise TypeError(
                "plugin must be a ParserPlugin instance, got %s"
                % type(plugin).__name__
            )
        if plugin.name in self._plugins and not replace:
            raise ValueError(
                "parser plugin %r is already registered (use replace=True "
                "to overwrite)" % plugin.name
            )
        self._plugins[plugin.name] = plugin
        for ext in plugin.extensions:
            self._ext_index[ext] = plugin.name

    # -- lookup ----------------------------------------------------------

    def get(self, name: str) -> Optional[ParserPlugin]:
        """Return the plugin registered under ``name`` (or None)."""
        return self._plugins.get(name)

    def by_extension(self, ext: str) -> Optional[str]:
        """Return the plugin name owning a file extension (or None).

        ``ext`` must be dot-prefixed and lower-case (callers normalize).
        """
        return self._ext_index.get(str(ext).lower())

    def names(self) -> List[str]:
        """Return all registered plugin names (built-ins + custom), sorted."""
        return sorted(self._plugins)

    def custom_names(self) -> List[str]:
        """Return custom (non-built-in) plugin names, sorted.

        Used by :func:`cfgdrift.core.parser.validate_format` to extend the
        accepted ``--format`` values.
        """
        return sorted(
            name for name in self._plugins if name not in BUILTIN_PLUGIN_NAMES
        )

    # -- entry points ----------------------------------------------------

    def load_entry_points(self, group: str = _ENTRY_POINT_GROUP) -> int:
        """Discover and register plugins from an entry point group.

        Returns the number of successfully loaded plugins.  Failures are
        logged as warnings and never raise — a broken plugin must not affect
        the built-in parsers.
        """
        try:
            from importlib import metadata

            eps = metadata.entry_points()
        except Exception as exc:  # noqa: BLE001 - discovery is best-effort
            logger.warning("entry point discovery failed: %s", exc)
            return 0
        if hasattr(eps, "select"):  # Python 3.10+
            selected = list(eps.select(group=group))
        else:  # Python 3.8 / 3.9: dict-like mapping
            selected = list(eps.get(group, []))
        count = 0
        for ep in selected:
            name = getattr(ep, "name", "?")
            try:
                obj = ep.load()
                plugin = _coerce_entry_point(obj, name)
                self.register(plugin, replace=True)  # entry point wins (Q5)
                count += 1
                logger.info("loaded parser plugin %r (entry point)", name)
            except Exception as exc:  # noqa: BLE001 - per-plugin isolation
                logger.warning(
                    "failed to load parser plugin %r via entry point: %s",
                    name,
                    exc,
                )
        return count


def _coerce_entry_point(obj: Any, name: str) -> ParserPlugin:
    """Normalize an entry point value into a :class:`ParserPlugin`.

    Supported shapes (``docs/system_design_v050.md`` §1.5):

    - a :class:`ParserPlugin` instance;
    - a ``(parse_fn, {"extensions": ..., "line_map": ...})`` tuple;
    - a plain callable ``parse(text)`` (name = entry point name);
    - a ``{"parse": fn, "extensions": [...], "line_map": fn}`` mapping.
    """
    if isinstance(obj, ParserPlugin):
        return obj
    if isinstance(obj, tuple) and len(obj) == 2 and callable(obj[0]):
        parse_fn, opts = obj
        if isinstance(opts, dict):
            return ParserPlugin(
                name=opts.get("name", name),
                extensions=opts.get("extensions", ()),
                parse=parse_fn,
                build_line_map=opts.get("line_map"),
            )
    if callable(obj):
        return ParserPlugin(name=name, parse=obj)
    if isinstance(obj, dict):
        parse_fn = obj.get("parse")
        if callable(parse_fn):
            return ParserPlugin(
                name=obj.get("name", name),
                extensions=obj.get("extensions", ()),
                parse=parse_fn,
                build_line_map=obj.get("line_map"),
            )
    raise TypeError(
        "entry point %r did not resolve to a parser plugin "
        "(expected ParserPlugin, (parse_fn, opts) or callable)" % name
    )


# ---------------------------------------------------------------------------
# Module-level default registry + helpers
# ---------------------------------------------------------------------------

default_registry = PluginRegistry()


def register_plugin(
    name: Optional[str] = None,
    extensions: Tuple[str, ...] = (),
    line_map: Optional[Callable[[str], Dict[str, int]]] = None,
    registry: Optional[PluginRegistry] = None,
):
    """Register a ``parse(text)`` function as a parser plugin (decorator).

    Usage::

        @register_plugin("mydsl", extensions=(".dsl",), line_map=build_my_lines)
        def parse_mydsl(text):
            return {...}

    Bare ``@register_plugin`` is also supported (name = function name).
    """
    if callable(name):  # bare @register_plugin
        fn = name
        plugin = ParserPlugin(
            name=fn.__name__, extensions=(), parse=fn, build_line_map=line_map
        )
        (registry or default_registry).register(plugin)
        return fn

    def decorator(fn: Callable[[str], Any]) -> Callable[[str], Any]:
        plugin_name = name if name is not None else fn.__name__
        plugin = ParserPlugin(
            name=plugin_name,
            extensions=extensions,
            parse=fn,
            build_line_map=line_map,
        )
        (registry or default_registry).register(plugin)
        return fn

    return decorator


def discover_entry_points(
    group: str = _ENTRY_POINT_GROUP,
    registry: Optional[PluginRegistry] = None,
) -> int:
    """Discover parser plugins from the ``cfgdrift.parsers`` entry point group.

    Best-effort: failures are logged as warnings and never raise.  Returns
    the number of plugins loaded.
    """
    reg = registry if registry is not None else default_registry
    return reg.load_entry_points(group)
