"""Deterministic narrative templates + built-in key semantics (v0.8.0, D8).

The :class:`TemplateEngine` renders one structured narrative per drift item
*without* any LLM: it combines

1. a change-type main clause (``modified`` / ``added`` / ``removed`` /
   ``type_changed``),
2. an impact suffix picked in priority order — constraint violation →
   ``image``/``tag``-with-``latest`` special case → severity fallback.

Every claim is derived from the input item only; evidence strings are the
three input-fact shapes (``key: ...`` / ``value: ... -> ...`` /
``constraint: ... 违反``).  The same input always produces the same output.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

#: Built-in key-semantics dictionary (D8, 24+ patterns).  Ordered by
#: priority — the *first* regex that matches a key path wins (``port`` first).
KEY_SEMANTICS: List[Tuple[str, str]] = [
    ("port", "监听端口"),
    ("tls|ssl", "传输安全（TLS/SSL）"),
    ("image", "容器镜像"),
    ("tag", "镜像/版本标签"),
    ("version", "软件/配置版本"),
    ("level", "日志级别"),
    ("worker_processes", "工作进程数"),
    ("worker_connections", "单进程最大连接数"),
    ("replicas", "副本数"),
    ("timeout", "超时时间"),
    ("retries", "重试次数"),
    ("keepalive", "连接保活"),
    ("max_connections", "最大连接数"),
    ("pool_size", "连接池大小"),
    ("password|passwd", "口令（敏感）"),
    ("token|secret|api_key", "令牌/密钥（敏感）"),
    ("cert|key_path", "证书与私钥路径"),
    ("protocol", "通信协议"),
    ("mode", "运行模式"),
    ("url|endpoint|host", "服务地址"),
    ("enabled", "功能开关"),
    ("engine", "存储引擎"),
    ("algorithm", "加密/签名算法"),
    ("threads", "线程数"),
    ("log", "日志配置"),
    ("gzip", "压缩开关"),
    ("cookie|authorization|credential", "鉴权相关（敏感）"),
]

#: Impact suffixes by severity (deterministic fallback).
_SEVERITY_SUFFIX = {
    "CRITICAL": "。可能导致服务不可用或安全风险，需立即确认。",
    "WARN": "。可能影响运行稳定性，建议确认。",
    "INFO": "。属于常规变更，影响有限。",
    "NONE": "。无显著影响。",
}

#: The image/tag special case suffix (values containing ``latest``).
_LATEST_SUFFIX = "；使用 latest 标签可能导致部署不可复现、升级不受控。"

#: Constraint-violation suffix template.
_CONSTRAINT_SUFFIX = "；且违反约束 {cid}（{message}），可能导致配置与约束不一致。"

#: Regexes whose matched key is treated as the image/tag special case.
_LATEST_KEYS = ("image", "tag")


def _fmt_value(value: Any) -> str:
    """Format a semantic-tree value for display (strings are quoted)."""
    if value is None:
        return "null"
    return json.dumps(value, ensure_ascii=False)


def merge_schema(user_schema: Optional[Dict[str, str]]) -> Dict[str, str]:
    """Merge the user schema over the built-in dictionary (user wins).

    ``user_schema`` is ``{regex: description}``; user entries are matched
    first (most specific first), and a user entry with the same regex
    replaces the built-in description.
    """
    merged: Dict[str, str] = {}
    if user_schema:
        for regex, desc in user_schema.items():
            merged[str(regex)] = str(desc)
    for regex, desc in KEY_SEMANTICS:
        merged.setdefault(regex, desc)
    return merged


def match_semantics(key_path: str, schema_dict: Dict[str, str]) -> Optional[Tuple[str, str]]:
    """Return the first ``(regex, description)`` matching ``key_path``.

    ``schema_dict`` is the merged dictionary (see :func:`merge_schema`);
    iteration order defines the priority.  Returns ``None`` when nothing
    matches.
    """
    if not key_path:
        return None
    for regex, desc in schema_dict.items():
        try:
            if re.search(regex, key_path, re.IGNORECASE) is not None:
                return regex, desc
        except re.error:
            continue
    return None


@dataclass
class NarrativeItem:
    """One structured business-impact narrative for a drift item."""

    key: str
    change_type: str
    severity: str
    impact: str
    evidence: List[str]
    source: str  # "template" | "llm"

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "change_type": self.change_type,
            "severity": self.severity,
            "impact": self.impact,
            "evidence": list(self.evidence),
            "source": self.source,
        }


class TemplateEngine:
    """Deterministic narrative renderer (template-first, D8)."""

    def render(self, item: dict, schema_dict: Optional[Dict[str, str]] = None) -> NarrativeItem:
        """Render one narrative from a drift-item dict (masked values)."""
        key = str(item.get("key_path", ""))
        change_type = str(item.get("change_type", "modified"))
        severity = str(item.get("severity", "WARN"))
        old_value = item.get("old_value")
        new_value = item.get("new_value")
        old_type = item.get("old_type")
        new_type = item.get("new_type")
        violations = item.get("constraint_violations") or []

        schema = merge_schema(schema_dict)
        matched = match_semantics(key, schema)
        semantics = matched[1] if matched else self._fallback_semantics(key)
        matched_regex = matched[0] if matched else None

        main = self._main_clause(
            change_type, semantics, old_value, new_value, old_type, new_type
        )

        if violations:
            cid = str(violations[0].get("constraint_id", "?"))
            message = str(violations[0].get("message", ""))
            impact = main + _CONSTRAINT_SUFFIX.format(cid=cid, message=message)
        elif (
            matched_regex in _LATEST_KEYS
            and new_value is not None
            and "latest" in str(new_value).lower()
        ):
            impact = main + _LATEST_SUFFIX
        else:
            impact = main + _SEVERITY_SUFFIX.get(severity, _SEVERITY_SUFFIX["NONE"])

        evidence = self._build_evidence(key, old_value, new_value, violations)
        return NarrativeItem(
            key=key,
            change_type=change_type,
            severity=severity,
            impact=impact,
            evidence=evidence,
            source="template",
        )

    # -- internals --------------------------------------------------------

    @staticmethod
    def _fallback_semantics(key_path: str) -> str:
        """A deterministic generic semantic name for unknown keys."""
        if not key_path:
            return "配置项"
        last = key_path.split(".")[-1]
        last = re.sub(r"\[\d+\]", "", last)
        return last if last else "配置项"

    @staticmethod
    def _main_clause(
        change_type: str,
        semantics: str,
        old_value: Any,
        new_value: Any,
        old_type: Optional[str],
        new_type: Optional[str],
    ) -> str:
        old_text = _fmt_value(old_value)
        new_text = _fmt_value(new_value)
        if change_type == "added":
            return "新增%s（值 %s）" % (semantics, new_text)
        if change_type == "removed":
            return "移除%s（原值 %s）" % (semantics, old_text)
        if change_type == "type_changed":
            return "%s类型由 %s 变为 %s（%s → %s）" % (
                semantics, old_type or "?", new_type or "?", old_text, new_text,
            )
        return "%s从 %s 改为 %s" % (semantics, old_text, new_text)

    @staticmethod
    def _build_evidence(
        key: str,
        old_value: Any,
        new_value: Any,
        violations: List[dict],
    ) -> List[str]:
        """Build the three evidence shapes from input facts only."""
        evidence: List[str] = []
        if key:
            evidence.append("key: %s" % key)
        evidence.append("value: %s -> %s" % (_fmt_value(old_value), _fmt_value(new_value)))
        for violation in violations:
            cid = violation.get("constraint_id")
            if cid:
                evidence.append("constraint: %s 违反" % cid)
        return evidence
