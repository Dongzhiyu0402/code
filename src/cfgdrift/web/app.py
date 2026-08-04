"""FastAPI application factory for the cfgdrift web dashboard.

The optional dependencies (fastapi / uvicorn) are imported lazily so that
``import cfgdrift.web`` never fails when the ``[web]`` extra is missing.

v0.4.0 additions: ``/api/alerts`` (alert rules from alerts.yaml),
``/api/alert-events`` (paginated delivery events), ``/api/daemon-status``,
``/api/file-snippet`` (line-context viewer constrained to baseline scan
roots), masked report responses, and daemon status in the overview.
"""

# NOTE: this module deliberately does **not** use ``from __future__ import
# annotations``: the route handlers annotate ``request: Request`` with the
# FastAPI ``Request`` class imported *inside* ``create_app`` (so that
# ``import cfgdrift.web`` never requires the ``[web]`` extra).  With PEP 563
# lazy annotations those would become the string ``'Request'`` which FastAPI
# cannot resolve against the closure scope, breaking body parsing (422).

import os
from typing import Any, Dict, Optional

from .. import __version__
from ..alert.config import AlertConfig
from ..core.masker import SensitiveMasker, masking_config_path
from ..rules.ignore import make_rule

_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def _default_home() -> str:
    return os.environ.get("CFGDRIFT_HOME") or os.path.join(
        os.path.expanduser("~"), ".cfgdrift"
    )


