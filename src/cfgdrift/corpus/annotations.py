"""Corpus double-annotation storage + inter-annotator agreement (v0.8.0, C-C5).

Annotations live in an **independent** ``annotations.jsonl`` file (Q1/D3) —
``instances.jsonl`` is re-derived by ``corpus export`` and would otherwise
overwrite manual labels.  The file is the single source of truth for every
annotator's record; ``corpus export`` projects the *latest* annotation of
each instance into the ``labels`` slot.

Schema (one JSON object per line)::

    {"instance_id": "docker-compose-7e20f3b-0", "annotator": "alice",
     "annotation": "minor", "annotated_at": "2026-08-04T12:00:00+00:00"}

- ``annotation`` is a 3-class ordinal label: ``severe | minor | normal``.
- ``annotated_at`` is an ISO-8601 UTC timestamp (``utcnow_iso()``).
- Write policy is **write-through full rewrite** (scale 100–300); ``add()``
  filters out the previous record with the same ``(instance_id, annotator)``
  before appending — re-annotating by the same person overwrites.
- A corrupt line raises :class:`ValueError` (the CLI surfaces it as exit 2),
  matching the corpus.yaml / state.json contract.

The :class:`KappaCalculator` implements Cohen's kappa / weighted kappa
(linear & quadratic) / confusion matrix per §6.2 of the v0.8.0 design.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from ..storage.store import utcnow_iso
from .workspace import CorpusWorkspace

logger = logging.getLogger("cfgdrift.corpus.annotations")

#: 3-class ordinal annotation labels (Q2).
ANNOTATION_VALUES = ("severe", "minor", "normal")

#: Default category order used by :class:`KappaCalculator` (index = rank).
DEFAULT_CATEGORIES = ("severe", "minor", "normal")

__all__ = [
    "ANNOTATION_VALUES",
    "Annotation",
    "AnnotationStore",
    "KappaCalculator",
]


def _validate_annotation(annotation: Any) -> str:
    """Validate an annotation label; raises ``ValueError`` when illegal."""
    if not isinstance(annotation, str) or annotation not in ANNOTATION_VALUES:
        raise ValueError(
            "invalid annotation %r (expected one of: %s)"
            % (annotation, ", ".join(ANNOTATION_VALUES))
        )
    return annotation


@dataclass
class Annotation:
    """One annotation record (an annotator's label for one instance)."""

    instance_id: str
    annotator: str
    annotation: str  # severe | minor | normal
    annotated_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.instance_id, str) or not self.instance_id.strip():
            raise ValueError("annotation instance_id must be a non-empty string")
        if not isinstance(self.annotator, str) or not self.annotator.strip():
            raise ValueError("annotation annotator must be a non-empty string")
        _validate_annotation(self.annotation)
        if not isinstance(self.annotated_at, str) or not self.annotated_at.strip():
            raise ValueError("annotation annotated_at must be a non-empty string")

    def to_dict(self) -> dict:
        return {
            "instance_id": self.instance_id,
            "annotator": self.annotator,
            "annotation": self.annotation,
            "annotated_at": self.annotated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Annotation":
        """Build a validated annotation from a raw JSONL line dict."""
        if not isinstance(data, dict):
            raise ValueError("annotation must be a mapping")
        return cls(
            instance_id=data.get("instance_id"),
            annotator=data.get("annotator"),
            annotation=data.get("annotation"),
            annotated_at=data.get("annotated_at"),
        )


class AnnotationStore:
    """Read/write access to ``annotations.jsonl`` under a corpus workspace."""

    def __init__(self, workspace: CorpusWorkspace) -> None:
        self.workspace = workspace

    # -- paths -----------------------------------------------------------

    def annotations_path(self) -> str:
        """Return the annotations.jsonl path in the workspace."""
        return os.path.join(self.workspace.root, "annotations.jsonl")

    # -- persistence -----------------------------------------------------

    def load(self) -> List[Annotation]:
        """Load all annotation records (empty list when the file is absent).

        Raises :class:`ValueError` on the first corrupt line (with the line
        number) so misconfigurations surface as exit code 2.
        """
        path = self.annotations_path()
        if not os.path.exists(path):
            return []
        out: List[Annotation] = []
        with open(path, "r", encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    data = json.loads(stripped)
                except ValueError as exc:
                    raise ValueError(
                        "corrupt annotations.jsonl line %d: %s" % (line_no, exc)
                    ) from exc
                try:
                    out.append(Annotation.from_dict(data))
                except ValueError as exc:
                    raise ValueError(
                        "corrupt annotations.jsonl line %d: %s" % (line_no, exc)
                    ) from exc
        return out

    def _write_all(self, records: List[Annotation]) -> None:
        """Write-through full rewrite of annotations.jsonl (idempotent)."""
        path = self.annotations_path()
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
        os.replace(tmp, path)

    # -- mutations -------------------------------------------------------

    def add(self, instance_id: str, annotator: str, annotation: str) -> Annotation:
        """Upsert one annotation: same ``(instance_id, annotator)`` overwrites.

        Returns the newly stored :class:`Annotation` (last write wins).
        """
        if not isinstance(instance_id, str) or not instance_id.strip():
            raise ValueError("annotation instance_id must be a non-empty string")
        if not isinstance(annotator, str) or not annotator.strip():
            raise ValueError("annotation annotator must be a non-empty string")
        _validate_annotation(annotation)
        records = self.load()
        kept = [
            r
            for r in records
            if not (r.instance_id == instance_id and r.annotator == annotator)
        ]
        new = Annotation(
            instance_id=instance_id,
            annotator=annotator,
            annotation=annotation,
            annotated_at=utcnow_iso(),
        )
        kept.append(new)
        self._write_all(kept)
        return new

    def remove(self, instance_id: str, annotator: str) -> None:
        """Remove the record for ``(instance_id, annotator)``.

        Raises :class:`ValueError` when no such record exists.
        """
        records = self.load()
        kept = [
            r
            for r in records
            if not (r.instance_id == instance_id and r.annotator == annotator)
        ]
        if len(kept) == len(records):
            raise ValueError(
                "annotation not found for %r by %r" % (instance_id, annotator)
            )
        self._write_all(kept)

    # -- queries ---------------------------------------------------------

    def by_instance(self) -> Dict[str, List[Annotation]]:
        """Group all records by instance id (insertion order preserved)."""
        grouped: Dict[str, List[Annotation]] = {}
        for record in self.load():
            grouped.setdefault(record.instance_id, []).append(record)
        return grouped

    def annotators(self) -> List[str]:
        """Unique annotator names in order of first appearance."""
        seen: List[str] = []
        for record in self.load():
            if record.annotator not in seen:
                seen.append(record.annotator)
        return seen

    def import_batch(
        self,
        mapping: Dict[str, dict],
        default_annotator: Optional[str] = None,
    ) -> int:
        """Import a batch mapping ``{instance_id: {annotation, annotator?, note?}}``.

        ``annotator`` defaults to ``default_annotator`` when the per-instance
        entry omits it; a missing effective annotator raises ``ValueError``.
        ``note`` is reserved for P1 and only logged (never persisted in
        v0.8.0).  Returns the number of imported annotations.
        """
        if not isinstance(mapping, dict):
            raise ValueError("batch labels must be a mapping {instance_id: {...}}")
        count = 0
        for instance_id, raw in mapping.items():
            if not isinstance(raw, dict):
                raise ValueError(
                    "batch entry %r must be a mapping {annotation, annotator?, "
                    "note?}" % (instance_id,)
                )
            annotation = raw.get("annotation")
            if annotation is None:
                raise ValueError(
                    "batch entry %r is missing 'annotation'" % (instance_id,)
                )
            annotator = raw.get("annotator") or default_annotator
            if not annotator or not isinstance(annotator, str):
                raise ValueError(
                    "batch entry %r has no annotator (set per-entry or pass "
                    "--annotator)" % (instance_id,)
                )
            note = raw.get("note")
            if note is not None:
                logger.info(
                    "batch annotation note for %r ignored (P1 reserved): %s",
                    instance_id, note,
                )
            self.add(str(instance_id), annotator, annotation)
            count += 1
        return count

    # -- statistics ------------------------------------------------------

    def stats(self, instances: List[dict]) -> dict:
        """Compute annotation progress statistics over ``instances``.

        Per §6.3: ``{instances, unannotated, single: {annotator: n}, double,
        agreement_rate, kappa_ready}``.  ``single`` counts instances labeled
        by *exactly one* annotator (split per annotator); ``double`` counts
        instances with at least two distinct annotators; ``agreement_rate``
        is the share of double instances whose two latest annotations agree;
        ``kappa_ready`` equals ``double``.  Orphan annotations (instance ids
        absent from ``instances``) are ignored with a warning.
        """
        known_ids = {
            str(inst.get("instance_id"))
            for inst in instances
            if inst.get("instance_id") is not None
        }
        by_instance = self.by_instance()
        orphans = [iid for iid in by_instance if iid not in known_ids]
        for iid in orphans:
            logger.warning(
                "annotation for unknown instance %r ignored (not in "
                "instances.jsonl)", iid,
            )
            by_instance.pop(iid, None)

        total = len(instances)
        unannotated = 0
        single: Dict[str, int] = {}
        double = 0
        agreeing = 0
        for inst in instances:
            iid = str(inst.get("instance_id"))
            records = by_instance.get(iid, [])
            distinct = sorted({r.annotator for r in records})
            if not distinct:
                unannotated += 1
                continue
            if len(distinct) == 1:
                single[distinct[0]] = single.get(distinct[0], 0) + 1
                continue
            double += 1
            if self._latest_two_agree(records):
                agreeing += 1
        agreement_rate = (agreeing / double) if double else 0.0
        return {
            "instances": total,
            "unannotated": unannotated,
            "single": single,
            "double": double,
            "agreement_rate": agreement_rate,
            "kappa_ready": double,
        }

    @staticmethod
    def _latest_two_agree(records: List[Annotation]) -> bool:
        """Return True when the two latest records agree (D3 ordering)."""
        ordered = sorted(
            records, key=lambda r: (r.annotated_at, r.annotator)
        )
        if len(ordered) < 2:
            return False
        return ordered[-1].annotation == ordered[-2].annotation

    @staticmethod
    def latest_by_instance(records: List[Annotation]) -> Dict[str, Annotation]:
        """Map ``instance_id -> latest record`` (D3: annotated_at, then
        annotator lexicographic order; the last in that order wins)."""
        latest: Dict[str, Annotation] = {}
        for record in records:
            current = latest.get(record.instance_id)
            if current is None or (record.annotated_at, record.annotator) >= (
                current.annotated_at, current.annotator,
            ):
                latest[record.instance_id] = record
        return latest


class KappaCalculator:
    """Inter-annotator agreement metrics (Cohen's kappa, §6.2)."""

    @staticmethod
    def confusion_matrix(
        a: List[str],
        b: List[str],
        categories: Tuple[str, ...] = DEFAULT_CATEGORIES,
    ) -> Dict[str, Dict[str, int]]:
        """Build the confusion matrix (rows = annotator A, cols = annotator B)."""
        if len(a) != len(b):
            raise ValueError(
                "kappa requires equal-length annotation lists (%d vs %d)"
                % (len(a), len(b))
            )
        categories = tuple(categories)
        if not categories:
            raise ValueError("kappa requires at least one category")
        index = {cat: i for i, cat in enumerate(categories)}
        for value in list(a) + list(b):
            if value not in index:
                raise ValueError(
                    "kappa annotation %r is not one of the categories %s"
                    % (value, ", ".join(categories))
                )
        matrix: Dict[str, Dict[str, int]] = {
            cat_a: {cat_b: 0 for cat_b in categories} for cat_a in categories
        }
        for va, vb in zip(a, b):
            matrix[va][vb] += 1
        return matrix

    @staticmethod
    def weighted_kappa(
        a: List[str],
        b: List[str],
        categories: Tuple[str, ...] = DEFAULT_CATEGORIES,
        weight: str = "linear",
    ) -> float:
        """Weighted kappa: ``1 - sum(w*o)/sum(w*e)``.

        ``weight`` is ``linear`` (|i-j|/(k-1)) or ``quadratic``
        (((i-j)/(k-1))^2).  Returns 1.0 when both weighted sums are zero
        (perfect agreement / degenerate denominator), matching the Cohen
        kappa edge convention.
        """
        if weight not in ("linear", "quadratic"):
            raise ValueError(
                "weighted kappa weight must be 'linear' or 'quadratic', "
                "got %r" % weight
            )
        if len(a) != len(b):
            raise ValueError(
                "kappa requires equal-length annotation lists (%d vs %d)"
                % (len(a), len(b))
            )
        n = len(a)
        if n < 2:
            raise ValueError("需要至少 2 条双人标注实例")
        categories = tuple(categories)
        if not categories:
            raise ValueError("kappa requires at least one category")
        matrix = KappaCalculator.confusion_matrix(a, b, categories)
        k = len(categories)
        if k <= 1:
            # Single category: all weights are zero -> perfect agreement.
            return 1.0
        cats = list(categories)
        rows = {cat: sum(matrix[cat].values()) for cat in cats}
        cols = {cat: sum(matrix[ca][cat] for ca in cats) for cat in cats}

        def w_ij(i: int, j: int) -> float:
            if weight == "linear":
                return abs(i - j) / (k - 1)
            return ((i - j) / (k - 1)) ** 2

        sum_wo = 0.0
        sum_we = 0.0
        for i, cat_a in enumerate(cats):
            for j, cat_b in enumerate(cats):
                o_ij = matrix[cat_a][cat_b] / n
                e_ij = (rows[cat_a] * cols[cat_b]) / (n * n)
                w = w_ij(i, j)
                sum_wo += w * o_ij
                sum_we += w * e_ij
        if sum_we == 0:
            return 1.0 if sum_wo == 0 else 0.0
        return 1.0 - sum_wo / sum_we

    @staticmethod
    def cohen_kappa(
        a: List[str],
        b: List[str],
        categories: Tuple[str, ...] = DEFAULT_CATEGORIES,
    ) -> dict:
        """Cohen's kappa with confusion matrix and weighted variants.

        Returns ``{kappa, po, pe, n, agreement_rate, confusion_matrix,
        weighted: {"linear": k, "quadratic": k}}``.  ``po = sum(n_ii)/n``,
        ``pe = sum(row_i*col_i)/n^2``, ``kappa = (po-pe)/(1-pe)``; when
        ``1 - pe == 0`` kappa is ``1.0`` iff ``po == 1`` else ``0.0``
        (sklearn-aligned).  Raises ``ValueError`` when ``n < 2``.
        """
        if len(a) != len(b):
            raise ValueError(
                "kappa requires equal-length annotation lists (%d vs %d)"
                % (len(a), len(b))
            )
        n = len(a)
        if n < 2:
            raise ValueError("需要至少 2 条双人标注实例")
        categories = tuple(categories)
        if not categories:
            raise ValueError("kappa requires at least one category")
        matrix = KappaCalculator.confusion_matrix(a, b, categories)
        cats = list(categories)
        rows = {cat: sum(matrix[cat].values()) for cat in cats}
        cols = {cat: sum(matrix[ca][cat] for ca in cats) for cat in cats}

        po = sum(matrix[cat][cat] for cat in cats) / n
        pe = sum(rows[cat] * cols[cat] for cat in cats) / (n * n)
        denom = 1.0 - pe
        if denom == 0:
            kappa = 1.0 if po == 1 else 0.0
        else:
            kappa = (po - pe) / denom

        return {
            "kappa": kappa,
            "po": po,
            "pe": pe,
            "n": n,
            "agreement_rate": po,
            "confusion_matrix": matrix,
            "weighted": {
                "linear": KappaCalculator.weighted_kappa(
                    a, b, categories, weight="linear"
                ),
                "quadratic": KappaCalculator.weighted_kappa(
                    a, b, categories, weight="quadratic"
                ),
            },
        }
