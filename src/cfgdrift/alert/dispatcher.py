"""AlertDispatcher: rule filtering -> dedupe -> payload -> delivery + retry.

The dispatcher implements the v0.3.0 alert pipeline:

1. filter rules (enabled / baseline scope / severity threshold);
2. compute the drift fingerprint and skip rules still in cooldown;
3. build the payload and deliver through the rule's channel with the global
   retry policy (3 attempts, 1s/5s/30s);
4. record success or failure in the :class:`AlertStateStore` — both write a
   10-minute cooldown so failed alerts are not re-tried every scan cycle.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .. import __version__
from .channels import Channel, ChannelError, build_channel, retry_with_backoff
from .models import (
    AlertRule,
    build_drift_payload,
    build_test_payload,
    drift_fingerprint,
)
from .state import AlertStateStore

logger = logging.getLogger("cfgdrift.alert.dispatcher")

_DEFAULT_RETRY_ATTEMPTS = 3
_DEFAULT_RETRY_DELAYS = (1, 5, 30)


@dataclass
class DispatchResult:
    """Outcome of dispatching one rule for one report."""

    rule: AlertRule
    fingerprint: str
    key: str
    attempted: bool
    sent: bool
    attempts: int
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "rule": self.rule.name,
            "type": self.rule.type,
            "fingerprint": self.fingerprint,
            "attempted": self.attempted,
            "sent": self.sent,
            "attempts": self.attempts,
            "error": self.error,
        }


class AlertDispatcher:
    """Coordinates alert rules, dedupe state, and delivery channels."""

    def __init__(
        self,
        rules: List[AlertRule],
        state: AlertStateStore,
        retry_attempts: int = _DEFAULT_RETRY_ATTEMPTS,
        retry_delays: tuple = _DEFAULT_RETRY_DELAYS,
        sleep_fn: Callable[[float], None] = time.sleep,
        version: str = __version__,
        event_sink=None,
        masker=None,
    ) -> None:
        self.rules = list(rules)
        self.state = state
        self.retry_attempts = int(retry_attempts)
        self.retry_delays = tuple(retry_delays)
        self.sleep_fn = sleep_fn
        self.version = version
        # v0.4.0: optional alert-event sink (a Store instance).  Only
        # sent/failed deliveries are recorded; cooldown-suppressed sends and
        # connectivity tests never touch the sink.
        self.event_sink = event_sink
        # v0.4.0: optional masker applied to alert payloads (raw DB values
        # are never changed).
        self.masker = masker

    # -- public API -------------------------------------------------------

    def dispatch_report(
        self,
        baseline_name: str,
        target: str,
        report,
    ) -> List[DispatchResult]:
        """Dispatch a drift report to every matching, non-cooled rule."""
        results: List[DispatchResult] = []
        for rule in self.rules:
            if not self._rule_matches(rule, baseline_name, report):
                continue
            fingerprint = drift_fingerprint(
                baseline_name, target, report.items
            )
            key = self.state.key_for(rule.name, fingerprint)
            if self.state.is_suppressed(key):
                logger.info(
                    "alert %s suppressed (cooldown) key=%s",
                    rule.name,
                    key[:12],
                )
                continue
            payload = build_drift_payload(
                report, baseline_name, target, self.version, masker=self.masker
            )
            meta = {
                "rule": rule.name,
                "fingerprint": fingerprint,
                "baseline": baseline_name,
                "target": target,
            }
            try:
                channel = build_channel(rule)
            except ChannelError as exc:
                self.state.record_failure(key, dict(meta, attempts=0))
                self._record_event(
                    baseline_name=baseline_name,
                    report=report,
                    rule=rule,
                    fingerprint=fingerprint,
                    target=target,
                    status="failed",
                    attempts=0,
                    error=str(exc),
                )
                logger.error("alert %s channel build failed: %s", rule.name, exc)
                results.append(
                    DispatchResult(
                        rule=rule,
                        fingerprint=fingerprint,
                        key=key,
                        attempted=True,
                        sent=False,
                        attempts=0,
                        error=str(exc),
                    )
                )
                continue

            sent, attempts, error = self._send_with_retry(channel, payload)
            if sent:
                self.state.record_success(
                    key, dict(meta, attempts=attempts)
                )
                self._record_event(
                    baseline_name=baseline_name,
                    report=report,
                    rule=rule,
                    fingerprint=fingerprint,
                    target=target,
                    status="sent",
                    attempts=attempts,
                    error=None,
                )
                logger.info(
                    "alert %s sent (attempts=%d) key=%s",
                    rule.name,
                    attempts,
                    key[:12],
                )
            else:
                self.state.record_failure(key, dict(meta, attempts=attempts))
                self._record_event(
                    baseline_name=baseline_name,
                    report=report,
                    rule=rule,
                    fingerprint=fingerprint,
                    target=target,
                    status="failed",
                    attempts=attempts,
                    error=error,
                )
                logger.error(
                    "alert %s failed after %d attempt(s): %s",
                    rule.name,
                    attempts,
                    error,
                )
            results.append(
                DispatchResult(
                    rule=rule,
                    fingerprint=fingerprint,
                    key=key,
                    attempted=True,
                    sent=sent,
                    attempts=attempts,
                    error=error,
                )
            )
        return results

    def test_rule(self, rule: AlertRule) -> DispatchResult:
        """Connectivity test for ``alert test`` (bypasses dedupe/cooldown)."""
        payload = build_test_payload(self.version)
        try:
            channel = build_channel(rule)
            attempts = retry_with_backoff(
                lambda: channel.send(payload),
                attempts=self.retry_attempts,
                delays=self.retry_delays,
                sleep_fn=self.sleep_fn,
            )
            logger.info("alert test %s ok (attempts=%d)", rule.name, attempts)
            return DispatchResult(
                rule=rule,
                fingerprint="",
                key="",
                attempted=True,
                sent=True,
                attempts=attempts,
                error=None,
            )
        except ChannelError as exc:
            logger.error("alert test %s failed: %s", rule.name, exc)
            return DispatchResult(
                rule=rule,
                fingerprint="",
                key="",
                attempted=True,
                sent=False,
                attempts=self.retry_attempts,
                error=str(exc),
            )

    # -- internals --------------------------------------------------------

    def _record_event(
        self,
        baseline_name: str,
        report,
        rule: AlertRule,
        fingerprint: str,
        target: str,
        status: str,
        attempts: int,
        error: Optional[str],
    ) -> None:
        """Write a sent/failed alert event to the optional sink (v0.4.0)."""
        if self.event_sink is None:
            return
        try:
            self.event_sink.add_alert_event(
                {
                    "rule": rule.name,
                    "baseline": baseline_name,
                    "severity": report.summary.max_severity.value,
                    "status": status,
                    "target": target,
                    "drift_count": report.summary.total,
                    "error": error,
                    "attempts": attempts,
                    "fingerprint": fingerprint,
                }
            )
        except Exception as exc:  # noqa: BLE001 - events must not break alerts
            logger.warning("failed to record alert event: %s", exc)

    def _rule_matches(
        self, rule: AlertRule, baseline_name: str, report
    ) -> bool:
        """Apply enabled / baseline-scope / severity-threshold filters."""
        if not rule.enabled:
            return False
        if rule.baseline and rule.baseline != baseline_name:
            return False
        max_severity = report.summary.max_severity
        if max_severity.rank < rule.severity.rank:
            return False
        return True

    def _send_with_retry(
        self, channel: Channel, payload: Dict[str, Any]
    ) -> Tuple[bool, int, Optional[str]]:
        """Deliver with the global retry policy; returns (sent, attempts, err)."""
        try:
            attempts = retry_with_backoff(
                lambda: channel.send(payload),
                attempts=self.retry_attempts,
                delays=self.retry_delays,
                sleep_fn=self.sleep_fn,
            )
            return True, attempts, None
        except ChannelError as exc:
            return False, self.retry_attempts, str(exc)
