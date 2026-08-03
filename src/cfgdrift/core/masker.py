"""Sensitive-value masking (v0.4.0).

The masking layer is deliberately *invisible* to the detection core: diff and
storage always work with the original values.  Masking is applied only at the
four display exits (terminal / JSON report / Web API / alert payload).

Design contract (see ``docs/system_design.md`` v0.4.0):

- ``DEFAULT_SENSITIVE_KEYWORDS``: the 13 keyword stems that mark a key as
  sensitive.  A bare ``key`` is **not** in the list, so ``key`` / ``key2`` /
  ``keyboard`` are never masked by default.
- Matching is per key-path segment (``.`` and ``[i]`` splits, reusing
  :func:`cfgdrift.core.model.parse_path` semantics) with a case-insensitive
  substring test, plus optional ``fnmatch`` glob patterns that are applied to
  the *full* key path.
- ``masking.yaml`` (``<home>/masking.yaml``) is optional: ``version`` /
  ``mask`` / ``keywords`` / ``patterns``.  A missing or corrupt file falls
  back to the defaults with a warning — masking must never break the tool.

The database always stores raw values; only the display copies are masked.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
from typing import Any, Dict, List, Optional

from .model import parse_path

logger = logging.getLogger("cfgdrift.core.masker")

DEFAULT_MASK = "******"

# The 13 default sensitive keyword stems (case-insensitive substring match).
DEFAULT_SENSITIVE_KEYWORDS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "access_token",
    "auth_token",
    "private_key",
    "client_secret",
    "credential",
    "authorization",
    "cookie",
)

# fnmatch glob patterns applied to the full key path (case-insensitive).
DEFAULT_PATTERNS: tuple = ()

_MASKING_CONFIG_VERSION = 1

# Split a key-path segment into sub-segments around array indices, e.g.
# ``servers[0]`` -> ``["servers", "0"]``.  Index digits are checked too but
# never match the keyword stems in practice.
_ARRAY_SPLIT_RE = re.compile(r"[\[\]]+")


def _path_segments(key_path: str) -> List[str]:
    """Return the matching segments of a key path.

    Uses :func:`parse_path` (handles ``.`` / ``[i]`` / backslash escaping)
    and additionally splits array indices out of a segment so
    ``servers[0].token`` yields ``["servers", "0", "token"]``.
    """
    out: List[str] = []
    for seg in parse_path(key_path or ""):
        for part in _ARRAY_SPLIT_RE.split(seg):
            if part != "":
                out.append(part)
    return out


class SensitiveMasker:
    """Masks sensitive values at display time (never mutates stored data)."""

    def __init__(
        self,
        keywords: Optional[List[str]] = None,
        patterns: Optional[List[str]] = None,
        mask: str = DEFAULT_MASK,
    ) -> None:
        self.mask = mask if mask else DEFAULT_MASK
        # ``None`` means "use the defaults"; an explicit list *replaces* them
        # (the CLI ``--sensitive-keys`` append happens in from_config).
        self.keywords: List[str] = [
            str(k).lower()
            for k in (keywords if keywords is not None else DEFAULT_SENSITIVE_KEYWORDS)
        ]
        self.patterns: List[str] = [
            str(p)
            for p in (patterns if patterns is not None else DEFAULT_PATTERNS)
        ]

    # -- construction ----------------------------------------------------

    @classmethod
    def from_config(
        cls, path: Optional[str] = None, extra_keywords: Optional[List[str]] = None
    ) -> "SensitiveMasker":
        """Build a masker from ``masking.yaml`` (missing/corrupt -> defaults).

        ``extra_keywords`` appends additional keyword stems (the CLI
        ``--sensitive-keys`` option uses this; it is an *append* semantic, it
        never replaces the defaults).
        """
        keywords = list(DEFAULT_SENSITIVE_KEYWORDS)
        patterns = list(DEFAULT_PATTERNS)
        mask = DEFAULT_MASK

        if path and os.path.exists(path):
            try:
                import yaml

                with open(path, "r", encoding="utf-8") as fh:
                    data = yaml.safe_load(fh) or {}
                if not isinstance(data, dict):
                    raise ValueError("masking config must be a mapping")
                if data.get("version") != _MASKING_CONFIG_VERSION:
                    logger.warning(
                        "masking.yaml version %r ignored (expected %d); "
                        "using the file keys anyway",
                        data.get("version"),
                        _MASKING_CONFIG_VERSION,
                    )
                configured_mask = data.get("mask")
                if isinstance(configured_mask, str) and configured_mask:
                    mask = configured_mask
                configured_keywords = data.get("keywords")
                if isinstance(configured_keywords, list):
                    # File keywords replace the defaults (explicit intent).
                    keywords = [str(k) for k in configured_keywords]
                configured_patterns = data.get("patterns")
                if isinstance(configured_patterns, list):
                    patterns = [str(p) for p in configured_patterns]
            except Exception as exc:  # noqa: BLE001 - never break masking
                logger.warning(
                    "masking.yaml %s is unreadable (%s); falling back to "
                    "default sensitive keywords",
                    path,
                    exc,
                )
                keywords = list(DEFAULT_SENSITIVE_KEYWORDS)
                patterns = list(DEFAULT_PATTERNS)
                mask = DEFAULT_MASK

        if extra_keywords:
            for k in extra_keywords:
                if k and str(k) not in keywords:
                    keywords.append(str(k))
        return cls(keywords=keywords, patterns=patterns, mask=mask)

    # -- matching --------------------------------------------------------

    def is_sensitive_key(self, key_path: str) -> bool:
        """Return True when a key path should be masked.

        Per-segment case-insensitive substring test plus fnmatch glob
        patterns against the full path (also case-insensitive).
        """
        if not key_path:
            return False
        lower_path = key_path.lower()
        for segment in _path_segments(key_path):
            seg_lower = segment.lower()
            for keyword in self.keywords:
                if keyword and keyword in seg_lower:
                    return True
        for pattern in self.patterns:
            if fnmatch.fnmatch(lower_path, pattern.lower()):
                return True
        return False

    # -- masking ---------------------------------------------------------

    def mask_item(self, item: Any) -> Any:
        """Mask one drift item in place (``DriftItem`` or item dict).

        ``DriftItem`` instances get ``masked = True`` and their
        ``old_value`` / ``new_value`` replaced with the mask; the type
        fields are preserved (a masked type change stays a type change).
        Plain dicts support both the report shape (``old_value`` /
        ``new_value`` + ``key_path``) and the alert payload shape
        (``baseline`` / ``current`` + ``key``).
        """
        if isinstance(item, dict):
            key_path = item.get("key_path") or item.get("key") or ""
            if not self.is_sensitive_key(str(key_path)):
                return item
            if "old_value" in item or "new_value" in item:
                item["old_value"] = self.mask
                item["new_value"] = self.mask
            if "baseline" in item or "current" in item:
                item["baseline"] = self.mask
                item["current"] = self.mask
            item["masked"] = True
            return item

        if not self.is_sensitive_key(item.key_path):
            return item
        item.old_value = self.mask
        item.new_value = self.mask
        item.masked = True
        return item

    def mask_payload(self, data: Any) -> Any:
        """Mask the items of a report/payload document in place.

        Accepts the 7.6 report envelope ``{"code": 0, "data": {"items": [...]}}``,
        a bare ``{"items": [...]}``, or an alert payload
        ``{"drift_items": [...]}``.  Returns ``data`` (mutated).
        """
        items: Optional[List[Any]] = None
        if isinstance(data, dict):
            inner = data.get("data")
            if isinstance(inner, dict) and isinstance(inner.get("items"), list):
                items = inner["items"]
            elif isinstance(data.get("items"), list):
                items = data["items"]
            elif isinstance(data.get("drift_items"), list):
                items = data["drift_items"]
        if items:
            for item in items:
                if isinstance(item, dict):
                    self.mask_item(item)
        return data


def default_masker(extra_keywords: Optional[List[str]] = None) -> SensitiveMasker:
    """Convenience constructor: defaults only, plus optional extra keywords."""
    return SensitiveMasker.from_config(path=None, extra_keywords=extra_keywords)


def masking_config_path(home: str) -> str:
    """Return the masking.yaml path under a cfgdrift home directory."""
    return os.path.join(home, "masking.yaml")
