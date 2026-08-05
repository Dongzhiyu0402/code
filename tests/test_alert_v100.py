"""cfgdrift v0.10.0 alert tests (P0-1): mute_until model + daemon reload.

Covers the acceptance surface of V10-P0-1:
- ``parse_iso_utc`` normalization (trailing ``Z``, offset conversion, naive
  treated as UTC) and strict rejection of malformed input;
- ``AlertRule.mute_until`` optional field + ``is_muted`` lexicographic
  boundary (``now == mute_until`` is NOT muted), to/from_dict round-trip;
- ``AlertConfig.set_mute`` / ``clear_mute`` persist to alerts.yaml and raise
  ``ValueError`` for unknown rules / invalid timestamps;
- dispatcher: a muted rule is skipped entirely (no channel call, no event
  row, no cooldown write) while other rules still dispatch normally (D2);
- daemon: ``_reload_alert_rules`` picks a mid-run ``alert mute`` up on the
  next cycle (D1), keeps previous rules on a corrupt file, and is a no-op
  without a dispatcher;
- CLI ``alert mute/unmute``: exit 0 on success, exit 2 on unknown rule /
  invalid timestamp, interoperable with the Web write path.
"""

from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

from cfgdrift.alert.config import AlertConfig  # noqa: E402
from cfgdrift.alert.dispatcher import AlertDispatcher  # noqa: E402
from cfgdrift.alert.models import (  # noqa: E402
    AlertRule,
    parse_iso_utc,
)
from cfgdrift.alert.state import AlertStateStore  # noqa: E402
from cfgdrift.core.model import (  # noqa: E402
    ChangeType,
    DriftItem,
    Report,
    ScanSummary,
    Severity,
)
from cfgdrift.storage.store import Store, utcnow_iso  # noqa: E402

_FUTURE = "2099-01-01T00:00:00+00:00"
_PAST = "2000-01-01T00:00:00+00:00"


def _rule(name="drift-wx", mute_until=None):
    return AlertRule(
        name=name,
        type="webhook",
        severity=Severity.WARN,
        config={"url": "https://example.invalid/x"},
        mute_until=mute_until,
    )


def _item(key="server.port"):
    return DriftItem(
        key_path=key,
        change_type=ChangeType.MODIFIED,
        severity=Severity.WARN,
        file="conf/app.json",
        old_value="8080",
        new_value="9090",
    )


def _report():
    summary = ScanSummary()
    summary.max_severity = Severity.WARN
    return Report(
        scan_id=1,
        baseline=None,
        created_at=utcnow_iso(),
        mode="daemon",
        summary=summary,
        items=[_item()],
    )


# ---------------------------------------------------------------------------
# model: parse_iso_utc / mute_until / is_muted
# ---------------------------------------------------------------------------


class TestParseIsoUtc:
    def test_z_suffix_tolerated(self):
        assert parse_iso_utc("2099-01-01T00:00:00Z") == _FUTURE

    def test_offset_converted_to_utc(self):
        assert (
            parse_iso_utc("2099-01-01T08:00:00+08:00") == _FUTURE
        )

    def test_naive_treated_as_utc(self):
        assert parse_iso_utc("2099-01-01T00:00:00") == _FUTURE

    def test_invalid_raises(self):
        for bad in ("not-a-date", "2026-13-45T99:99:99", "", 123, None):
            with pytest.raises(ValueError):
                parse_iso_utc(bad)


class TestAlertRuleMute:
    def test_default_not_muted(self):
        rule = _rule()
        assert rule.mute_until is None
        assert rule.is_muted() is False

    def test_muted_inside_window(self):
        rule = _rule(mute_until=_FUTURE)
        assert rule.is_muted(now="2098-06-01T00:00:00+00:00") is True

    def test_boundary_not_muted(self):
        rule = _rule(mute_until="2026-08-06T09:00:00+00:00")
        assert rule.is_muted(now="2026-08-06T09:00:00+00:00") is False

    def test_expired_not_muted(self):
        rule = _rule(mute_until=_PAST)
        assert rule.is_muted(now=utcnow_iso()) is False

    def test_normalized_in_post_init(self):
        rule = _rule(mute_until="2099-01-01T00:00:00Z")
        assert rule.mute_until == _FUTURE

    def test_to_dict_zero_noise(self):
        assert "mute_until" not in _rule().to_dict()
        assert _rule(mute_until=_FUTURE).to_dict()["mute_until"] == _FUTURE

    def test_from_dict_round_trip(self):
        rule = AlertRule.from_dict(
            {
                "name": "r",
                "type": "webhook",
                "severity": "WARN",
                "config": {"url": "https://x"},
                "mute_until": "2099-01-01T00:00:00Z",
            }
        )
        assert rule.mute_until == _FUTURE
        legacy = AlertRule.from_dict(
            {"name": "r", "type": "webhook", "severity": "WARN",
             "config": {"url": "https://x"}}
        )
        assert legacy.mute_until is None

    def test_invalid_mute_until_rejected(self):
        with pytest.raises(ValueError):
            _rule(mute_until="garbage")
        with pytest.raises(ValueError):
            AlertRule.from_dict(
                {"name": "r", "type": "webhook", "severity": "WARN",
                 "config": {"url": "u"}, "mute_until": 123}
            )


