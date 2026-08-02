"""setuptools build configuration for cfgdrift.

Compiles the optional C extension ``cfgdrift._cfgdrift`` from the sources in
``src/csrc`` when a C compiler is available.  The extension implements
JSON/TOML/INI parsing; YAML is handled on the Python side by PyYAML.

v0.2.0 distribution model (see docs/system_design.md):

- ``Extension(..., optional=True)``: a failed C compile only warns and the
  install continues, producing a pure-Python ``py3-none-any`` wheel.
- ``CFGDRIFT_NO_C=1``: deterministically skip the C extension so ``python -m
  build --wheel`` emits the universal wheel.
"""

import os
import sys

from setuptools import Extension, setup

_c_sources = [
    "src/csrc/parser_core.c",
    "src/csrc/parser_json.c",
    "src/csrc/parser_toml.c",
    "src/csrc/parser_ini.c",
]

# MSVC reads source files with the system code page by default; the C sources
# contain UTF-8 comments, so ask for UTF-8 explicitly (silences C4819 and
# keeps the compiler's view of the source correct).
_extra_compile_args = []
if sys.platform == "win32":
    _extra_compile_args.append("/utf-8")

_ext_modules = []
if os.environ.get("CFGDRIFT_NO_C", "").strip().lower() not in ("1", "true", "yes"):
    try:
        _ext_modules.append(
            Extension(
                "cfgdrift._cfgdrift",
                sources=_c_sources,
                include_dirs=["src/csrc"],
                language="c",
                extra_compile_args=_extra_compile_args,
                optional=True,
            )
        )
    except Exception:  # pragma: no cover - build-environment edge cases
        _ext_modules = []

setup(ext_modules=_ext_modules)
