"""Web-facing constraint-candidate read + promote logic (v0.11.0, P0-3).

These helpers are the single shared write path for the「一键转正」flow: they
reuse the same building blocks as the CLI ``constraint add``
(:meth:`ConstraintConfig.add_rule`) and the miner
(:meth:`ConstraintMiner.load_candidates` / :meth:`mark_promoted`), so the
Web and CLI can never drift apart.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List

from ..core.model import Constraint
from ..rules.constraints import ConstraintConfig
from ..rules.constraints import default_path as constraints_config_path
from ..rules.mining import ConstraintMiner, MinedCandidate

_GUIDE_MESSAGE = "运行 `cfgdrift constraint mine` 生成候选"


def candidates_path(home: str) -> str:
    """Return the mined_candidates.yaml path (same default as ``constraint mine``)."""
    return os.path.join(home, "mined_candidates.yaml")


def load_candidates_view(home: str) -> Dict[str, Any]:
    """Assemble the ``GET /api/constraint-candidates`` payload.

    A missing file returns the zero-noise empty state
    ``{"candidates": [], "message": <guide>}`` (never an error); a corrupt
    file raises ``ValueError`` (the endpoint maps it to a readable 400).
    """
    path = candidates_path(home)
    if not os.path.exists(path):
        return {
            "candidates": [],
            "generated_at": None,
            "source": None,
            "min_support": None,
            "message": _GUIDE_MESSAGE,
        }
    try:
        candidates: List[MinedCandidate] = ConstraintMiner.load_candidates(path)
    except Exception as exc:  # noqa: BLE001 - corrupt file -> readable 400
        raise ValueError(
            "mined candidates file %s is corrupt: %s" % (path, exc)
        ) from exc
    payload = {
        "candidates": [c.to_dict() for c in candidates],
        "generated_at": None,
        "source": None,
        "min_support": None,
        "message": None,
    }
    # Best-effort top-level metadata (a valid file written by the miner).
    try:
        import yaml

        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if isinstance(data, dict):
            payload["generated_at"] = data.get("generated_at")
            payload["source"] = data.get("source")
            payload["min_support"] = data.get("min_support")
    except Exception:  # noqa: BLE001 - metadata is best-effort
        pass
    return payload


def promote_candidate(home: str, candidate_id: str) -> Dict[str, Any]:
    """Promote one mined candidate into ``constraints.yaml`` (P0-3, D4).

    Steps (single write path):
      1. Locate the candidate (missing -> ``ValueError``); an already
         ``promoted`` candidate returns immediately (idempotent).
      2. Build the ``Constraint`` from the candidate's own dict with
         ``source="user"`` — ``enabled`` stays False (conservative: never
         auto-activated).
      3. ``ConstraintConfig.add_rule`` on ``<home>/constraints.yaml``; if a
         rule with the same id already exists (a previous add succeeded but
         the mark failed), the add is skipped — the recovery path only
         completes the mark.
      4. ``mark_promoted`` on ``mined_candidates.yaml`` (atomic write-back).

    Both files are written atomically; any failure leaves the originals
    intact.  Returns ``{"id", "status", "constraint_id", "enabled"}``.
    """
    cpath = candidates_path(home)
    try:
        candidates = ConstraintMiner.load_candidates(cpath)
    except Exception as exc:  # noqa: BLE001 - corrupt file -> readable 400
        raise ValueError(
            "mined candidates file %s is corrupt: %s" % (cpath, exc)
        ) from exc
    candidate = next((c for c in candidates if c.id == candidate_id), None)
    if candidate is None:
        raise ValueError("mined candidate %r not found" % candidate_id)
    if candidate.status == "promoted":
        return {
            "id": candidate.id,
            "status": "promoted",
            "constraint_id": candidate.id,
            "enabled": False,
        }
    constraint = Constraint.from_dict(candidate.constraint, source="user")
    # Idempotent recovery: if the rule already landed in constraints.yaml
    # (previous promote succeeded through add but died before the mark),
    # skip the add and just complete the promotion marker.
    existing = {r.id for r in ConstraintConfig.list_rules(constraints_config_path(home))}
    if constraint.id not in existing:
        ConstraintConfig.add_rule(constraints_config_path(home), constraint)
    ConstraintMiner.mark_promoted(cpath, candidate_id)
    return {
        "id": candidate.id,
        "status": "promoted",
        "constraint_id": constraint.id,
        "enabled": bool(constraint.enabled),
    }
