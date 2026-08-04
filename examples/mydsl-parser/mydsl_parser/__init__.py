"""mydsl-parser — an example cfgdrift parser plugin (nginx-like DSL).

Importing this package registers the ``mydsl`` parser in-process (decorator
path); the ``cfgdrift.parsers`` entry point in ``pyproject.toml`` exposes the
same :data:`mydsl_parser.plugin.plugin` instance for pip-installed discovery.
"""

from .plugin import build_line_map, parse, plugin

__all__ = ["parse", "build_line_map", "plugin"]
__version__ = "0.1.0"