def create_app(store, home: Optional[str] = None):
    """Create the FastAPI application bound to a :class:`Store`.

    ``home`` (v0.4.0) is the cfgdrift data directory used to resolve
    ``alerts.yaml`` / ``masking.yaml`` and the daemon PID/info files; it
    defaults to ``CFGDRIFT_HOME`` or ``~/.cfgdrift``.
    """
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import JSONResponse
    from fastapi.staticfiles import StaticFiles

    home = os.path.abspath(home) if home else _default_home()
    masker = SensitiveMasker.from_config(masking_config_path(home))

    app = FastAPI(title="cfgdrift dashboard", version=__version__)

    def ok(data: Any = None) -> JSONResponse:
        return JSONResponse({"code": 0, "data": data, "message": "ok"})

    def err(message: str, status: int = 400) -> JSONResponse:
        return JSONResponse(
            {"code": 2, "data": None, "message": message}, status_code=status
        )

    @app.exception_handler(ValueError)
    async def value_error_handler(request: Request, exc: ValueError):
        return err(str(exc), status=400)

    # -- API -------------------------------------------------------------

    @app.get("/api/overview")
    def api_overview():
        scans = store.list_scans(limit=50)
        baselines = store.list_baselines()
        latest = scans[0] if scans else None

        severity_distribution = {
            "CRITICAL": 0,
            "WARN": 0,
            "INFO": 0,
            "NONE": 0,
        }
        totals = {
            "added": 0,
            "removed": 0,
            "modified": 0,
            "type_changed": 0,
            "ignored": 0,
            "total": 0,
        }
        for s in scans:
            sev = s["summary"].get("max_severity", "NONE")
            severity_distribution[sev] = severity_distribution.get(sev, 0) + 1
            for k in totals:
                totals[k] += int(s["summary"].get(k, 0))

        daemon_status = None
        try:
            from ..daemon.daemon import DaemonManager

            daemon_status = DaemonManager(home).status_dict()
            if daemon_status.get("running"):
                try:
                    daemon_status["last_scan"] = scans[0] if scans else None
                except Exception:  # noqa: BLE001
                    daemon_status["last_scan"] = None
        except Exception:  # noqa: BLE001 - daemon status is best-effort
            daemon_status = {"running": False, "pid": None, "error": "unavailable"}

        return ok(
            {
                "latest_scan": latest,
                "timeline": scans,
                "severity_distribution": severity_distribution,
                "totals": totals,
                "baseline_count": len(baselines),
                "scan_count": len(scans),
                "daemon_status": daemon_status,
            }
        )

    @app.get("/api/reports/{scan_id}")
    def api_report(scan_id: int):
        try:
            payload = store.get_scan(scan_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        # Inject the baseline scan root so the SPA can call /api/file-snippet
        # with a root that passes the known-roots check.
        baseline_ref = (payload.get("data") or {}).get("baseline")
        if baseline_ref and baseline_ref.get("name"):
            try:
                bl = store.get_baseline(baseline_ref["name"])
                payload["data"]["scan_root"] = bl.scan_root
            except ValueError:
                pass
        # v0.4.0: mask sensitive values at the Web display exit; the database
        # keeps raw values.
        masker.mask_payload(payload)
        return payload

    @app.get("/api/reports/{scan_id}/html")
    def api_report_html(scan_id: int):
        """Standalone offline HTML report (v0.5.0) — same renderer as the CLI.

        Data flow (D6): ``store.get_scan`` -> ``mask_payload`` ->
        ``HtmlReporter.render_html``, so the Web export and
        ``cfgdrift report --html`` are structurally identical.
        """
        from fastapi.responses import Response

        try:
            payload = store.get_scan(scan_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if payload.get("code") != 0:
            return err(payload.get("message", "scan report is invalid"))
        masker.mask_payload(payload)
        from ..core.htmlreport import HtmlReporter

        html = HtmlReporter.render_html(
            payload["data"], title="cfgdrift report #%s" % scan_id
        )
        return Response(content=html, media_type="text/html; charset=utf-8")

    @app.post("/api/compare")
    async def api_compare(request: Request):
        """Compare two environments' baselines (v0.5.0).

        Contract: ``{"env1": ..., "env2": ...}`` -> 200 with the masked
        :class:`CompareReport` (plus per-item ``snippet_root``); 400 for
        missing/identical environments; 404 for an uncollected baseline.
        """
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - malformed JSON body
            return err("invalid JSON body")
        env1 = str(body.get("env1", "")).strip()
        env2 = str(body.get("env2", "")).strip()
        if not env1 or not env2:
            return err("env1 and env2 are required")
        if env1 == env2:
            return err("env1 and env2 must be different")

        from ..core.compare import CompareEngine
        from ..rules.severity import SeverityConfig
        from ..rules.severity import default_path as severity_config_path

        engine = CompareEngine(store)
        env_map = engine.load_environments(home)
        baseline1 = engine.resolve_baseline_name(env1, env_map)
        baseline2 = engine.resolve_baseline_name(env2, env_map)

        # Baselines must exist; otherwise a readable 404 (D7).
        scan_roots: Dict[str, str] = {}
        for env, baseline_name in ((env1, baseline1), (env2, baseline2)):
            try:
                bl = store.get_baseline(baseline_name)
                scan_roots[baseline_name] = bl.scan_root
            except ValueError as exc:
                return err(
                    "环境 %s 未采集基线（%s）" % (env, exc),
                    status=404,
                )

        severity_rules = []
        sev_path = severity_config_path(home)
        if os.path.exists(sev_path):
            severity_rules = SeverityConfig.load(sev_path)

        try:
            reports = engine.compare(
                [env1, env2],
                env_map=env_map,
                severity_rules=severity_rules,
                masker=masker,
            )
        except ValueError as exc:
            return err(str(exc), status=404)

        data = reports[0].to_dict()
        # Inject the snippet root per item based on the line source side
        # (removed -> env1 baseline root, everything else -> env2 root).
        for item in data.get("items", []):
            if item.get("change_type") == "removed":
                item["snippet_root"] = scan_roots.get(baseline1, "")
            else:
                item["snippet_root"] = scan_roots.get(baseline2, "")
        return ok(data)

    @app.get("/api/baselines")
    def api_baselines():
        rows = store.list_baselines()
        return ok({"baselines": [b.to_dict() for b in rows]})

    @app.get("/api/rules")
    def api_rules(baseline_id: Optional[int] = None):
        rows = store.list_rules(baseline_id)
        return ok({"rules": [r.to_dict() for r in rows]})

    @app.post("/api/rules")
    async def api_rules_create(request: Request):
        body = await request.json()
        name = str(body.get("name", "")).strip()
        key_pattern = str(body.get("key_pattern", "")).strip()
        match_type = str(body.get("match_type", "path_exact"))
        if not name or not key_pattern:
            return err("name and key_pattern are required")
        try:
            rule = make_rule(
                name=name,
                key_pattern=key_pattern,
                match_type=match_type,
                baseline_id=body.get("baseline_id"),
                file_pattern=body.get("file_pattern"),
                change_type=body.get("change_type"),
                enabled=bool(body.get("enabled", True)),
            )
        except ValueError as exc:
            return err(str(exc))
        rule_id = store.add_rule(rule)
        return ok({"id": rule_id})

    @app.delete("/api/rules/{rule_id}")
    def api_rules_delete(rule_id: int):
        try:
            store.delete_rule(rule_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return ok({"deleted": True})

    # -- v0.4.0: alert rules / events / daemon status --------------------

    @app.get("/api/alerts")
    def api_alerts():
        """List alert rules from <home>/alerts.yaml (empty when absent)."""
        try:
            rules = AlertConfig.list_rules(os.path.join(home, "alerts.yaml"))
            return ok({"alerts": [r.to_dict() for r in rules]})
        except ValueError as exc:
            return err(str(exc))

    @app.get("/api/alert-events")
    def api_alert_events(
        rule: Optional[str] = None,
        status: Optional[str] = None,
        severity: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ):
        """List alert delivery events with filters + pagination."""
        try:
            result = store.list_alert_events(
                rule=rule,
                status=status,
                severity=severity,
                limit=min(max(1, int(limit)), 500),
                offset=max(0, int(offset)),
            )
        except ValueError as exc:
            return err(str(exc))
        return ok(result)

    @app.get("/api/daemon-status")
    def api_daemon_status():
        """Return daemon status + the most recent scan (no printing)."""
        try:
            from ..daemon.daemon import DaemonManager

            status = DaemonManager(home).status_dict()
        except Exception as exc:  # noqa: BLE001 - best-effort
            return err(str(exc))
        status["last_scan"] = store.list_scans(1)[0] if store.list_scans(1) else None
        return ok(status)

    @app.get("/api/file-snippet")
    def api_file_snippet(root: str, file: str, line: int):
        """Return ``line +/- 5`` context of a config file.

        ``root`` must be the scan root of an existing baseline (defense in
        depth: the file is ``realpath``-checked to stay inside ``root``, so
        directory traversal is rejected).
        """
        try:
            line_no = max(1, int(line))
        except (TypeError, ValueError):
            return err("line must be an integer")
        try:
            known_roots = {
                os.path.realpath(b.scan_root) for b in store.list_baselines()
            }
        except Exception:  # noqa: BLE001
            known_roots = set()
        real_root = os.path.realpath(root)
        if real_root not in known_roots:
            return err("root is not a known baseline scan root", status=403)
        full = os.path.realpath(os.path.join(real_root, file))
        try:
            common = os.path.commonpath([real_root, full])
        except ValueError:
            common = ""
        if common != real_root or not os.path.isfile(full):
            return err("file escapes the scan root", status=403)
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        except OSError as exc:
            return err("cannot read file: %s" % exc)
        start = max(0, line_no - 6)
        end = min(len(lines), line_no + 5)
        snippet = [
            {"line": i + 1, "text": lines[i]} for i in range(start, end)
        ]
        return ok(
            {
                "root": real_root,
                "file": file,
                "line": line_no,
                "total_lines": len(lines),
                "snippet": snippet,
            }
        )

    @app.get("/api/health")
    def api_health():
        return ok({"status": "ok"})

    # -- v0.7.0: constraints view (C-09) + violation events (C-10) --------

    @app.get("/api/constraints")
    def api_constraints():
        """List the effective constraints (D6: ``resolve(home, [], True)``).

        Same view as ``cfgdrift constraint list --source all``: built-in
        library merged with user rules, same-id user rules overriding
        built-ins.
        """
        from ..core.model import Severity
        from ..rules.constraints import (
            resolve as resolve_constraints,
        )

        try:
            rules = resolve_constraints(home, [], builtin_enabled=True)
        except ValueError as exc:
            return err(str(exc))
        constraints = []
        for rule in rules:
            severity = rule.severity
            constraints.append(
                {
                    "id": rule.id,
                    "type": rule.type,
                    "keys": list(rule.keys),
                    "severity": severity.value
                    if isinstance(severity, Severity)
                    else str(severity),
                    "enabled": bool(rule.enabled),
                    "source": rule.source,
                    "message": rule.message,
                }
            )
        return ok({"constraints": constraints})

    @app.put("/api/constraints/{constraint_id}/enabled")
    async def api_constraints_set_enabled(constraint_id: str, request: Request):
        """Enable/disable a user constraint (built-in rules -> 400, Q5)."""
        from ..rules.constraints import (
            ConstraintConfig,
            default_path as constraints_config_path,
            resolve as resolve_constraints,
        )

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - malformed JSON body
            return err("invalid JSON body")
        enabled = bool(body.get("enabled", True))
        try:
            rules = resolve_constraints(home, [], builtin_enabled=True)
        except ValueError as exc:
            return err(str(exc))
        rule = next((r for r in rules if r.id == constraint_id), None)
        if rule is None:
            return err(
                "constraint %r not found" % constraint_id, status=404
            )
        if rule.source == "builtin":
            return err(
                "内置约束不可直接切换；可添加同 id 用户规则覆盖", status=400
            )
        path = constraints_config_path(home)
        try:
            ConstraintConfig.set_enabled(path, constraint_id, enabled)
        except ValueError as exc:
            return err(str(exc), status=404)
        return ok({"id": constraint_id, "enabled": enabled})

    @app.get("/api/constraint-events")
    def api_constraint_events(
        constraint_id: Optional[str] = None,
        kind: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ):
        """List constraint violations with filters + pagination (C-10)."""
        try:
            result = store.list_constraint_violations(
                constraint_id=constraint_id,
                kind=kind,
                limit=min(max(1, int(limit)), 500),
                offset=max(0, int(offset)),
            )
        except ValueError as exc:
            return err(str(exc))
        return ok(result)

    # -- static ----------------------------------------------------------

    if os.path.isdir(_STATIC_DIR):
        app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="static")

    return app
