"""FastAPI application factory for the cfgdrift web dashboard.

The optional dependencies (fastapi / uvicorn) are imported lazily so that
``import cfgdrift.web`` never fails when the ``[web]`` extra is missing.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from .. import __version__
from ..rules.ignore import make_rule

_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def create_app(store):
    """Create the FastAPI application bound to a :class:`Store`."""
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import JSONResponse
    from fastapi.staticfiles import StaticFiles

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

        return ok(
            {
                "latest_scan": latest,
                "timeline": scans,
                "severity_distribution": severity_distribution,
                "totals": totals,
                "baseline_count": len(baselines),
                "scan_count": len(scans),
            }
        )

    @app.get("/api/reports/{scan_id}")
    def api_report(scan_id: int):
        try:
            return store.get_scan(scan_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

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

    @app.get("/api/health")
    def api_health():
        return ok({"status": "ok"})

    # -- static ----------------------------------------------------------

    if os.path.isdir(_STATIC_DIR):
        app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="static")

    return app
