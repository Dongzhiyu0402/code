"""cfgdrift command-line interface (click).

Exit codes: 0 = success / no drift, 1 = drift detected, 2 = error.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import click

from . import __version__
from .alert.config import AlertConfig
from .alert.dispatcher import AlertDispatcher
from .alert.models import AlertRule
from .alert.state import AlertStateStore
from .core.compare import CompareEngine
from .core.differ import SemanticDiffer
from .core.masker import SensitiveMasker, masking_config_path
from .core.model import Constraint, IgnoreRule, Report, ScanSummary, Severity
from .core.parser import parse_file, validate_format
from .core.reporter import Reporter
from .daemon.autostart import AutostartManager
from .daemon.daemon import DaemonManager
from .daemon.worker import main as worker_main
from .rules.constraints import (
    ConstraintConfig,
    default_path as constraints_config_path,
    resolve as resolve_constraints,
)
from .rules.ignore import make_rule
from .rules.severity import SeverityConfig, default_path as severity_config_path
from .rules.severity import make_rule as make_severity_rule
from .scanner.scanner import Scanner
from .storage.store import Store, utcnow_iso

_REPORTER = Reporter()
_DIFFER = SemanticDiffer()
_SCANNER = Scanner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _default_store_path() -> str:
    home = os.environ.get("CFGDRIFT_HOME")
    if home:
        return os.path.join(home, "cfgdrift.db")
    return os.path.join(os.path.expanduser("~"), ".cfgdrift", "cfgdrift.db")


def _daemon_home() -> str:
    """Data directory for daemon PID/sentinel/alerts (CFGDRIFT_HOME or ~/.cfgdrift)."""
    return os.environ.get("CFGDRIFT_HOME") or os.path.join(
        os.path.expanduser("~"), ".cfgdrift"
    )


def _open_store(ctx: click.Context) -> Store:
    store_path = ctx.obj.get("store") or _default_store_path()
    return Store(store_path)


def _echo_json(payload: Dict[str, Any]) -> None:
    click.echo(json.dumps(payload, ensure_ascii=False, indent=2))


def _report_payload(report: Report) -> Dict[str, Any]:
    return {"code": 0, "data": report.to_dict(), "message": "ok"}


def _build_masker(extra_keywords: Optional[List[str]] = None) -> SensitiveMasker:
    """Build the display masker from masking.yaml + --sensitive-keys (append).

    Always returns a masker: masking.yaml is optional (defaults apply), and
    the four display exits (terminal / JSON / Web API / alert payload) mask by
    default while the database keeps raw values.
    """
    return SensitiveMasker.from_config(
        masking_config_path(_daemon_home()), extra_keywords=extra_keywords
    )


def _load_severity_rules() -> List[Any]:
    """Load custom severity rules from severity.yaml (empty when absent)."""
    path = severity_config_path(_daemon_home())
    if not os.path.exists(path):
        return []
    return SeverityConfig.load(path)


def _load_constraints(
    extra_paths: Optional[List[str]] = None,
    builtin_enabled: bool = True,
) -> List[Constraint]:
    """Resolve the effective constraints for diff/scan (D8)."""
    return resolve_constraints(
        _daemon_home(), extra_paths=list(extra_paths or []),
        builtin_enabled=builtin_enabled,
    )


# ---------------------------------------------------------------------------
# Top-level group
# ---------------------------------------------------------------------------

@click.group()
@click.option(
    "--store",
    "store_path",
    type=click.Path(),
    default=None,
    help="SQLite database file (default: ~/.cfgdrift/cfgdrift.db or $CFGDRIFT_HOME).",
)
@click.version_option(__version__, prog_name="cfgdrift")
@click.pass_context
def cli(ctx: click.Context, store_path: Optional[str]) -> None:
    """cfgdrift — semantic-level configuration drift detection."""
    ctx.ensure_object(dict)
    ctx.obj["store"] = store_path


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

@cli.command()
@click.pass_context
def init(ctx: click.Context) -> int:
    """Initialize the cfgdrift database."""
    store = _open_store(ctx)
    path = store.db_path
    store.close()
    click.echo("initialized database at %s" % path)
    return 0


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------

def _perform_scan(
    ctx: click.Context,
    path: str,
    fmt: str,
    baseline_name: Optional[str],
    save_as_baseline: Optional[str],
    mode: str,
    description: str = "",
    no_line: bool = False,
    masker: Optional[SensitiveMasker] = None,
    constraints: Optional[List[Constraint]] = None,
) -> int:
    store = _open_store(ctx)
    snapshot, line_maps = _SCANNER.scan_path_with_lines(path, fmt)

    baseline = None
    baseline_id = None
    if baseline_name:
        baseline = store.get_baseline(baseline_name)
        baseline_id = baseline.id

    if save_as_baseline:
        baseline = store.create_baseline(
            name=save_as_baseline,
            description=description,
            scan_root=os.path.abspath(path),
            format=fmt,
            data=snapshot,
            line_maps=line_maps,
        )
        baseline_id = baseline.id

    items: List[Any] = []
    summary = ScanSummary()
    if baseline is not None:
        rules = store.list_rules(baseline_id)
        severity_rules = _load_severity_rules()
        items, summary = _DIFFER.diff_snapshot(
            baseline.data,
            snapshot,
            rules,
            severity_rules=severity_rules,
            old_lines=baseline.line_maps,
            new_lines=line_maps,
            constraints=constraints,
        )

    report = Report(
        scan_id=None,
        baseline=baseline,
        created_at=utcnow_iso(),
        mode=mode,
        summary=summary,
        items=items,
    )
    payload = _report_payload(report)
    scan_id = store.add_scan(baseline_id, mode, payload)
    report.scan_id = scan_id
    store.close()

    if baseline is None:
        click.echo("recorded scan #%d (no baseline comparison)" % scan_id)
        return 0

    click.echo(
        _REPORTER.render_terminal(
            report,
            color=_use_color(ctx),
            masker=masker,
            show_line=not no_line,
        )
    )
    if summary.total > 0:
        return 1
    return 0


def _use_color(ctx: click.Context) -> bool:
    return ctx.obj.get("color", True)


@cli.command()
@click.argument("path", type=click.Path(exists=True))
@click.option("--baseline", "baseline_name", default=None, help="Baseline name to diff against.")
@click.option(
    "--save-as-baseline",
    "save_as_baseline",
    default=None,
    help="Save this snapshot as a new baseline version (same name -> version+1).",
)
@click.option(
    "--format",
    "fmt",
    default="auto",
    help="Config format (auto/json/yaml/toml/ini or a registered parser plugin, v0.5.0).",
)
@click.option("--watch", is_flag=True, help="Watch the path and re-scan periodically.")
@click.option("--interval", default=60, type=int, help="Watch interval in seconds.")
@click.option("--description", default="", help="Description for --save-as-baseline.")
@click.option(
    "--sensitive-keys",
    "sensitive_keys",
    multiple=True,
    help="Extra sensitive key stems to mask (append; default list is always active).",
)
@click.option("--no-line", "no_line", is_flag=True, help="Hide line numbers in output.")
@click.option("--builtin/--no-builtin", "builtin", default=True,
              help="Enable the built-in constraint library (v0.6.0; default: on).")
@click.option("--constraints", "constraint_files", multiple=True,
              type=click.Path(exists=True, dir_okay=False),
              help="Extra constraints.yaml file (repeatable; v0.6.0).")
@click.pass_context
def scan(
    ctx: click.Context,
    path: str,
    baseline_name: Optional[str],
    save_as_baseline: Optional[str],
    fmt: str,
    watch: bool,
    interval: int,
    description: str,
    sensitive_keys: tuple,
    no_line: bool,
    builtin: bool,
    constraint_files: tuple,
) -> int:
    """Scan a file or directory and (optionally) compare against a baseline."""
    if interval <= 0:
        raise ValueError("--interval must be a positive integer")
    masker = _build_masker(list(sensitive_keys))
    constraints = _load_constraints(list(constraint_files), builtin)

    def on_scan(snapshot: Dict[str, object]) -> None:
        _perform_scan(
            ctx,
            path,
            fmt,
            baseline_name,
            save_as_baseline,
            mode="watch",
            description=description,
            no_line=no_line,
            masker=masker,
            constraints=constraints,
        )

    if watch:
        try:
            _SCANNER.watch(path, fmt, interval, on_scan)
        except KeyboardInterrupt:
            pass
        return 0
    return _perform_scan(
        ctx, path, fmt, baseline_name, save_as_baseline, mode="manual",
        description=description, no_line=no_line, masker=masker,
        constraints=constraints,
    )


# ---------------------------------------------------------------------------
# baseline group
# ---------------------------------------------------------------------------

@cli.group()
def baseline() -> None:
    """Manage baselines (versioned snapshots)."""


@baseline.command("create")
@click.argument("name")
@click.option("--scan-root", "scan_root", required=True, type=click.Path(exists=True),
              help="File or directory to snapshot.")
@click.option("--format", "fmt", default="auto",
              help="Config format (auto/json/yaml/toml/ini or a registered parser plugin, v0.5.0).")
@click.option("--description", default="")
@click.pass_context
def baseline_create(ctx: click.Context, name: str, scan_root: str, fmt: str,
                    description: str) -> int:
    """Create a new baseline version from a file/directory snapshot."""
    store = _open_store(ctx)
    snapshot, line_maps = _SCANNER.scan_path_with_lines(scan_root, fmt)
    bl = store.create_baseline(
        name=name,
        description=description,
        scan_root=os.path.abspath(scan_root),
        format=fmt,
        data=snapshot,
        line_maps=line_maps,
    )
    store.close()
    click.echo("baseline %r version %d created" % (bl.name, bl.version))
    return 0


@baseline.command("list")
@click.pass_context
def baseline_list(ctx: click.Context) -> int:
    """List baselines (latest version of each name)."""
    store = _open_store(ctx)
    rows = store.list_baselines()
    store.close()
    if not rows:
        click.echo("no baselines")
        return 0
    for bl in rows:
        click.echo(
            "%s v%d  %s  root=%s  %s"
            % (bl.name, bl.version, bl.created_at, bl.scan_root, bl.description)
        )
    return 0


@baseline.command("show")
@click.argument("name")
@click.option("--version", "version", type=int, default=None)
@click.pass_context
def baseline_show(ctx: click.Context, name: str, version: Optional[int]) -> int:
    """Show baseline metadata and data."""
    store = _open_store(ctx)
    bl = store.show_baseline(name, version)
    store.close()
    click.echo("name: %s" % bl.name)
    click.echo("version: %d" % bl.version)
    click.echo("description: %s" % bl.description)
    click.echo("created_at: %s" % bl.created_at)
    click.echo("scan_root: %s" % bl.scan_root)
    click.echo("format: %s" % bl.format)
    click.echo("files: %s" % ", ".join(sorted(bl.data.keys())))
    return 0


@baseline.command("rollback")
@click.argument("name")
@click.pass_context
def baseline_rollback(ctx: click.Context, name: str) -> int:
    """Delete the latest version; the previous version becomes current."""
    store = _open_store(ctx)
    bl = store.rollback_baseline(name)
    store.close()
    click.echo("rolled back %r to version %d" % (bl.name, bl.version))
    return 0


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------

def _run_compare(
    ctx: click.Context,
    environments: List[str],
    no_line: bool = False,
    json_output: bool = False,
    severity_filter: Optional[str] = None,
) -> int:
    """Shared compare logic used by ``compare`` and ``diff --compare``."""
    store = _open_store(ctx)
    engine = CompareEngine(store)
    env_map = engine.load_environments(_daemon_home())
    severity_rules = _load_severity_rules()
    # compare is a display exit: mask sensitive values (masking.yaml + defaults)
    # so password-like keys never leak plaintext in terminal/JSON output.
    masker = _build_masker()
    try:
        reports = engine.compare(
            list(environments),
            env_map=env_map,
            severity_rules=severity_rules,
            masker=masker,
        )
    except ValueError as exc:
        store.close()
        raise ValueError(str(exc)) from exc
    store.close()

    any_drift = any(rep.summary.total > 0 for rep in reports)

    if json_output:
        payload = {
            "code": 0,
            "data": [rep.to_dict() for rep in reports],
            "message": "ok",
        }
        _echo_json(payload)
        return 1 if any_drift else 0

    for rep in reports:
        version_a = rep.env1_version if rep.env1_version is not None else "?"
        version_b = rep.env2_version if rep.env2_version is not None else "?"
        click.echo(
            "compare %s -> %s (v%s vs v%s)"
            % (rep.baseline_a, rep.baseline_b, version_a, version_b)
        )
        items = rep.items
        if severity_filter:
            min_rank = Severity(severity_filter).rank
            items = [it for it in items if it.severity.rank >= min_rank]
        if not items:
            click.echo("  no differences")
            continue
        for it in items:
            sev = it.severity.value
            where = it.key_path if it.key_path else "(file)"
            location = it.file
            if not no_line and it.line is not None:
                location = "%s:%d" % (it.file, it.line)
            click.echo(
                "  [%s] %s (%s): %s %s -> %s"
                % (
                    sev,
                    where,
                    location,
                    it.change_type.value,
                    json.dumps(it.old_value, ensure_ascii=False)
                    if it.old_value is not None
                    else "null",
                    json.dumps(it.new_value, ensure_ascii=False)
                    if it.new_value is not None
                    else "null",
                )
            )
        s = rep.summary
        click.echo(
            "  Summary: added=%d removed=%d modified=%d type_changed=%d "
            "ignored=%d total=%d max=%s"
            % (
                s.added,
                s.removed,
                s.modified,
                s.type_changed,
                s.ignored,
                s.total,
                s.max_severity.value,
            )
        )
    return 1 if any_drift else 0


@cli.command()
@click.argument("path", type=click.Path(exists=True), required=False)
@click.option("--baseline", "baseline_name", default=None,
              help="Baseline name (required unless --compare).")
@click.option("--format", "fmt", default="auto",
              help="Config format (auto/json/yaml/toml/ini or a registered parser plugin, v0.5.0).")
@click.option("--color/--no-color", default=True, help="Colored terminal output.")
@click.option("--compare", "compare_mode", is_flag=True,
              help="Compare two baselines by environment name (--env1/--env2).")
@click.option("--env1", default=None, help="Reference environment name (with --compare).")
@click.option("--env2", default=None, help="Compared environment name (with --compare).")
@click.option(
    "--sensitive-keys",
    "sensitive_keys",
    multiple=True,
    help="Extra sensitive key stems to mask (append; default list is always active).",
)
@click.option("--no-line", "no_line", is_flag=True, help="Hide line numbers in output.")
@click.option("--builtin/--no-builtin", "builtin", default=True,
              help="Enable the built-in constraint library (v0.6.0; default: on).")
@click.option("--constraints", "constraint_files", multiple=True,
              type=click.Path(exists=True, dir_okay=False),
              help="Extra constraints.yaml file (repeatable; v0.6.0).")
@click.pass_context
def diff(ctx: click.Context, path: Optional[str], baseline_name: Optional[str],
         fmt: str, color: bool, compare_mode: bool, env1: Optional[str],
         env2: Optional[str], sensitive_keys: tuple, no_line: bool,
         builtin: bool, constraint_files: tuple) -> int:
    """Diff a file/directory against a baseline and print the drift report.

    ``--compare`` is a v0.4.0 alias for ``cfgdrift compare ENV1 ENV2``.
    """
    ctx.obj["color"] = color
    if compare_mode:
        if not env1 or not env2:
            raise ValueError("--compare requires --env1 and --env2")
        return _run_compare(ctx, [env1, env2], no_line=no_line)
    if not baseline_name:
        raise ValueError("--baseline is required (or use --compare)")
    if not path:
        raise ValueError("path is required (unless --compare)")
    masker = _build_masker(list(sensitive_keys))
    constraints = _load_constraints(list(constraint_files), builtin)
    return _perform_scan(
        ctx, path, fmt, baseline_name, None, mode="manual",
        no_line=no_line, masker=masker, constraints=constraints,
    )


# ---------------------------------------------------------------------------
# compare (v0.4.0)
# ---------------------------------------------------------------------------

@cli.command("compare")
@click.argument("environments", nargs=-1, required=True)
@click.option("--severity", "severity_filter", default=None,
              type=click.Choice(["CRITICAL", "WARN", "INFO", "NONE"]),
              help="Only show items at or above this severity.")
@click.option("--json", "json_output", is_flag=True, help="Output JSON.")
@click.option("--no-line", "no_line", is_flag=True, help="Hide line numbers in output.")
@click.option("-v", "verbose", is_flag=True, help="Verbose output (accepted for parity).")
@click.pass_context
def compare(ctx: click.Context, environments: tuple, severity_filter: Optional[str],
            json_output: bool, no_line: bool, verbose: bool) -> int:
    """Compare multiple environments' baselines against the first one.

    ``ENV1`` is the reference; every other environment is diffed against it.
    Environment names resolve to baselines through environments.yaml (when
    present); absent mappings use the environment name as the baseline name.
    Exit codes: 0 = no differences, 1 = differences, 2 = error.
    """
    return _run_compare(
        ctx,
        list(environments),
        no_line=no_line,
        json_output=json_output,
        severity_filter=severity_filter,
    )


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--scan-id", "scan_id", type=int, default=None,
              help="Scan id to render (default: latest scan).")
@click.option("--json", "json_path", type=click.Path(), default=None,
              help="Write the JSON report to a file.")
@click.option("--html", "html_path", type=click.Path(), default=None,
              help="Write a single-file offline HTML report to a file (v0.5.0).")
@click.option("--color/--no-color", default=True, help="Colored terminal output.")
@click.option("--no-line", "no_line", is_flag=True, help="Hide line numbers in output.")
@click.pass_context
def report(ctx: click.Context, scan_id: Optional[int], json_path: Optional[str],
           html_path: Optional[str], color: bool, no_line: bool) -> int:
    """Render a stored scan report (terminal, JSON or standalone HTML)."""
    if json_path and html_path:
        raise ValueError("--json and --html are mutually exclusive")
    store = _open_store(ctx)
    if scan_id is None:
        scans = store.list_scans(limit=1)
        if not scans:
            store.close()
            raise ValueError("no scans recorded yet")
        scan_id = scans[0]["scan_id"]
    payload = store.get_scan(scan_id)
    store.close()

    if payload.get("code") != 0:
        raise ValueError(payload.get("message", "scan report is invalid"))

    data = payload["data"]

    if html_path:
        # D6: same data source as the Web export — get_scan payload masked
        # at the display exit, then rendered by the shared HtmlReporter.
        masker = _build_masker()
        masker.mask_payload(payload)
        from .core.htmlreport import HtmlReporter

        html = HtmlReporter.render_html(data, title="cfgdrift report #%s" % scan_id)
        with open(html_path, "w", encoding="utf-8") as fh:
            fh.write(html)
        click.echo("report written to %s" % html_path)
        return 0

    summary_data = data.get("summary", {})
    summary = ScanSummary(
        added=int(summary_data.get("added", 0)),
        removed=int(summary_data.get("removed", 0)),
        modified=int(summary_data.get("modified", 0)),
        type_changed=int(summary_data.get("type_changed", 0)),
        ignored=int(summary_data.get("ignored", 0)),
    )
    summary.max_severity = summary_data.get("max_severity", "NONE")

    # Rebuild a lightweight Report for terminal rendering.
    items = [_item_from_dict(i) for i in data.get("items", [])]
    rep = Report(
        scan_id=scan_id,
        baseline=None,
        created_at=data.get("created_at", ""),
        mode=data.get("mode", "manual"),
        summary=summary,
        items=items,
    )

    if json_path:
        masker = _build_masker()
        masker.mask_payload(payload)
        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        click.echo("report written to %s" % json_path)
        return 0
    masker = _build_masker()
    click.echo(_REPORTER.render_terminal(rep, color=color, masker=masker,
                                         show_line=not no_line))
    return 1 if summary.total > 0 else 0


def _item_from_dict(d: Dict[str, Any]):
    from .core.model import ChangeType, DriftItem, Severity

    return DriftItem(
        key_path=d.get("key_path", ""),
        change_type=ChangeType(d.get("change_type", "modified")),
        severity=Severity(d.get("severity", "WARN")),
        file=d.get("file", ""),
        old_value=d.get("old_value"),
        new_value=d.get("new_value"),
        old_type=d.get("old_type"),
        new_type=d.get("new_type"),
        rule_id=d.get("rule_id"),
        line=d.get("line"),
        masked=bool(d.get("masked", False)),
        constraint_violations=list(d.get("constraint_violations", []) or []),
    )


# ---------------------------------------------------------------------------
# ignore group
# ---------------------------------------------------------------------------

@cli.group()
def ignore() -> None:
    """Manage ignore rules."""


@ignore.command("add")
@click.argument("name")
@click.argument("key_pattern")
@click.option("--match-type", "match_type", default="path_exact",
              type=click.Choice(["path_exact", "path_prefix", "regex"]))
@click.option("--file-pattern", "file_pattern", default=None,
              help="Regex matched against the file relpath (optional).")
@click.option("--change-type", "change_type", default=None,
              type=click.Choice(["added", "removed", "modified", "type_changed"]))
@click.option("--baseline", "baseline_name", default=None,
              help="Scope the rule to a baseline (default: global).")
@click.pass_context
def ignore_add(ctx: click.Context, name: str, key_pattern: str,
               match_type: str, file_pattern: Optional[str],
               change_type: Optional[str], baseline_name: Optional[str]) -> int:
    """Add an ignore rule."""
    store = _open_store(ctx)
    baseline_id = None
    if baseline_name:
        baseline_id = store.get_baseline(baseline_name).id
    rule = make_rule(
        name=name,
        key_pattern=key_pattern,
        match_type=match_type,
        baseline_id=baseline_id,
        file_pattern=file_pattern,
        change_type=change_type,
    )
    rule_id = store.add_rule(rule)
    store.close()
    click.echo("ignore rule #%d added" % rule_id)
    return 0


@ignore.command("list")
@click.option("--baseline", "baseline_name", default=None,
              help="Show global rules plus rules scoped to a baseline.")
@click.pass_context
def ignore_list(ctx: click.Context, baseline_name: Optional[str]) -> int:
    """List ignore rules."""
    store = _open_store(ctx)
    baseline_id = None
    if baseline_name:
        baseline_id = store.get_baseline(baseline_name).id
    rules = store.list_rules(baseline_id)
    store.close()
    if not rules:
        click.echo("no ignore rules")
        return 0
    for r in rules:
        scope = "global" if r.baseline_id is None else "baseline#%d" % r.baseline_id
        click.echo(
            "#%d [%s] %s key=%s type=%s file=%s change=%s"
            % (
                r.id,
                scope,
                r.name,
                r.key_pattern,
                r.match_type,
                r.file_pattern or "-",
                r.change_type or "-",
            )
        )
    return 0


@ignore.command("remove")
@click.argument("rule_id", type=int)
@click.pass_context
def ignore_remove(ctx: click.Context, rule_id: int) -> int:
    """Remove an ignore rule by id."""
    store = _open_store(ctx)
    store.delete_rule(rule_id)
    store.close()
    click.echo("ignore rule #%d removed" % rule_id)
    return 0


# ---------------------------------------------------------------------------
# severity group (v0.4.0)
# ---------------------------------------------------------------------------

@cli.group()
def severity() -> None:
    """Manage custom severity override rules (severity.yaml)."""


@severity.command("add")
@click.option("--name", required=True, help="Unique rule name.")
@click.option("--severity", "sev", required=True,
              type=click.Choice(["CRITICAL", "WARN", "INFO", "NONE"]))
@click.option("--change-type", "change_type", default=None,
              type=click.Choice(["added", "removed", "modified", "type_changed"]))
@click.option("--key-pattern", "key_pattern", default=None,
              help="Regex matched against the item key_path (optional).")
@click.option("--value-pattern", "value_pattern", default=None,
              help="Regex matched against old/new values (optional).")
@click.option("--file-pattern", "file_pattern", default=None,
              help="Regex matched against the file relpath (optional).")
@click.option("--disable", "disable", is_flag=True,
              help="Create the rule disabled (default: enabled).")
@click.pass_context
def severity_add(ctx: click.Context, name: str, sev: str,
                 change_type: Optional[str], key_pattern: Optional[str],
                 value_pattern: Optional[str], file_pattern: Optional[str],
                 disable: bool) -> int:
    """Add a severity override rule (first-match-wins, file order)."""
    path = severity_config_path(_daemon_home())
    rule = make_severity_rule(
        name=name,
        severity=sev,
        change_type=change_type,
        key_pattern=key_pattern,
        value_pattern=value_pattern,
        file_pattern=file_pattern,
        enabled=not disable,
    )
    SeverityConfig.add_rule(path, rule)
    click.echo(
        "severity rule %r added (severity=%s%s)" % (
            name, sev, "" if not disable else ", disabled")
    )
    return 0


@severity.command("list")
@click.pass_context
def severity_list(ctx: click.Context) -> int:
    """List severity override rules (source=severity.yaml)."""
    path = severity_config_path(_daemon_home())
    rules = SeverityConfig.list_rules(path)
    if not rules:
        click.echo("no severity rules")
        return 0
    for r in rules:
        click.echo(
            "# %s severity=%s enabled=%s change=%s key=%s value=%s file=%s "
            "source=severity.yaml"
            % (
                r.name,
                r.severity.value,
                "yes" if r.enabled else "no",
                r.change_type or "-",
                r.key_pattern or "-",
                r.value_pattern or "-",
                r.file_pattern or "-",
            )
        )
    return 0


@severity.command("remove")
@click.argument("name")
@click.pass_context
def severity_remove(ctx: click.Context, name: str) -> int:
    """Remove a severity rule by name."""
    path = severity_config_path(_daemon_home())
    SeverityConfig.remove_rule(path, name)
    click.echo("severity rule %r removed" % name)
    return 0


@severity.command("enable")
@click.argument("name")
@click.pass_context
def severity_enable(ctx: click.Context, name: str) -> int:
    """Enable a severity rule by name."""
    path = severity_config_path(_daemon_home())
    SeverityConfig.set_enabled(path, name, True)
    click.echo("severity rule %r enabled" % name)
    return 0


@severity.command("disable")
@click.argument("name")
@click.pass_context
def severity_disable(ctx: click.Context, name: str) -> int:
    """Disable a severity rule by name."""
    path = severity_config_path(_daemon_home())
    SeverityConfig.set_enabled(path, name, False)
    click.echo("severity rule %r disabled" % name)
    return 0


# ---------------------------------------------------------------------------
# constraint group (v0.6.0)
# ---------------------------------------------------------------------------

@cli.group()
def constraint() -> None:
    """Manage consistency constraints (constraints.yaml + built-in library)."""


def _constraint_source_options(source: str, show_all: bool) -> List[Constraint]:
    """Return the constraints to list for ``--source builtin|user|all``.

    ``all`` (the default view) shows the *effective* set: built-in library
    merged with user rules, later (user) entries overriding same-id built-ins
    (D8).  ``builtin`` lists only the built-in library; ``user`` only the
    constraints.yaml entries.
    """
    if source == "builtin":
        from .core.constraints import BUILTIN_CONSTRAINTS

        return list(BUILTIN_CONSTRAINTS)
    if source == "user":
        return ConstraintConfig.list_rules(constraints_config_path(_daemon_home()))
    return _load_constraints()


@constraint.command("add")
@click.option("--rule", "rule_json", required=True,
              help="Constraint as a JSON string (id/type/message/...; v0.6.0).")
@click.option("--disable", "disable", is_flag=True,
              help="Create the constraint disabled (default: enabled).")
@click.pass_context
def constraint_add(ctx: click.Context, rule_json: str, disable: bool) -> int:
    """Add a user constraint to <home>/constraints.yaml."""
    try:
        data = json.loads(rule_json)
    except ValueError as exc:
        raise ValueError("invalid --rule JSON: %s" % exc) from exc
    if not isinstance(data, dict):
        raise ValueError("--rule must be a JSON object")
    constraint_obj = Constraint.from_dict(data, source="user")
    if disable:
        constraint_obj.enabled = False
    path = constraints_config_path(_daemon_home())
    ConstraintConfig.add_rule(path, constraint_obj)
    click.echo(
        "constraint %r added (type=%s severity=%s%s)"
        % (
            constraint_obj.id,
            constraint_obj.type,
            constraint_obj.severity.value,
            "" if not disable else ", disabled",
        )
    )
    return 0


@constraint.command("list")
@click.option("--source", "source", default="all",
              type=click.Choice(["builtin", "user", "all"]),
              help="Filter by source (default: all = effective view).")
@click.option("--all", "show_all", is_flag=True,
              help="Explicitly show every rule (disabled ones are always "
                   "listed with their enabled flag).")
@click.pass_context
def constraint_list(ctx: click.Context, source: str, show_all: bool) -> int:
    """List consistency constraints (id / type / severity / enabled / source)."""
    rules = _constraint_source_options(source, show_all)
    if not rules:
        click.echo("no constraints")
        return 0
    for r in rules:
        click.echo(
            "# %s type=%s severity=%s enabled=%s source=%s"
            % (
                r.id,
                r.type,
                r.severity.value,
                "yes" if r.enabled else "no",
                r.source,
            )
        )
    return 0


@constraint.command("remove")
@click.argument("constraint_id")
@click.pass_context
def constraint_remove(ctx: click.Context, constraint_id: str) -> int:
    """Remove a user constraint by id."""
    path = constraints_config_path(_daemon_home())
    ConstraintConfig.remove_rule(path, constraint_id)
    click.echo("constraint %r removed" % constraint_id)
    return 0


@constraint.command("enable")
@click.argument("constraint_id")
@click.pass_context
def constraint_enable(ctx: click.Context, constraint_id: str) -> int:
    """Enable a user constraint by id."""
    path = constraints_config_path(_daemon_home())
    ConstraintConfig.set_enabled(path, constraint_id, True)
    click.echo("constraint %r enabled" % constraint_id)
    return 0


@constraint.command("disable")
@click.argument("constraint_id")
@click.pass_context
def constraint_disable(ctx: click.Context, constraint_id: str) -> int:
    """Disable a user constraint by id."""
    path = constraints_config_path(_daemon_home())
    ConstraintConfig.set_enabled(path, constraint_id, False)
    click.echo("constraint %r disabled" % constraint_id)
    return 0


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1).")
@click.option("--port", default=8080, type=int, help="Bind port (default: 8080).")
@click.pass_context
def serve(ctx: click.Context, host: str, port: int) -> int:
    """Start the local web dashboard (requires the [web] extra)."""
    try:
        import uvicorn  # noqa: F401
        from .web.app import create_app  # noqa: F401
    except ImportError as exc:
        click.echo(
            "error: web extra not installed; run: pip install 'cfgdrift[web]' "
            "(%s)" % exc,
            err=True,
        )
        return 2
    store = _open_store(ctx)
    app = create_app(store, home=_daemon_home())
    click.echo("serving dashboard at http://%s:%d (Ctrl+C to stop)" % (host, port))
    uvicorn.run(app, host=host, port=port, log_level="warning")
    store.close()
    return 0


# ---------------------------------------------------------------------------
# daemon group (v0.3.0)
# ---------------------------------------------------------------------------

@cli.group()
def daemon() -> None:
    """Manage the background drift-monitoring daemon."""


@daemon.command("start")
@click.option("--target", "--path", "targets", multiple=True, required=True,
              type=click.Path(exists=True),
              help="Path to monitor (repeatable; file or directory).")
@click.option("--baseline", "baseline_name", required=True,
              help="Baseline name to diff against.")
@click.option("--format", "fmt", default="auto",
              help="Config format (auto/json/yaml/toml/ini or a registered parser plugin, v0.5.0).")
@click.option("--interval", default=300, type=int,
              help="Scan interval in seconds (default: 300).")
@click.option("--log-file", default=None, help="Override daemon log file path.")
@click.option("--log-level", default="INFO",
              type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"]))
@click.option("--foreground", is_flag=True,
              help="Run in the foreground (development/CI; Ctrl+C stops).")
@click.option("--builtin/--no-builtin", "builtin", default=True,
              help="Enable the built-in constraint library (v0.6.0; default: on).")
@click.option("--constraints", "constraint_files", multiple=True,
              type=click.Path(exists=True, dir_okay=False),
              help="Extra constraints.yaml file (repeatable; v0.6.0).")
@click.pass_context
def daemon_start(
    ctx: click.Context,
    targets: tuple,
    baseline_name: str,
    fmt: str,
    interval: int,
    log_file: Optional[str],
    log_level: str,
    foreground: bool,
    builtin: bool,
    constraint_files: tuple,
) -> int:
    """Start the daemon (background by default)."""
    if interval <= 0:
        raise ValueError("--interval must be a positive integer")
    # v0.5.0: fail fast on an invalid --format before forking/spawning.
    validate_format(fmt)

    home = _daemon_home()
    store_path = ctx.obj.get("store") or os.path.join(home, "cfgdrift.db")
    manager = DaemonManager(home)

    # Pre-flight: baseline must exist and the daemon must not be running.
    store = Store(store_path)
    try:
        store.get_baseline(baseline_name)
    finally:
        store.close()
    try:
        pid = manager._read_pid()
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    if pid is not None and manager._process_exists(pid):
        raise ValueError("daemon already running (pid=%d)" % pid)
    if pid is not None:
        manager._clear_pid()
        manager._clear_stop_file()
        manager._clear_info()

    if foreground:
        argv = [
            "--home", home,
            "--store", store_path,
            "--baseline", baseline_name,
            "--format", fmt,
            "--interval", str(interval),
            "--pid-file", manager.pid_file,
            "--stop-file", manager.stop_file,
            "--info-file", manager.info_file,
            "--log-file", log_file or manager.log_file,
            "--log-level", log_level,
            "--alerts-config", AlertConfig.default_path(home),
            "--alert-state", os.path.join(home, "alert_state.json"),
            "--foreground",
        ]
        if not builtin:
            argv += ["--no-builtin"]
        for extra in constraint_files:
            argv += ["--constraints", extra]
        for target in targets:
            argv += ["--path", target]
        try:
            return worker_main(argv)
        except KeyboardInterrupt:
            click.echo("daemon stopped (foreground)")
            return 0

    opts = {
        "targets": list(targets),
        "baseline": baseline_name,
        "fmt": fmt,
        "interval": interval,
        "store": store_path,
        "log_level": log_level,
        "log_file": log_file or manager.log_file,
        "alerts_config": AlertConfig.default_path(home),
        "alert_state": os.path.join(home, "alert_state.json"),
        "builtin": builtin,
        "constraint_paths": list(constraint_files),
    }
    return manager.start(opts)


@daemon.command("stop")
@click.option("--timeout", default=30, type=int,
              help="Seconds to wait for graceful stop before force-kill.")
@click.pass_context
def daemon_stop(ctx: click.Context, timeout: int) -> int:
    """Stop the daemon (idempotent; force-kills after --timeout)."""
    manager = DaemonManager(_daemon_home())
    return manager.stop(timeout=timeout)


@daemon.command("status")
@click.pass_context
def daemon_status(ctx: click.Context) -> int:
    """Show daemon status (0 running / 1 stopped / 2 error)."""
    manager = DaemonManager(_daemon_home())
    code = manager.status()
    if code == 0:
        info = manager.read_info()
        if info:
            click.echo("pid: %s" % info.get("pid"))
            click.echo("started_at: %s" % info.get("started_at"))
            click.echo("interval: %s" % info.get("interval"))
            click.echo("targets: %s" % ", ".join(info.get("targets", [])))
            click.echo("baseline: %s" % info.get("baseline"))
        # Best-effort last scan info (stop/status must not require the Store).
        try:
            store = _open_store(ctx)
            try:
                scans = store.list_scans(limit=1)
            finally:
                store.close()
            if scans:
                s = scans[0]
                click.echo("last_scan: %s" % s["created_at"])
                click.echo(
                    "last_result: mode=%s total=%d max=%s"
                    % (s["mode"], s["summary"]["total"], s["summary"]["max_severity"])
                )
            else:
                click.echo("last_scan: none")
        except Exception:
            click.echo("last_scan: unavailable")
    return code


@daemon.command("enable-autostart")
@click.option("--target", "--path", "targets", multiple=True, required=True,
              type=click.Path(exists=True),
              help="Path to monitor (repeatable; file or directory).")
@click.option("--baseline", "baseline_name", required=True,
              help="Baseline name to diff against.")
@click.option("--format", "fmt", default="auto",
              help="Config format (auto/json/yaml/toml/ini or a registered parser plugin).")
@click.option("--interval", default=300, type=int,
              help="Scan interval in seconds (autostart requires >= 60; default: 300).")
@click.option("--user/--system", "scope_user", default=True,
              help="Install a user-level unit (default) or a system-wide one (needs root).")
@click.option("--dry-run", is_flag=True,
              help="Print the unit/command + autostart.json without writing anything.")
@click.option("--force", is_flag=True,
              help="Overwrite an existing autostart configured with different parameters.")
@click.pass_context
def daemon_enable_autostart(
    ctx: click.Context,
    targets: tuple,
    baseline_name: str,
    fmt: str,
    interval: int,
    scope_user: bool,
    dry_run: bool,
    force: bool,
) -> int:
    """Install the daemon autostart (systemd / launchd / schtasks).

    The single source of truth is <home>/autostart.json; the platform
    artifact is written/removed together with it (double-write / double-clear).
    """
    if interval <= 0:
        raise ValueError("--interval must be a positive integer")
    validate_format(fmt)
    home = _daemon_home()
    store_path = ctx.obj.get("store") or os.path.join(home, "cfgdrift.db")
    manager = AutostartManager(home, store_path)
    opts = {
        "targets": list(targets),
        "baseline": baseline_name,
        "fmt": fmt,
        "interval": interval,
        "store": store_path,
        "log_file": os.path.join(home, "logs", "daemon.log"),
        "log_level": "INFO",
        "scope": "user" if scope_user else "system",
    }
    return manager.enable(opts, dry_run=dry_run)


@daemon.command("disable-autostart")
@click.option("--dry-run", is_flag=True,
              help="Print the removal commands without touching disk.")
@click.pass_context
def daemon_disable_autostart(ctx: click.Context, dry_run: bool) -> int:
    """Remove the daemon autostart artifact + autostart.json (idempotent)."""
    manager = AutostartManager(_daemon_home())
    return manager.disable(dry_run=dry_run)


@daemon.command("autostart-status")
@click.pass_context
def daemon_autostart_status(ctx: click.Context) -> int:
    """Show autostart status (0 enabled / 1 disabled / 2 error)."""
    manager = AutostartManager(_daemon_home())
    return manager.status()


# ---------------------------------------------------------------------------
# alert group (v0.3.0)
# ---------------------------------------------------------------------------

@cli.group()
def alert() -> None:
    """Manage alert rules (webhook / email / script channels)."""


@alert.command("add")
@click.option("--name", required=True, help="Unique rule name.")
@click.option("--type", "rule_type", required=True,
              type=click.Choice(["webhook", "email", "script"]))
@click.option("--severity", default="WARN",
              type=click.Choice(["CRITICAL", "WARN", "INFO", "NONE"]),
              help="Trigger threshold (default: WARN).")
@click.option("--baseline", "baseline_name", default=None,
              help="Scope the rule to one baseline (default: all).")
# webhook
@click.option("--url", default=None, help="Webhook URL.")
@click.option("--header", "headers", multiple=True,
              help="Webhook header KEY=VALUE (repeatable; values support {env:VAR}).")
# email
@click.option("--smtp-host", default=None)
@click.option("--smtp-port", type=int, default=None)
@click.option("--from", "from_addr", default=None, help="SMTP sender address.")
@click.option("--to", "to_addrs", multiple=True, help="SMTP recipient (repeatable).")
@click.option("--smtp-user", default=None, help="SMTP login user (default: --from).")
@click.option("--smtp-password-env", default=None,
              help="Name of the env var holding the SMTP password (never stored).")
@click.option("--subject", default=None,
              help="Subject template (supports {severity}/{baseline}/{env:VAR}).")
@click.option("--use-tls/--no-use-tls", default=True, help="STARTTLS (default: on).")
@click.option("--use-ssl", is_flag=True, help="Use implicit SSL (SMTP_SSL).")
# script
@click.option("--command", default=None, help="Script command path.")
@click.option("--arg", "script_args", multiple=True,
              help="Script argument (repeatable; supports {baseline}/{env:VAR}).")
@click.option("--timeout", type=float, default=None,
              help="Channel timeout in seconds.")
# v0.5.0: rule-level retry (optional; falls back to the global default).
@click.option("--retry-count", "retry_count", type=int, default=None,
              help="Total send attempts (>= 1; default: global 3).")
@click.option("--retry-delay", "retry_delays", multiple=True, default=None,
              help="Seconds to wait between attempts (repeatable or comma-separated; "
                   "attempts = len(delays)+1 when only delays are given).")
@click.pass_context
def alert_add(
    ctx: click.Context,
    name: str,
    rule_type: str,
    severity: str,
    baseline_name: Optional[str],
    url: Optional[str],
    headers: tuple,
    smtp_host: Optional[str],
    smtp_port: Optional[int],
    from_addr: Optional[str],
    to_addrs: tuple,
    smtp_user: Optional[str],
    smtp_password_env: Optional[str],
    subject: Optional[str],
    use_tls: bool,
    use_ssl: bool,
    command: Optional[str],
    script_args: tuple,
    timeout: Optional[float],
    retry_count: Optional[int],
    retry_delays: tuple,
) -> int:
    """Add an alert rule to <home>/alerts.yaml."""
    home = _daemon_home()
    path = AlertConfig.default_path(home)

    config: Dict[str, Any] = {}
    if rule_type == "webhook":
        if not url:
            raise ValueError("webhook alert requires --url")
        config = {"url": url, "timeout": timeout if timeout is not None else 10}
        if headers:
            parsed: Dict[str, str] = {}
            for kv in headers:
                if "=" not in kv:
                    raise ValueError("--header must be KEY=VALUE, got %r" % kv)
                key, value = kv.split("=", 1)
                parsed[key.strip()] = value
            config["headers"] = parsed
    elif rule_type == "email":
        if not smtp_host:
            raise ValueError("email alert requires --smtp-host")
        if smtp_port is None:
            raise ValueError("email alert requires --smtp-port")
        if not from_addr:
            raise ValueError("email alert requires --from")
        if not to_addrs:
            raise ValueError("email alert requires at least one --to")
        config = {
            "smtp_host": smtp_host,
            "smtp_port": int(smtp_port),
            "smtp_from": from_addr,
            "smtp_to": list(to_addrs),
            "use_tls": use_tls,
            "use_ssl": use_ssl,
            "timeout": timeout if timeout is not None else 15,
        }
        if smtp_user:
            config["smtp_user"] = smtp_user
        if smtp_password_env:
            config["smtp_password_env"] = smtp_password_env
        if subject:
            config["subject_template"] = subject
    elif rule_type == "script":
        if not command:
            raise ValueError("script alert requires --command")
        config = {
            "command": command,
            "args": list(script_args),
            "timeout": timeout if timeout is not None else 30,
        }

    rule = AlertRule(
        name=name,
        type=rule_type,
        severity=Severity(severity),
        baseline=baseline_name,
        enabled=True,
        config=config,
        retry_count=retry_count,
        retry_delays=_flatten_retry_delays(retry_delays),
    )
    AlertConfig.add_rule(path, rule)
    click.echo("alert rule %r added (type=%s severity=%s)" % (name, rule_type, severity))
    return 0


def _flatten_retry_delays(values: tuple) -> Optional[List[float]]:
    """Flatten ``--retry-delay`` values (comma-separated and/or repeated).

    ``("1,5,30",)`` and ``("1", "5", "30")`` both produce ``[1.0, 5.0, 30.0]``.
    Returns ``None`` when no values were given.
    """
    out: List[float] = []
    for value in values:
        for part in str(value).split(","):
            part = part.strip()
            if part:
                out.append(float(part))
    return out or None


@alert.command("list")
@click.pass_context
def alert_list(ctx: click.Context) -> int:
    """List alert rules."""
    path = AlertConfig.default_path(_daemon_home())
    rules = AlertConfig.list_rules(path)
    if not rules:
        click.echo("no alert rules")
        return 0
    for r in rules:
        scope = r.baseline if r.baseline else "all"
        # v0.5.0: show the effective retry strategy (rule > global default).
        retry_text = "default"
        if r.retry_count is not None or r.retry_delays is not None:
            attempts, delays = r.effective_retry(3, (1, 5, 30))
            retry_text = "%d/%s" % (
                attempts,
                ",".join(str(float(d)) for d in delays),
            )
        click.echo(
            "# %s type=%s severity=%s baseline=%s enabled=%s retry=%s"
            % (
                r.name,
                r.type,
                r.severity.value,
                scope,
                "yes" if r.enabled else "no",
                retry_text,
            )
        )
    return 0


@alert.command("remove")
@click.argument("name")
@click.pass_context
def alert_remove(ctx: click.Context, name: str) -> int:
    """Remove an alert rule by name."""
    path = AlertConfig.default_path(_daemon_home())
    AlertConfig.remove_rule(path, name)
    click.echo("alert rule %r removed" % name)
    return 0


@alert.command("test")
@click.argument("name")
@click.pass_context
def alert_test(ctx: click.Context, name: str) -> int:
    """Send a connectivity test through a rule's channel (event=cfgdrift.test)."""
    home = _daemon_home()
    path = AlertConfig.default_path(home)
    rules = AlertConfig.list_rules(path)
    rule = next((r for r in rules if r.name == name), None)
    if rule is None:
        raise ValueError("alert rule %r not found" % name)
    state = AlertStateStore(os.path.join(home, "alert_state.json"))
    dispatcher = AlertDispatcher(rules, state)
    result = dispatcher.test_rule(rule)
    if result.sent:
        click.echo("alert test %r ok (attempts=%d)" % (name, result.attempts))
        return 0
    click.echo("alert test %r failed: %s" % (name, result.error), err=True)
    return 2


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    """Programmatic entry point returning an exit code (0/1/2)."""
    if os.environ.get("CFGDRIFT_DEBUG", "").strip() == "1":
        logging.basicConfig(level=logging.DEBUG)
        # The parser module already logged its backend choice at import time
        # (before basicConfig raised the level), so re-emit it here so
        # CFGDRIFT_DEBUG=1 actually shows the initial selection.
        from .core import parser as parser_mod

        parser_mod.logger.debug("parser backend: %s", parser_mod.PARSER_BACKEND)
    try:
        result = cli.main(args=argv, prog_name="cfgdrift", standalone_mode=False)
        if result is None:
            return 0
        return int(result)
    except click.ClickException as exc:
        click.echo("error: %s" % exc.format_message(), err=True)
        return 2
    except (ValueError, RuntimeError, OSError) as exc:
        click.echo("error: %s" % exc, err=True)
        return 2


if __name__ == "__main__":
    sys.exit(main())