# ---------------------------------------------------------------------------
# config: set_mute / clear_mute
# ---------------------------------------------------------------------------


class TestAlertConfigMute:
    def _path(self, tmp_path):
        path = os.path.join(str(tmp_path), "alerts.yaml")
        AlertConfig.save(path, [_rule()])
        return path

    def test_set_mute_persists(self, tmp_path):
        path = self._path(tmp_path)
        AlertConfig.set_mute(path, "drift-wx", "2099-01-01T00:00:00Z")
        rule = AlertConfig.load(path)[0]
        assert rule.mute_until == _FUTURE
        assert rule.is_muted() is True

    def test_clear_mute_persists(self, tmp_path):
        path = self._path(tmp_path)
        AlertConfig.set_mute(path, "drift-wx", "2099-01-01T00:00:00Z")
        AlertConfig.clear_mute(path, "drift-wx")
        assert AlertConfig.load(path)[0].mute_until is None

    def test_unknown_rule_raises(self, tmp_path):
        path = self._path(tmp_path)
        with pytest.raises(ValueError):
            AlertConfig.set_mute(path, "nope", "2099-01-01T00:00:00Z")
        with pytest.raises(ValueError):
            AlertConfig.clear_mute(path, "nope")

    def test_invalid_until_raises(self, tmp_path):
        path = self._path(tmp_path)
        with pytest.raises(ValueError):
            AlertConfig.set_mute(path, "drift-wx", "garbage")


# ---------------------------------------------------------------------------
# dispatcher: mute skips the whole rule (D2)
# ---------------------------------------------------------------------------


class _FakeChannel:
    def __init__(self, ok=True):
        self.ok = ok
        self.sent = 0

    def send(self, payload):
        self.sent += 1
        if not self.ok:
            raise RuntimeError("channel failed")
        return None


class TestDispatcherMute:
    def _dispatcher(self, tmp_path, rules, monkeypatch):
        state = AlertStateStore(os.path.join(str(tmp_path), "alert_state.json"))
        store = Store(os.path.join(str(tmp_path), "events.db"))
        dispatcher = AlertDispatcher(rules, state, event_sink=store)
        fake = {"channels": {}, "next_id": 0}

        def fake_build(rule):
            key = rule.name
            if key not in fake["channels"]:
                fake["channels"][key] = _FakeChannel()
            return fake["channels"][key]

        monkeypatch.setattr("cfgdrift.alert.dispatcher.build_channel", fake_build)
        return dispatcher, fake, store

    def test_muted_rule_never_dispatched(self, tmp_path, monkeypatch):
        dispatcher, fake, store = self._dispatcher(
            tmp_path, [_rule(mute_until=_FUTURE)], monkeypatch
        )
        results = dispatcher.dispatch_report("prod", "/etc/app", _report())
        # No channel call, no event row, no cooldown entry.
        assert fake["channels"] == {}
        assert store.count_alert_events() == 0
        assert AlertStateStore(
            os.path.join(str(tmp_path), "alert_state.json")
        ).entries() == {}
        assert results == []

    def test_expired_rule_dispatches_normally(self, tmp_path, monkeypatch):
        dispatcher, fake, store = self._dispatcher(
            tmp_path, [_rule(mute_until=_PAST)], monkeypatch
        )
        results = dispatcher.dispatch_report("prod", "/etc/app", _report())
        assert fake["channels"]["drift-wx"].sent == 1
        assert store.count_alert_events() == 1
        assert results and results[0].sent is True

    def test_other_rules_unaffected_zero_noise(self, tmp_path, monkeypatch):
        rules = [_rule(name="muted", mute_until=_FUTURE),
                 _rule(name="normal")]
        dispatcher, fake, store = self._dispatcher(tmp_path, rules, monkeypatch)
        results = dispatcher.dispatch_report("prod", "/etc/app", _report())
        # Only the non-muted rule dispatched.
        assert "muted" not in fake["channels"]
        assert fake["channels"]["normal"].sent == 1
        events = store.list_alert_events()["events"]
        assert len(events) == 1
        assert events[0]["rule"] == "normal"
        assert [r.rule.name for r in results] == ["normal"]

    def test_test_and_retry_bypass_mute(self, tmp_path, monkeypatch):
        # D4: alert test and event retry are explicit manual actions that
        # deliberately ignore the mute window.
        rule = _rule(mute_until=_FUTURE)
        dispatcher, fake, store = self._dispatcher(
            tmp_path, [rule], monkeypatch
        )
        result = dispatcher.test_rule(rule)
        assert result.sent is True
        assert fake["channels"]["drift-wx"].sent == 1
        event_id = store.add_alert_event(
            {"rule": "drift-wx", "baseline": "prod", "severity": "WARN",
             "status": "failed", "target": "x", "drift_count": 1,
             "error": "old", "attempts": 1}
        )
        retry = dispatcher.retry_event(store.get_alert_event(event_id))
        assert retry.sent is True
        assert fake["channels"]["drift-wx"].sent == 2


