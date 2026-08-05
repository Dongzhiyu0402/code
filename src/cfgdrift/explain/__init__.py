"""Business-impact narrative generation (v0.8.0, direction A).

The package provides a deterministic template narrative engine plus an
optional LLM enhancement backend:

- :mod:`cfgdrift.explain.templates` — built-in key-semantics dictionary
  (24+ patterns) and the deterministic :class:`TemplateEngine`.
- :mod:`cfgdrift.explain.validator` — evidence-chain anti-hallucination
  (:class:`EvidenceValidator` + :func:`build_facts`).
- :mod:`cfgdrift.explain.llm` — OpenAI-compatible REST backend (stdlib
  ``urllib`` only, no third-party dependency).
- :mod:`cfgdrift.explain.engine` — :class:`ExplainEngine` orchestrating the
  template-first / LLM-enhanced / fallback pipeline.

The package version reuses ``cfgdrift.__version__`` (no separate version).
"""

from .engine import ExplainEngine, NarrativeItem  # noqa: F401
from .llm import LLMBackend, OpenAICompatBackend  # noqa: F401
from .templates import KEY_SEMANTICS, TemplateEngine  # noqa: F401
from .validator import EvidenceValidator, build_facts  # noqa: F401

__all__ = [
    "KEY_SEMANTICS",
    "EvidenceValidator",
    "ExplainEngine",
    "LLMBackend",
    "NarrativeItem",
    "OpenAICompatBackend",
    "TemplateEngine",
    "build_facts",
]
