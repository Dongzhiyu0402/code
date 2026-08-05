"""Explain engine: template-first, LLM-enhanced, evidence-validated (v0.8.0).

:class:`ExplainEngine.generate` implements the P0 pipeline:

1. every drift item is rendered by :class:`TemplateEngine` first
   (``source: template`` — deterministic, offline);
2. when an ``llm_backend`` is available and LLM use is not disabled, a
   prompt containing the fact whitelist + JSON contract is sent;
3. a successful JSON array reply is validated per item by
   :class:`EvidenceValidator`; valid items get their ``impact`` /
   ``evidence`` replaced with ``source: llm``;
4. any failure (no key / timeout / HTTP error / parse failure / evidence
   violation) keeps the template narrative with ``source: template`` and
   logs the reason — never a partial adoption.

``drift_items`` are the *masked* item dicts (the CLI applies
:class:`SensitiveMasker` first, D7) so narratives never leak sensitive
values.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .llm import LLMBackend
from .templates import NarrativeItem, TemplateEngine
from .validator import EvidenceValidator, Facts, build_facts

logger = logging.getLogger("cfgdrift.explain.engine")

_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


class ExplainEngine:
    """Orchestrates template rendering + optional LLM enhancement."""

    def __init__(
        self,
        template_engine: Optional[TemplateEngine] = None,
        validator: Optional[EvidenceValidator] = None,
    ) -> None:
        self._template = template_engine or TemplateEngine()
        self._validator = validator or EvidenceValidator()

    # -- public API -------------------------------------------------------

    def generate(
        self,
        drift_items: List[dict],
        schema_dict: Optional[Dict[str, str]] = None,
        llm_backend: Optional[LLMBackend] = None,
        allow_llm: bool = True,
    ) -> List[NarrativeItem]:
        """Generate one narrative per drift item (template default + LLM).

        ``allow_llm`` defaults to True; the caller disables it with
        ``--no-llm`` for fully deterministic offline output.  The backend's
        ``available()`` is consulted as an additional gate.
        """
        narratives: List[NarrativeItem] = []
        for item in drift_items or []:
            narratives.append(self._template.render(item, schema_dict))

        if (
            allow_llm
            and llm_backend is not None
            and llm_backend.available()
            and drift_items
        ):
            facts = build_facts(drift_items)
            prompt = self._build_prompt(facts)
            raw = llm_backend.generate(prompt)
            if raw is None:
                logger.info("explain: LLM unavailable/failed -> template fallback")
            else:
                parsed = self._parse_json_array(raw)
                if parsed is None:
                    logger.info("explain: LLM JSON parse failed -> template fallback")
                else:
                    self._merge_llm(narratives, parsed, facts)
        return narratives

    # -- LLM merging ------------------------------------------------------

    def _merge_llm(
        self,
        narratives: List[NarrativeItem],
        parsed: List[dict],
        facts: Facts,
    ) -> None:
        """Replace template narratives with validated LLM ones per item."""
        by_key = {n.key: n for n in narratives}
        for entry in parsed:
            if not isinstance(entry, dict):
                continue
            key = entry.get("key")
            if not key or key not in by_key:
                logger.info("explain: LLM returned unknown key %r -> template", key)
                continue
            ok, reasons = self._validator.validate(entry, facts)
            if not ok:
                logger.info(
                    "explain: evidence validation failed for %r (%s) -> template",
                    key, "; ".join(reasons),
                )
                continue
            item = by_key[key]
            impact = entry.get("impact")
            evidence = entry.get("evidence")
            if not isinstance(impact, str) or not impact.strip():
                logger.info("explain: LLM impact missing for %r -> template", key)
                continue
            if not isinstance(evidence, list) or not evidence:
                logger.info("explain: LLM evidence missing for %r -> template", key)
                continue
            item.impact = impact
            item.evidence = [str(e) for e in evidence]
            item.source = "llm"

    # -- prompt / parsing -------------------------------------------------

    @staticmethod
    def _build_prompt(facts: Facts) -> str:
        """Build the LLM prompt with the fact whitelist + JSON contract."""
        lines = [
            "请基于以下输入事实，为每条配置漂移生成业务影响叙事。",
            "",
            "允许使用的键：%s" % (", ".join(sorted(facts.keys)) or "-"),
            "允许使用的约束：%s" % (", ".join(sorted(facts.constraints)) or "-"),
            "",
            "每条输出 JSON 对象：{\"key\": <输入中的键>, "
            "\"impact\": <业务影响一句话>, \"evidence\": [<证据串列表>]}",
            "evidence 只能从以下三类中选取（严格原文）：",
            "  key: <键>",
            "  value: <旧值> -> <新值>",
            "  constraint: <约束id> 违反",
            "禁止编造输入中不存在的键、值或约束。",
            "",
            "输入事实：",
        ]
        for key, item_facts in sorted(facts.by_key.items()):
            lines.append(
                "- key=%s change=%s severity=%s constraints=[%s]"
                % (
                    key,
                    item_facts.change_type,
                    item_facts.severity,
                    ", ".join(item_facts.constraints) or "-",
                )
            )
        return "\n".join(lines)

    @staticmethod
    def _parse_json_array(raw: str) -> Optional[List[dict]]:
        """Parse an LLM reply into a JSON array (strip markdown fences)."""
        if not isinstance(raw, str) or not raw.strip():
            return None
        text = raw.strip()
        match = _JSON_FENCE_RE.match(text)
        if match:
            text = match.group(1).strip()
        try:
            data = json.loads(text)
        except ValueError:
            return None
        if not isinstance(data, list):
            return None
        return data