# ---------------------------------------------------------------------------
# daemon: per-cycle alerts.yaml reload (D1)
# ---------------------------------------------------------------------------


class TestDaemonReload:
    def _worker(self, tmp_path, monkeypatch):
        from cfgdrift.daemon.worker import DaemonWorker

        alerts_path = os.path.join(str(tmp_path), "alerts.yaml")
        AlertConfig.save(alerts_path, [_rule()])
        state = AlertStateStore(os.path.join(str(tmp_path), "alert_state.json"))
        dispatcher = AlertDispatcher([_rule()], state)
        worker = DaemonWorker(
            store_path=os.path.join(str(tmp_path), "db.sqlite"),
            paths=[],
            fmt="auto",
            baseline_name="prod",
            interval=60,
            dispatcher=dispatcher,
            alerts_config=alerts_path,
            home=str(tmp_path),
        )
        return worker, dispatcher, alerts_path

    def test_reload_applies_midrun_mute(self, tmp_path, monkeypatch):
        worker, dispatcher, alerts_path = self._worker(tmp_path, monkeypatch)
        assert dispatcher.rules[0].is_muted() is False
        # A Web/CLI mute written while the daemon runs...
        AlertConfig.set_mute(alerts_path, "drift-wx", "2099-01-01T00:00:00Z")
        worker._reload_alert_rules()
        # ...takes effect on the next cycle without a restart.
        assert dispatcher.rules[0].is_muted() is True

    def test_corrupt_file_keeps_previous_rules(self, tmp_path, monkeypatch):
        worker, dispatcher, alerts_path = self._worker(tmp_path, monkeypatch)
        with open(alerts_path, "w", encoding="utf-8") as fh:
            fh.write("version: 99\nrules: [broken\n")
        worker._reload_alert_rules()  # must not raise
        assert dispatcher.rules[0].name == "drift-wx"

    def test_no_dispatcher_noop(self, tmp_path, monkeypatch):
        from cfgdrift.daemon.worker import DaemonWorker

        worker = DaemonWorker(
            store_path=os.path.join(str(tmp_path), "db.sqlite"),
            paths=[],
            fmt="auto",
            baseline_name="prod",
            interval=60,
            dispatcher=None,
            alerts_config=os.path.join(str(tmp_path), "alerts.yaml"),
            home=str(tmp_path),
        )
        worker._reload_alert_rules()  # no-op, must not raise

    def test_cycle_calls_reload(self, tmp_path, monkeypatch):
        worker, dispatcher, alerts_path = self._worker(tmp_path, monkeypatch)
        called = {"n": 0}
        original = worker._reload_alert_rules

        def spy():
            called["n"] += 1
            return original()

        worker._reload_alert_rules = spy
        worker._constraints = []
        worker.paths = []  # no scan targets -> cycle only reloads
        worker._cycle(store=None)
        assert called["n"] >= 1


# ---------------------------------------------------------------------------
# CLI alert mute/unmute
# ---------------------------------------------------------------------------


class TestCliAlertMute:
    def test_mute_unmute_cli(self, tmp_path, monkeypatch, capsys):
        from cfgdrift.cli import main

        home = str(tmp_path)
        os.makedirs(home, exist_ok=True)
        path = os.path.join(home, "alerts.yaml")
        AlertConfig.save(path, [_rule()])
        monkeypatch.setenv("CFGDRIFT_HOME", home)
        rc = main(["alert", "mute", "drift-wx", "--until", "2099-01-01T00:00:00Z"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "muted until 2099-01-01T00:00:00+00:00" in out
        assert AlertConfig.load(path)[0].is_muted() is True
        rc = main(["alert", "unmute", "drift-wx"])
        assert rc == 0
        assert "unmuted" in capsys.readouterr().out
        assert AlertConfig.load(path)[0].mute_until is None

    def test_errors_exit_2(self, tmp_path, monkeypatch, capsys):
        from cfgdrift.cli import main

        home = str(tmp_path)
        os.makedirs(home, exist_ok=True)
        AlertConfig.save(os.path.join(home, "alerts.yaml"), [_rule()])
        monkeypatch.setenv("CFGDRIFT_HOME", home)
        assert main(["alert", "mute", "nope", "--until", "2099-01-01T00:00:00Z"]) == 2
        assert main(["alert", "mute", "drift-wx", "--until", "junk"]) == 2
        assert main(["alert", "unmute", "nope"]) == 2
