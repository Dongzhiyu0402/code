"""Alert delivery channels (webhook / email / script) + retry with backoff.

All channels use only the standard library:

- :class:`WebhookChannel` — ``urllib.request`` POST with JSON body.
- :class:`EmailChannel` — ``smtplib`` + ``email.message.EmailMessage``.
- :class:`ScriptChannel` — ``subprocess.run`` with ``CFGDRIFT_*`` env vars.

A send that fails raises :class:`ChannelError`; the dispatcher applies
:func:`retry_with_backoff` (3 attempts, 1s/5s/30s) and records the outcome.
"""

from __future__ import annotations

import json
import logging
import os
import smtplib
import subprocess
import time
import urllib.error
import urllib.request
from email.message import EmailMessage
from typing import Callable, Dict, List, Optional

from .models import payload_context, substitute

logger = logging.getLogger("cfgdrift.alert.channels")


class ChannelError(Exception):
    """Raised when a channel fails to deliver an alert."""


# ---------------------------------------------------------------------------
# Shared payload helpers
# ---------------------------------------------------------------------------

_CFGDRIFT_ENV_KEYS = (
    "CFGDRIFT_EVENT",
    "CFGDRIFT_VERSION",
    "CFGDRIFT_TIMESTAMP",
    "CFGDRIFT_SEVERITY",
    "CFGDRIFT_BASELINE",
    "CFGDRIFT_TARGET",
    "CFGDRIFT_DRIFT_COUNT",
    "CFGDRIFT_SUMMARY",
    "CFGDRIFT_DRIFT_ITEMS_JSON",
)


def payload_env(payload: Dict[str, object]) -> Dict[str, str]:
    """Map a payload onto the ``CFGDRIFT_*`` environment variable contract."""
    return {
        "CFGDRIFT_EVENT": str(payload.get("event", "cfgdrift.drift")),
        "CFGDRIFT_VERSION": str(payload.get("version", "")),
        "CFGDRIFT_TIMESTAMP": str(payload.get("timestamp", "")),
        "CFGDRIFT_SEVERITY": str(payload.get("severity", "")),
        "CFGDRIFT_BASELINE": str(payload.get("baseline", "")),
        "CFGDRIFT_TARGET": str(payload.get("target", "")),
        "CFGDRIFT_DRIFT_COUNT": str(payload.get("drift_count", 0)),
        "CFGDRIFT_SUMMARY": str(payload.get("summary", "")),
        "CFGDRIFT_DRIFT_ITEMS_JSON": json.dumps(
            payload.get("drift_items", []), ensure_ascii=False
        ),
    }


