"""cfgdrift corpus benchmark toolchain (v0.7.0).

Sub-packages / modules:
- :mod:`cfgdrift.corpus.config` — corpus.yaml load/save/validate (``CorpusConfig``).
- :mod:`cfgdrift.corpus.workspace` — workspace directory layout + state.json.
- :mod:`cfgdrift.corpus.fetcher` — git history sources + change-pair extraction.
- :mod:`cfgdrift.corpus.exporter` — change pairs -> normalized JSONL instances.
- :mod:`cfgdrift.corpus.validator` — JSONL schema validation + statistics.

The package reuses ``cfgdrift.__version__``; it does not define its own
version.
"""
