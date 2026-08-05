"""Report-to-report drift diff (v0.10.0, P0-3).

``diff_reports`` compares the **drift items of two stored scans** (the
``data.items`` lists of two ``store.get_scan`` payloads), not the raw
snapshot trees — :class:`cfgdrift.core.compare.CompareEngine` /
``SemanticDiffer.diff_snapshot`` operate on original semantic trees for
cross-environment comparison, whereas a report diff answers "what changed
between this scan and the previous one" at the already-computed drift level.

Fingerprint is ``(file, key_path)`` — deliberately **without** the
``change_type`` so that the same key changing kind between two scans is
reported as a *change*, not as add+remove.  Three groups are produced:

- ``added``   — present in A, absent in B (the full A item);
- ``removed`` — present in B, absent in A (the full B item);
- ``changed`` — present in both but with a different severity **or** a
  different ``new_value`` **or** a different ``change_type``.  Each entry
  carries both items plus ``severity_changed`` / ``value_changed`` flags.

``old_value`` is never compared across scans (each side keeps its own
display copy).  Sensitive values are expected to be masked by the caller
(``SensitiveMasker.mask_payload``) **before** calling this function, so the
output never needs re-masking.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def _fingerprint(item: Dict[str, Any]) -> tuple:
    """Diff identity of a drift item: ``(file, key_path)``."""
    return (
        str(item.get("file", "")),
        str(item.get("key_path", "")),
    )


def _sort_key(fp: tuple) -> tuple:
    return (fp[0], fp[1])


def diff_reports(
    items_a: List[Dict[str, Any]],
    items_b: List[Dict[str, Any]],
    base_scan_id: Optional[int] = None,
    target_scan_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Diff two scan reports' drift-item lists into added/removed/changed.

    ``items_a`` / ``items_b`` are the ``data.items`` lists of the two stored
    reports (each item a dict with ``key_path`` / ``change_type`` /
    ``severity`` / ``file`` / ``old_value`` / ``new_value`` / ``line`` ...
    per :meth:`cfgdrift.core.model.DriftItem.to_dict`).

    Returns a dict with ``base_scan_id`` / ``target_scan_id`` (filled from
    the optional arguments), the three groups and a ``summary`` of counts;
    ``summary.total == 0`` means "no difference".
    """
    map_a: Dict[tuple, Dict[str, Any]] = {
        _fingerprint(it): it for it in items_a
    }
    map_b: Dict[tuple, Dict[str, Any]] = {
        _fingerprint(it): it for it in items_b
    }
    fp_a = set(map_a)
    fp_b = set(map_b)

    added = [map_a[fp] for fp in sorted(fp_a - fp_b, key=_sort_key)]
    removed = [map_b[fp] for fp in sorted(fp_b - fp_a, key=_sort_key)]

    changed: List[Dict[str, Any]] = []
    for fp in sorted(fp_a & fp_b, key=_sort_key):
        item_a = map_a[fp]
        item_b = map_b[fp]
        severity_changed = str(item_a.get("severity")) != str(
            item_b.get("severity")
        )
        value_changed = item_a.get("new_value") != item_b.get("new_value")
        change_type_changed = str(item_a.get("change_type")) != str(
            item_b.get("change_type")
        )
        if severity_changed or value_changed or change_type_changed:
            changed.append(
                {
                    "item_a": item_a,
                    "item_b": item_b,
                    "severity_changed": bool(severity_changed),
                    "value_changed": bool(value_changed),
                }
            )

    summary = {
        "added": len(added),
        "removed": len(removed),
        "changed": len(changed),
        "total": len(added) + len(removed) + len(changed),
    }
    return {
        "base_scan_id": base_scan_id,
        "target_scan_id": target_scan_id,
        "added": added,
        "removed": removed,
        "changed": changed,
        "summary": summary,
    }
