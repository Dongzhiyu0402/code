"""Evidence-chain anti-hallucination for narratives (v0.8.0, A-P0-3).

:func:`build_facts` turns the drift items into a whitelist of *everything*
the narrator is allowed to reference: key paths, formatted values and
constraint ids.  :class:`EvidenceValidator.validate` checks an LLM-produced
narrative against that whitelist:

1. ``evidence`` is non-empty and every element belongs to the item's allowed
   evidence set (``key: ...`` / ``value: ... -> ...`` / ``constraint: ...
   违反`` — all taken from the input, already masked);
2. any constraint id appearing in ``impact``/``evidence`` is in the facts;
3. any key-path-shaped token in ``impact`` that is neither a fact key nor a
   substring of an allowed value is treated as fabrication.

A failed validation never partially adopts the LLM output — the engine falls
back to the deterministic template for that item.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from .templates import _fmt_value

#: Key-path-shaped token: word characters, dots, dashes and brackets, and it
#: must contain a dot or an opening bracket.
_KEY_TOKEN_RE = re.compile(r"[\w.\-\[\]]+")

#: Constraint-id-shaped token: snake_case identifiers (e.g. ``http_port_range``).
_CONSTRAINT_TOKEN_RE = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+")


@dataclass
class ItemFacts:
    """The facts one drift item exposes to the narrator."""

    key: str
    change_type: str
    severity: str
    old_value: Any
    new_value: Any
    constraints: List[str]
    allowed_evidence: Set[str] = field(default_factory=set)
    allowed_value_strings: Set[str] = field(default_factory=set)


@dataclass
class Facts:
    """The whitelist of all input facts across drift items."""

    by_key: Dict[str, ItemFacts] = field(default_factory=dict)
    keys: Set[str] = field(default_factory=set)
    values: Set[str] = field(default_factory=set)
    constraints: Set[str] = field(default_factory=set)


def build_facts(drift_items: List[dict]) -> Facts:
    """Build the evidence whitelist from masked drift-item dicts."""
    facts = Facts()
    for item in drift_items or []:
        key = str(item.get("key_path", ""))
        change_type = str(item.get("change_type", "modified"))
        severity = str(item.get("severity", "WARN"))
        old_value = item.get("old_value")
        new_value = item.get("new_value")
        cids: List[str] = []
        for violation in item.get("constraint_violations") or []:
            cid = violation.get("constraint_id")
            if cid and str(cid) not in cids:
                cids.append(str(cid))

        allowed: Set[str] = set()
        if key:
            allowed.add("key: %s" % key)
        old_text = _fmt_value(old_value)
        new_text = _fmt_value(new_value)
        allowed.add("value: %s -> %s" % (old_text, new_text))
        for cid in cids:
            allowed.add("constraint: %s 违反" % cid)

        facts.by_key[key] = ItemFacts(
            key=key,
            change_type=change_type,
            severity=severity,
            old_value=old_value,
            new_value=new_value,
            constraints=cids,
            allowed_evidence=allowed,
            allowed_value_strings={old_text, new_text},
        )
        if key:
            facts.keys.add(key)
        facts.values.add(old_text)
        facts.values.add(new_text)
        facts.constraints.update(cids)
    return facts


class EvidenceValidator:
    """Validates an LLM narrative against the input-fact whitelist."""

    @staticmethod
    def validate(narrative: dict, facts: Facts) -> Tuple[bool, List[str]]:
        """Return ``(ok, reasons)`` for one narrative dict.

        ``narrative`` is the LLM output shape ``{key, impact, evidence[]}``.
        ``ok`` is False with human-readable reasons when any of the three
        anti-hallucination rules is violated.
        """
        if not isinstance(narrative, dict):
            return False, ["narrative must be a mapping"]
        reasons: List[str] = []

        key = narrative.get("key")
        if not key or key not in facts.by_key:
            return False, ["key %r is not among the input facts" % (key,)]
        item_facts = facts.by_key[key]

        evidence = narrative.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            reasons.append("evidence must be a non-empty list")
        else:
            for entry in evidence:
                if not isinstance(entry, str) or entry not in item_facts.allowed_evidence:
                    reasons.append(
                        "evidence %r is not derived from the input facts" % (entry,)
                    )
                    break

        impact = narrative.get("impact")
        if not isinstance(impact, str) or not impact.strip():
            reasons.append("impact must be a non-empty string")

        text = " ".join(
            [str(impact or "")]
            + [str(e) for e in (evidence if isinstance(evidence, list) else [])]
        )

        # Rule 2: constraint ids appearing in impact/evidence must be facts.
        for token in _CONSTRAINT_TOKEN_RE.findall(text):
            if token not in facts.constraints:
                reasons.append(
                    "constraint %r is not among the input facts" % token
                )
                break

        # Rule 3: key-path-shaped tokens in impact must be facts (or inside
        # an allowed value string — e.g. a version like "nginx:1.25").
        for token in _KEY_TOKEN_RE.findall(str(impact or "")):
            if "." not in token and "[" not in token:
                continue
            if token in facts.keys:
                continue
            if any(token in allowed for allowed in item_facts.allowed_value_strings):
                continue
            reasons.append(
                "key-path token %r is not among the input facts" % token
            )
            break

        return (not reasons), reasons