def render_email_body(payload: Dict[str, object]) -> str:
    """Plain-text email body: summary + drift item detail lines."""
    lines = [
        str(payload.get("summary", "")),
        "baseline: %s" % payload.get("baseline", ""),
        "target: %s" % payload.get("target", ""),
        "severity: %s" % payload.get("severity", ""),
        "drift_count: %s" % payload.get("drift_count", 0),
        "",
    ]
    for i, item in enumerate(payload.get("drift_items", []), 1):
        if not isinstance(item, dict):
            continue
        lines.append(
            "%d. [%s] %s (file=%s, %s): %r -> %r"
            % (
                i,
                item.get("severity", ""),
                item.get("key", ""),
                item.get("file", ""),
                item.get("change_type", ""),
                item.get("baseline"),
                item.get("current"),
            )
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Channel base + implementations
# ---------------------------------------------------------------------------

class Channel:
    """Abstract alert channel.

    Subclasses implement :meth:`send` which raises :class:`ChannelError` on
    any delivery failure.  ``test(payload)`` reuses :meth:`send` with a
    sample payload (``alert test`` connectivity check).
    """

    type = "base"

    def send(self, payload: Dict[str, object]) -> None:
        raise NotImplementedError

    def test(self, payload: Dict[str, object]) -> None:
        self.send(payload)


class WebhookChannel(Channel):
    """POST the payload as JSON to a URL (urllib.request)."""

    type = "webhook"

    def __init__(self, config: Dict[str, object]) -> None:
        self.url = config.get("url")
        self.headers = dict(config.get("headers") or {})
        self.timeout = float(config.get("timeout", 10) or 10)

    def send(self, payload: Dict[str, object]) -> None:
        if not self.url:
            raise ChannelError("webhook url is not configured")
        headers = {"Content-Type": "application/json"}
        for key, value in self.headers.items():
            headers[str(key)] = substitute(str(value), env=os.environ)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.url, data=body, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status = getattr(resp, "status", None)
                if status is None:
                    status = resp.getcode() or 200
                if int(status) >= 400:
                    raise ChannelError(
                        "webhook returned HTTP %d for %s" % (int(status), self.url)
                    )
        except urllib.error.HTTPError as exc:
            raise ChannelError(
                "webhook returned HTTP %d for %s" % (exc.code, self.url)
            ) from exc
        except urllib.error.URLError as exc:
            reason = exc.reason if exc.reason is not None else exc
            raise ChannelError("webhook request failed: %s" % reason) from exc


class EmailChannel(Channel):
    """Send an email via SMTP (STARTTLS by default, optional implicit SSL).

    The SMTP password is never stored in ``alerts.yaml``: only the name of an
    environment variable (``smtp_password_env``) is persisted and resolved at
    send time via ``os.environ``.
    """

    type = "email"

    _DEFAULT_SUBJECT = "[cfgdrift] {severity} drift in {baseline}"

    def __init__(self, config: Dict[str, object]) -> None:
        self.host = config.get("smtp_host")
        self.port = int(config.get("smtp_port", 587) or 587)
        self.user = config.get("smtp_user")
        self.from_addr = config.get("smtp_from")
        to_value = config.get("smtp_to") or []
        if isinstance(to_value, str):
            to_value = [to_value]
        self.to_addrs = [str(addr) for addr in to_value]
        self.password_env = config.get("smtp_password_env")
        self.use_tls = bool(config.get("use_tls", True))
        self.use_ssl = bool(config.get("use_ssl", False))
        self.subject_template = config.get("subject_template") or self._DEFAULT_SUBJECT
        self.timeout = float(config.get("timeout", 15) or 15)

    def _build_message(self, payload: Dict[str, object]) -> EmailMessage:
        ctx = payload_context(payload)
        subject = substitute(str(self.subject_template), ctx, env=os.environ)
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.from_addr or ""
        msg["To"] = ", ".join(self.to_addrs)
        msg.set_content(render_email_body(payload))
        return msg

    def send(self, payload: Dict[str, object]) -> None:
        if not self.host:
            raise ChannelError("email smtp_host is not configured")
        if not self.from_addr:
            raise ChannelError("email smtp_from is not configured")
        if not self.to_addrs:
            raise ChannelError("email smtp_to is not configured")

        password = None
        if self.password_env:
            password = os.environ.get(self.password_env)
            if password is None:
                raise ChannelError(
                    "environment variable %r (smtp_password_env) is not set"
                    % self.password_env
                )
        msg = self._build_message(payload)
        try:
            if self.use_ssl:
                server = smtplib.SMTP_SSL(
                    self.host, self.port, timeout=self.timeout
                )
            else:
                server = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
            with server:
                if self.use_tls and not self.use_ssl:
                    server.starttls()
                login_user = self.user or self.from_addr
                if login_user or password:
                    server.login(login_user, password or "")
                server.send_message(msg)
        except (smtplib.SMTPException, OSError) as exc:
            raise ChannelError("email send failed: %s" % exc) from exc


class ScriptChannel(Channel):
    """Run a command with drift info via ``CFGDRIFT_*`` env vars (argv为辅)."""

    type = "script"

    def __init__(self, config: Dict[str, object]) -> None:
        self.command = config.get("command")
        self.args = [str(arg) for arg in (config.get("args") or [])]
        self.timeout = float(config.get("timeout", 30) or 30)

    def _build_env(self, payload: Dict[str, object]) -> Dict[str, str]:
        env = os.environ.copy()
        env.update(payload_env(payload))
        return env

    def _build_command(self, payload: Dict[str, object]) -> List[str]:
        ctx = payload_context(payload)
        args = [substitute(str(arg), ctx, env=os.environ) for arg in self.args]
        return [str(self.command)] + args

    def send(self, payload: Dict[str, object]) -> None:
        if not self.command:
            raise ChannelError("script command is not configured")
        cmd = self._build_command(payload)
        env = self._build_env(payload)
        try:
            proc = subprocess.run(
                cmd,
                env=env,
                timeout=self.timeout,
                capture_output=True,
                text=True,
            )
        except subprocess.TimeoutExpired as exc:
            raise ChannelError(
                "script %r timed out after %ss" % (self.command, self.timeout)
            ) from exc
        except OSError as exc:
            raise ChannelError(
                "failed to run script %r: %s" % (self.command, exc)
            ) from exc
        if proc.returncode != 0:
            detail = (proc.stderr or "").strip() or (proc.stdout or "").strip()
            raise ChannelError(
                "script %r exited with code %d%s"
                % (self.command, proc.returncode, ": %s" % detail if detail else "")
            )


def build_channel(rule) -> Channel:
    """Instantiate the channel for an :class:`AlertRule`."""
    if rule.type == "webhook":
        return WebhookChannel(rule.config)
    if rule.type == "email":
        return EmailChannel(rule.config)
    if rule.type == "script":
        return ScriptChannel(rule.config)
    raise ChannelError("unsupported alert type %r" % rule.type)


# ---------------------------------------------------------------------------
# Retry with backoff
# ---------------------------------------------------------------------------

def retry_with_backoff(
    send_fn: Callable[[], None],
    attempts: int = 3,
    delays: tuple = (1, 5, 30),
    sleep_fn: Callable[[float], None] = time.sleep,
) -> int:
    """Call ``send_fn`` up to ``attempts`` times.

    Returns the number of attempts used on success.  After all attempts fail,
    raises :class:`ChannelError` (the last error).  ``sleep_fn`` is injectable
    so tests can avoid real sleeps.
    """
    last_error: Optional[ChannelError] = None
    used = 0
    for i in range(int(attempts)):
        used = i + 1
        try:
            send_fn()
            return used
        except ChannelError as exc:
            last_error = exc
            if i < int(attempts) - 1:
                delay = delays[i] if i < len(delays) else delays[-1]
                logger.warning(
                    "channel attempt %d/%d failed: %s; retrying in %ss",
                    used,
                    attempts,
                    exc,
                    delay,
                )
                sleep_fn(float(delay))
    raise ChannelError(str(last_error)) from last_error
