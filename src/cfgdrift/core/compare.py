"""Multi-environment baseline comparison (v0.4.0).

``cfgdrift compare ENV1 ENV2 [ENV3...]`` diffs every environment after the
first against the first (the reference).  Each environment resolves to a
baseline through the optional ``environments.yaml`` mapping; when no mapping
exists (or an environment is absent from it) the environment name is used
directly as the baseline name.

``environments.yaml`` (``<home>/environments.yaml``)::

    version: 1
    environments:
      prod: prod-baseline
      staging: staging-baseline

Exit codes follow the CLI convention: 0 = no differences, 1 = differences
found, 2 = error (missing baseline, unreadable mapping, ...).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from .constraints import ConstraintEngine
from .differ import SemanticDiffer
from .masker import SensitiveMasker
from .model import CompareReport, IgnoreRule, ScanSummary, SeverityRule

logger = logging.getLogger("cfgdrift.core.compare")

_ENVIRONMENTS_CONFIG_VERSION = 1


def environments_config_path(home: str) -> str:
    """Return the environments.yaml path under a cfgdrift home directory."""
    return os.path.join(home, "environments.yaml")


class CompareEngine:
    """Compares stored baselines for multiple environments."""

    def __init__(self, store) -> None:
        self.store = store
        self._differ = SemanticDiffer()

    # -- environments.yaml ----------------------------------------------

    def load_environments(self, home: Optional[str] = None) -> Dict[str, str]:
        """Load the optional ``{env_name: baseline_name}`` mapping.

        A missing file returns ``{}`` (environments fall back to their own
        names).  A corrupt file returns ``{}`` with a warning — comparison
        falls back to the environment names rather than failing.
        """
        if not home:
            return {}
        path = environments_config_path(home)
        if not os.path.exists(path):
            return {}
        try:
            import yaml

            with open(path, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            if not isinstance(data, dict):
                raise ValueError("environments config must be a mapping")
            environments = data.get("environments")
            if environments is None:
                environments = data
            if not isinstance(environments, dict):
                raise ValueError("'environments' must be a mapping")
            out: Dict[str, str] = {}
            for env, baseline in environments.items():
                if isinstance(baseline, str) and baseline:
                    out[str(env)] = baseline
            return out
        except Exception as exc:  # noqa: BLE001 - fall back to env names
            logger.warning(
                "environments.yaml %s is unreadable (%s); using environment "
                "names as baseline names",
                path,
                exc,
            )
            return {}

    # -- resolution ------------------------------------------------------

    def resolve_baseline_name(
        self, environment: str, env_map: Optional[Dict[str, str]] = None
    ) -> str:
        """Map an environment name to a baseline name (fallback: itself)."""
        if env_map and environment in env_map:
            return env_map[environment]
        return environment

    # -- comparison ------------------------------------------------------

    def compare_snapshots(
        self,
        baseline_a_name: str,
        baseline_b_name: str,
        snapshot_a: dict,
        snapshot_b: dict,
        rules: Optional[List[IgnoreRule]] = None,
        severity_rules: Optional[List[SeverityRule]] = None,
        old_lines: Optional[Dict[str, Dict[str, int]]] = None,
        new_lines: Optional[Dict[str, Dict[str, int]]] = None,
        constraints: Optional[List[Any]] = None,  # v0.8.0 (D10)
    ) -> Tuple[List[Any], ScanSummary]:
        """Diff two snapshots with the shared differ (v0.4.0 extensions).

        ``constraints`` (v0.8.0, optional) is forwarded to the differ so the
        diff itself attaches constraint violations to drift items.
        """
        return self._differ.diff_snapshot(
            snapshot_a,
            snapshot_b,
            rules=rules,
            severity_rules=severity_rules,
            old_lines=old_lines,
            new_lines=new_lines,
            constraints=constraints,
        )

    def compare(
        self,
        environments: List[str],
        env_map: Optional[Dict[str, str]] = None,
        rules: Optional[List[IgnoreRule]] = None,
        severity_rules: Optional[List[SeverityRule]] = None,
        masker: Optional[SensitiveMasker] = None,
        constraints: Optional[List[Any]] = None,  # v0.8.0 (D10)
    ) -> List[CompareReport]:
        """Compare ``environments[1:]`` against ``environments[0]``.

        Returns one :class:`CompareReport` per compared environment.  Raises
        ``ValueError`` when a baseline cannot be resolved or the list has
        fewer than two environments.

        ``masker`` (optional) applies sensitive-value masking to the returned
        items — the display exits (terminal / JSON) pass a masker so
        ``password``-like keys never leak plaintext.  Masking mutates the
        report items only; no stored data is touched.

        ``constraints`` (v0.8.0, D10, optional): when given, the engine runs
        ``ConstraintEngine.check_tree`` against *both* environment baselines
        and stores the per-side violations in
        ``CompareReport.constraint_violations`` (``env_a`` = reference,
        ``env_b`` = compared).  Violations are informational — they never
        change the drift-based exit code (D6).
        """
        if len(environments) < 2:
            raise ValueError("compare requires at least two environments")
        reference_env = environments[0]
        reference_baseline_name = self.resolve_baseline_name(reference_env, env_map)
        baseline_a = self.store.get_baseline(reference_baseline_name)

        reports: List[CompareReport] = []
        for env in environments[1:]:
            baseline_name = self.resolve_baseline_name(env, env_map)
            baseline_b = self.store.get_baseline(baseline_name)
            items, summary = self.compare_snapshots(
                reference_baseline_name,
                baseline_name,
                baseline_a.data,
                baseline_b.data,
                rules=rules,
                severity_rules=severity_rules,
                old_lines=baseline_a.line_maps,
                new_lines=baseline_b.line_maps,
                constraints=constraints,
            )
            violations: Dict[str, List[dict]] = {}
            if constraints:
                violations["env_a"] = ConstraintEngine.check_tree(
                    constraints, baseline_a.data
                )
                violations["env_b"] = ConstraintEngine.check_tree(
                    constraints, baseline_b.data
                )
            reports.append(
                CompareReport(
                    baseline_a=reference_baseline_name,
                    baseline_b=baseline_name,
                    created_at=baseline_b.created_at,
                    summary=summary,
                    items=items,
                    env1_version=baseline_a.version,
                    env2_version=baseline_b.version,
                    constraint_violations=violations,
                )
            )
        if masker is not None:
            for rep in reports:
                for item in rep.items:
                    masker.mask_item(item)
        return reports
