"""OpenAI-compatible LLM backend (v0.8.0, Q5) — stdlib only.

The backend talks to any OpenAI-compatible ``/chat/completions`` REST
endpoint with :mod:`urllib.request` (no ``openai`` dependency).  Environment
configuration::

    CFGDRIFT_LLM_URL      default https://api.openai.com/v1/chat/completions
    CFGDRIFT_LLM_KEY      API key (empty -> backend unavailable -> template)
    CFGDRIFT_LLM_MODEL    default gpt-4o-mini
    CFGDRIFT_LLM_TIMEOUT  default 10 (seconds)

Four degradation classes all collapse to ``None`` (the engine falls back to
the deterministic template): no key, timeout, HTTP error, and JSON parse
failure.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Optional

logger = logging.getLogger("cfgdrift.explain.llm")

_DEFAULT_URL = "https://api.openai.com/v1/chat/completions"
_DEFAULT_MODEL = "gpt-4o-mini"
_DEFAULT_TIMEOUT = 10.0

_SYSTEM_PROMPT = (
    "你是一名配置漂移影响分析助手。你只能基于给定的输入事实组织语言，"
    "绝对禁止编造输入中不存在的键、值或约束。输出必须为合法 JSON 数组，"
    "每项形如 {\"key\": ..., \"impact\": ..., \"evidence\": [...]}。"
)


class LLMBackend:
    """Abstract LLM backend interface (P1 extension point)."""

    def available(self) -> bool:
        """Return True when the backend is configured and usable."""
        raise NotImplementedError

    def generate(self, prompt: str) -> Optional[str]:
        """Send a prompt and return the raw text reply (None on failure)."""
        raise NotImplementedError


class OpenAICompatBackend(LLMBackend):
    """OpenAI-compatible chat-completions backend over urllib."""

    def __init__(
        self,
        url: Optional[str] = None,
        key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> None:
        self.url = url or os.environ.get("CFGDRIFT_LLM_URL", _DEFAULT_URL)
        self.key = key if key is not None else os.environ.get("CFGDRIFT_LLM_KEY", "")
        self.model = model or os.environ.get("CFGDRIFT_LLM_MODEL", _DEFAULT_MODEL)
        try:
            raw_timeout = (
                timeout if timeout is not None
                else os.environ.get("CFGDRIFT_LLM_TIMEOUT", str(_DEFAULT_TIMEOUT))
            )
            self.timeout = float(raw_timeout)
        except (TypeError, ValueError):
            self.timeout = _DEFAULT_TIMEOUT

    def available(self) -> bool:
        return bool(self.key and self.key.strip())

    def generate(self, prompt: str) -> Optional[str]:
        """POST ``chat/completions`` with ``temperature=0`` and return the
        assistant message content.  Returns ``None`` on any failure (no key,
        timeout, HTTP error, malformed payload).
        """
        if not self.available():
            logger.info("LLM unavailable: CFGDRIFT_LLM_KEY not set")
            return None
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
        }
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer %s" % self.key,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            logger.info("LLM HTTP error %s: %s", exc.code, exc.reason)
            return None
        except urllib.error.URLError as exc:
            logger.info("LLM connection error: %s", exc.reason)
            return None
        except OSError as exc:
            logger.info("LLM timeout/IO error: %s", exc)
            return None
        try:
            data = json.loads(body)
            content = data["choices"][0]["message"]["content"]
            return content if isinstance(content, str) else None
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            logger.info("LLM response parse failure: %s", exc)
            return None
