"""Consistency constraint engine (v0.6.0).

The engine sits on top of the semantic differ: after drift items are produced
for a snapshot, it evaluates a list of :class:`Constraint` against the *new*
configuration tree and attaches a :class:`ConstraintViolation` to every drift
item whose ``key_path`` participates in a violated constraint
(``involved_keys ∩ drift_keys ≠ ∅`` — per-file association, D5).  Attached
items are then severity-upgraded once using

    new_rank = min(3, max(item.severity.rank + 1,
                          max(violated constraint severity ranks)))

which reuses :class:`Severity.rank` (NONE=0 / INFO=1 / WARN=2 / CRITICAL=3)
without introducing a separate CONSTRAINT level (Q1).  Only drift-associated
breaks are reported — pre-existing violations in the baseline are not
surfaced (Q2, P0).

Skipping semantics (the basis of the zero-noise contract, D7):
- a target key that is missing from the new tree is skipped;
- a non-numeric value under a ``range`` constraint is skipped;
- an unsatisfied ``when`` condition is skipped.

Performance: ``check_one`` uses directed path lookup (:func:`_get_path`,
O(depth)) and never walks the whole tree, so a 10k-key snapshot × 20
constraints costs far less than 10 ms.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .model import (
    CONSTRAINT_TYPES,
    Constraint,
    ConstraintViolation,
    DriftItem,
    Severity,
)

__all__ = [
    "ConstraintEngine",
    "BUILTIN_CONSTRAINTS",
    "apply_constraints",
    "constraint_types",
    "violations_from_items",
]

constraint_types = CONSTRAINT_TYPES

_SEV_BY_RANK = [
    Severity.NONE,
    Severity.INFO,
    Severity.WARN,
    Severity.CRITICAL,
]

_MISSING = object()

_CORRELATION_OPS = (">=", ">", "<=", "<", "==", "!=")


# ---------------------------------------------------------------------------
# Directed path lookup
# ---------------------------------------------------------------------------

def _split_key_path(path: str) -> List[tuple]:
    """Split a dotted key path into ``("key", name)`` / ``("index", i)`` parts.

    Supports backslash escaping (``\\.`` / ``\\[`` / ``\\]`` / ``\\\\``) and
    list indices (``a[0].b``).  Best effort; the built-in library only uses
    plain dotted paths.
    """
    parts: List[tuple] = []
    buf: List[str] = []
    i = 0
    n = len(path)
    while i < n:
        ch = path[i]
        if ch == "\\" and i + 1 < n:
            buf.append(path[i + 1])
            i += 2
            continue
        if ch == ".":
            if buf:
                parts.append(("key", "".join(buf)))
                buf = []
            i += 1
            continue
        if ch == "[":
            if buf:
                parts.append(("key", "".join(buf)))
                buf = []
            end = path.find("]", i)
            if end == -1:
                parts.append(("index", path[i + 1:]))
                break
            parts.append(("index", path[i + 1:end]))
            i = end + 1
            continue
        buf.append(ch)
        i += 1
    if buf:
        parts.append(("key", "".join(buf)))
    return parts


def _get_path(tree: Any, key_path: str) -> Any:
    """Resolve a dotted key path in a semantic tree.

    Returns the ``_MISSING`` sentinel when the path does not exist (or a
    segment is not traversable).  The sentinel is never a legal config value.
    """
    if not isinstance(tree, dict):
        return _MISSING
    cur: Any = tree
    for kind, value in _split_key_path(key_path):
        if kind == "key":
            if not isinstance(cur, dict) or value not in cur:
                return _MISSING
            cur = cur[value]
        else:  # index
            if not isinstance(cur, list):
                return _MISSING
            try:
                idx = int(value)
            except (TypeError, ValueError):
                return _MISSING
            if idx < 0 or idx >= len(cur):
                return _MISSING
            cur = cur[idx]
    return cur


# ---------------------------------------------------------------------------
# Message rendering (D6: built-in messages contain no live values; user
# messages render {key}/{value}/{min}/{max} verbatim; unknown placeholders
# are left untouched).
# ---------------------------------------------------------------------------

def _render_message(constraint: Constraint, key: Optional[str] = None,
                    value: Any = None) -> str:
    msg = constraint.message
    replacements: Dict[str, str] = {}
    if key is not None:
        replacements["key"] = str(key)
    if value is not None:
        replacements["value"] = str(value)
    if constraint.min is not None:
        replacements["min"] = str(constraint.min)
    if constraint.max is not None:
        replacements["max"] = str(constraint.max)
    for name, text in replacements.items():
        msg = msg.replace("{%s}" % name, text)
    return msg


# ---------------------------------------------------------------------------
# Per-type validators
# ---------------------------------------------------------------------------

def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _compare(actual: Any, op: str, expected: Any) -> bool:
    """Compare ``actual`` against ``expected`` with a correlation op."""
    try:
        if _is_number(actual) and _is_number(expected):
            left: Any = actual
            right: Any = expected
        else:
            left = str(actual)
            right = str(expected)
        if op == ">=":
            return left >= right
        if op == ">":
            return left > right
        if op == "<=":
            return left <= right
        if op == "<":
            return left < right
        if op == "==":
            return left == right
        if op == "!=":
            return left != right
        return False
    except TypeError:
        return False


def _check_range(c: Constraint, tree: dict) -> List[ConstraintViolation]:
    key = c.keys[0]
    value = _get_path(tree, key)
    if value is _MISSING or not _is_number(value):
        return []
    if c.min is not None and value < c.min:
        return [
            ConstraintViolation(
                constraint_id=c.id,
                type=c.type,
                message=_render_message(c, key=key, value=value),
                involved_keys=[key],
            )
        ]
    if c.max is not None and value > c.max:
        return [
            ConstraintViolation(
                constraint_id=c.id,
                type=c.type,
                message=_render_message(c, key=key, value=value),
                involved_keys=[key],
            )
        ]
    return []


def _check_enum(c: Constraint, tree: dict) -> List[ConstraintViolation]:
    key = c.keys[0]
    value = _get_path(tree, key)
    if value is _MISSING:
        return []
    if value not in c.allowed:
        return [
            ConstraintViolation(
                constraint_id=c.id,
                type=c.type,
                message=_render_message(c, key=key, value=value),
                involved_keys=[key],
            )
        ]
    return []


def _check_conditional_required(c: Constraint, tree: dict) -> List[ConstraintViolation]:
    when_key = c.when["key"]
    when_value = c.when["value"]
    actual = _get_path(tree, when_key)
    if actual is _MISSING or actual != when_value:
        return []
    violations: List[ConstraintViolation] = []
    for req in c.then["require"]:
        if _get_path(tree, req) is _MISSING:
            violations.append(
                ConstraintViolation(
                    constraint_id=c.id,
                    type=c.type,
                    message=_render_message(c, key=req),
                    involved_keys=[when_key, req],
                )
            )
    return violations


def _check_correlation(c: Constraint, tree: dict) -> List[ConstraintViolation]:
    when_key = c.when["key"]
    when_value = c.when["value"]
    actual = _get_path(tree, when_key)
    if actual is _MISSING or actual != when_value:
        return []
    violations: List[ConstraintViolation] = []
    for cond in c.then:
        cond_key = cond["key"]
        cond_value = _get_path(tree, cond_key)
        if cond_value is _MISSING:
            continue  # missing target key -> skip (zero-noise)
        if not _compare(cond_value, cond["op"], cond["value"]):
            violations.append(
                ConstraintViolation(
                    constraint_id=c.id,
                    type=c.type,
                    message=_render_message(c, key=cond_key, value=cond_value),
                    involved_keys=[when_key, cond_key],
                )
            )
    return violations


def _check_mutual_exclusion(c: Constraint, tree: dict) -> List[ConstraintViolation]:
    present = [k for k in c.keys if _get_path(tree, k) is not _MISSING]
    if c.forbid is None:
        if len(present) >= 2:
            return [
                ConstraintViolation(
                    constraint_id=c.id,
                    type=c.type,
                    message=_render_message(c),
                    involved_keys=list(c.keys),
                )
            ]
        return []
    for pair in c.forbid:
        v1, v2 = pair
        k1, k2 = c.keys[0], c.keys[1]
        val1 = _get_path(tree, k1)
        val2 = _get_path(tree, k2)
        if val1 is _MISSING or val2 is _MISSING:
            continue
        if val1 == v1 and val2 == v2:
            return [
                ConstraintViolation(
                    constraint_id=c.id,
                    type=c.type,
                    message=_render_message(c),
                    involved_keys=list(c.keys),
                )
            ]
    return []


_VALIDATORS = {
    "range": _check_range,
    "enum": _check_enum,
    "conditional_required": _check_conditional_required,
    "correlation": _check_correlation,
    "mutual_exclusion": _check_mutual_exclusion,
}


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class ConstraintEngine:
    """Evaluates constraints against a snapshot and attaches violations."""

    @staticmethod
    def check_one(constraint: Constraint, tree: dict) -> List[ConstraintViolation]:
        """Run one constraint against one (single-file) semantic tree.

        Missing target keys and unsatisfied conditions are skipped (no
        violations).  Returns a possibly-empty list of violations.
        """
        validator = _VALIDATORS.get(constraint.type)
        if validator is None or not constraint.enabled:
            return []
        return validator(constraint, tree)

    @staticmethod
    def apply(new_snapshot: Optional[dict], items: List[DriftItem],
              constraints: Optional[List[Constraint]]) -> None:
        """Attach violations + upgrade severities in place (D5 / Q1).

        - group ``items`` by ``file``;
        - for each file, collect the drift ``key_path`` set;
        - evaluate every enabled constraint against that file's new tree;
        - a violation is *associated* when ``involved_keys ∩ drift_keys`` is
          non-empty, and is attached to every drift item whose ``key_path``
          is in ``involved_keys``;
        - after all attachments, upgrade each violated item exactly once.
        """
        if not constraints or not items:
            return
        enabled = [c for c in constraints if c.enabled]
        if not enabled:
            return
        constraints_by_id: Dict[str, Constraint] = {c.id: c for c in enabled}

        by_file: Dict[str, List[DriftItem]] = {}
        for item in items:
            by_file.setdefault(item.file or "", []).append(item)

        for file, file_items in by_file.items():
            tree = None
            if new_snapshot:
                tree = new_snapshot.get(file)
                if tree is None and file == "" and len(new_snapshot) == 1:
                    # Single-file diff with an empty relpath: the snapshot is
                    # {"": new} and the tree is the sole value.
                    tree = next(iter(new_snapshot.values()))
            if tree is None or not isinstance(tree, dict):
                continue
            drift_keys = {it.key_path for it in file_items if it.key_path}
            if not drift_keys:
                continue

            for constraint in enabled:
                for violation in ConstraintEngine.check_one(constraint, tree):
                    involved = set(violation.involved_keys)
                    if not (involved & drift_keys):
                        continue
                    for item in file_items:
                        if item.key_path in involved:
                            item.constraint_violations.append(
                                violation.to_dict()
                            )

        for item in items:
            if item.constraint_violations:
                ConstraintEngine._upgrade(item, constraints_by_id)

    @staticmethod
    def _upgrade(item: DriftItem, constraints_by_id: Dict[str, Constraint]) -> None:
        """Upgrade one item once (Q1): min(3, max(item.rank+1, max(c.rank)))."""
        max_constraint_rank = 0
        for violation in item.constraint_violations:
            constraint = constraints_by_id.get(violation.get("constraint_id"))
            if constraint is not None:
                max_constraint_rank = max(
                    max_constraint_rank, constraint.severity.rank
                )
        new_rank = min(3, max(item.severity.rank + 1, max_constraint_rank))
        item.severity = _SEV_BY_RANK[new_rank]

    @staticmethod
    def check_tree(constraints: Optional[List[Constraint]],
                   new_snapshot: Optional[dict]) -> List[dict]:
        """Evaluate every enabled constraint against the whole new snapshot.

        Returns **ALL** violations (not only drift-associated ones), each as
        ``{constraint_id, type, message, involved_keys, file, severity}``.
        ``severity`` is taken directly from the constraint itself (Q6).  The
        function is pure: it never touches the database.
        """
        if not constraints or not new_snapshot:
            return []
        enabled = [c for c in constraints if c.enabled]
        if not enabled:
            return []
        out: List[dict] = []
        for relpath in sorted(new_snapshot.keys()):
            tree = new_snapshot[relpath]
            if not isinstance(tree, dict):
                continue
            for constraint in enabled:
                for violation in ConstraintEngine.check_one(constraint, tree):
                    out.append(
                        {
                            "constraint_id": violation.constraint_id,
                            "type": violation.type,
                            "message": violation.message,
                            "involved_keys": list(violation.involved_keys),
                            "file": relpath,
                            "severity": constraint.severity.value,
                        }
                    )
        return out

    @staticmethod
    def baseline_violations(constraints: Optional[List[Constraint]],
                            new_snapshot: Optional[dict],
                            drift_items: Optional[List[DriftItem]]) -> List[dict]:
        """Pre-existing violations = check_tree − drift-associated violations.

        The signature used for de-duplication is
        ``(constraint_id, file, frozenset(involved_keys))``: a violation that
        is already attached to a drift item (i.e. its keys intersect the drift
        keys of that file) is *not* re-reported as baseline.  Severity is the
        constraint's own severity (Q6).  Pure function, zero DB access.
        """
        if not constraints or not new_snapshot:
            return []
        all_violations = ConstraintEngine.check_tree(constraints, new_snapshot)
        if not all_violations:
            return []
        drift_signatures: set = set()
        for item in drift_items or []:
            file = item.file or ""
            for violation in getattr(item, "constraint_violations", None) or []:
                involved = frozenset(violation.get("involved_keys") or [])
                drift_signatures.add(
                    (violation.get("constraint_id", ""), file, involved)
                )
        out: List[dict] = []
        seen: set = set()
        for violation in all_violations:
            involved = frozenset(violation["involved_keys"])
            signature = (violation["constraint_id"], violation["file"], involved)
            if signature in drift_signatures:
                continue
            if signature in seen:
                continue
            seen.add(signature)
            out.append(violation)
        return out


def violations_from_items(items: Optional[List[DriftItem]]) -> List[dict]:
    """Extract C-10 rows from drift items' attached constraint violations (D1).

    Each returned row is shaped for ``Store.add_constraint_violations``:
    ``{constraint_id, kind: 'drift', file, keys, severity, detail}``.  The
    severity is the item's (post-upgrade) severity — the value users see in
    the drift report.  This is the *only* place drift violations are turned
    into rows; the differ/engine never write to the database.
    """
    rows: List[dict] = []
    for item in items or []:
        for violation in getattr(item, "constraint_violations", None) or []:
            severity = item.severity
            rows.append(
                {
                    "constraint_id": violation.get("constraint_id", ""),
                    "kind": "drift",
                    "file": item.file or "",
                    "keys": list(violation.get("involved_keys") or []),
                    "severity": severity.value
                    if isinstance(severity, Severity)
                    else str(severity),
                    "detail": violation.get("message", ""),
                }
            )
    return rows


def apply_constraints(new_snapshot: Optional[dict], items: List[DriftItem],
                      constraints: Optional[List[Constraint]]) -> None:
    """Module-level convenience entry point (called by differ._finish)."""
    ConstraintEngine.apply(new_snapshot, items, constraints)


# ---------------------------------------------------------------------------
# Built-in constraint library (20 rules, §6.3 — four domains × five types)
# ---------------------------------------------------------------------------

def _bi(cid: str, ctype: str, message: str, severity: Severity,
        keys: Optional[List[str]] = None, cmin: Optional[float] = None,
        cmax: Optional[float] = None, allowed: Optional[List[Any]] = None,
        when: Optional[dict] = None, then: Optional[Any] = None,
        forbid: Optional[List[list]] = None) -> Constraint:
    """Build a built-in constraint (source=builtin, enabled by default)."""
    return Constraint(
        id=cid,
        type=ctype,
        message=message,
        severity=severity,
        enabled=True,
        source="builtin",
        keys=list(keys or []),
        min=cmin,
        max=cmax,
        allowed=allowed,
        when=when,
        then=then,
        forbid=forbid,
    )


BUILTIN_CONSTRAINTS: List[Constraint] = [
    # --- web / server (1-9) ---
    _bi("http_port_range", "range",
        "server.port 必须在 [1, 65535] 范围内", Severity.WARN,
        keys=["server.port"], cmin=1, cmax=65535),
    _bi("http_worker_processes_min", "range",
        "worker_processes 必须 >= 1", Severity.WARN,
        keys=["worker_processes"], cmin=1, cmax=1024),
    _bi("http_keepalive_timeout_min", "range",
        "keepalive_timeout 必须在 [1, 86400] 秒内", Severity.WARN,
        keys=["keepalive_timeout"], cmin=1, cmax=86400),
    _bi("http_gzip_enum", "enum",
        "gzip 必须是 on 或 off", Severity.WARN,
        keys=["gzip"], allowed=["on", "off"]),
    _bi("http_log_level_enum", "enum",
        "logging.level 必须是 debug/info/warn/error 之一", Severity.WARN,
        keys=["logging.level"],
        allowed=["debug", "info", "warn", "error"]),
    _bi("http_ssl_protocol_enum", "enum",
        "tls.protocol 必须是 TLSv1.2 或 TLSv1.3", Severity.WARN,
        keys=["tls.protocol"], allowed=["TLSv1.2", "TLSv1.3"]),
    _bi("http_ssl_cert_required", "conditional_required",
        "{key} 缺失（tls.enabled=true 需要该字段）", Severity.CRITICAL,
        when={"key": "tls.enabled", "value": True},
        then={"require": ["tls.cert_path", "tls.key_path"]}),
    _bi("http_protocol_ssl_conflict", "mutual_exclusion",
        "protocol=http 与 ssl=on 冲突", Severity.CRITICAL,
        keys=["protocol", "ssl"], forbid=[["http", "on"]]),
    _bi("http_mode_replicas_correlation", "correlation",
        "mode=cluster 时 replicas 必须 >= 3", Severity.WARN,
        when={"key": "mode", "value": "cluster"},
        then=[{"key": "replicas", "op": ">=", "value": 3}]),
    # --- db (10-14) ---
    _bi("db_port_range", "range",
        "db.port 必须在 [1, 65535] 范围内", Severity.WARN,
        keys=["db.port"], cmin=1, cmax=65535),
    _bi("db_pool_size_min", "range",
        "db.pool_size 必须 >= 1", Severity.WARN,
        keys=["db.pool_size"], cmin=1, cmax=1000),
    _bi("db_engine_enum", "enum",
        "db.engine 必须是 mysql/postgresql/sqlite/oracle 之一", Severity.WARN,
        keys=["db.engine"],
        allowed=["mysql", "postgresql", "sqlite", "oracle"]),
    _bi("db_ssl_cert_required", "conditional_required",
        "{key} 缺失（db.ssl=true 需要该字段）", Severity.CRITICAL,
        when={"key": "db.ssl", "value": True},
        then={"require": ["db.ssl_cert", "db.ssl_key"]}),
    _bi("db_replica_max_connections", "correlation",
        "db.mode=replica 时 db.max_connections 必须 >= 10", Severity.WARN,
        when={"key": "db.mode", "value": "replica"},
        then=[{"key": "db.max_connections", "op": ">=", "value": 10}]),
    # --- log (15-16) ---
    _bi("log_level_enum", "enum",
        "log.level 必须是 debug/info/warn/error 之一", Severity.WARN,
        keys=["log.level"], allowed=["debug", "info", "warn", "error"]),
    _bi("log_max_files_min", "range",
        "log.max_files 必须 >= 1", Severity.WARN,
        keys=["log.max_files"], cmin=1, cmax=100),
    # --- auth (17-20) ---
    _bi("auth_token_ttl_range", "range",
        "auth.token_ttl 必须在 [300, 86400] 秒内", Severity.WARN,
        keys=["auth.token_ttl"], cmin=300, cmax=86400),
    _bi("auth_password_min_length", "range",
        "auth.password_min_length 必须 >= 8", Severity.WARN,
        keys=["auth.password_min_length"], cmin=8, cmax=128),
    _bi("auth_algorithm_enum", "enum",
        "auth.algorithm 必须是 HS256 或 RS256", Severity.WARN,
        keys=["auth.algorithm"], allowed=["HS256", "RS256"]),
    _bi("auth_https_cert_required", "conditional_required",
        "{key} 缺失（auth.force_https=true 需要该字段）", Severity.CRITICAL,
        when={"key": "auth.force_https", "value": True},
        then={"require": ["auth.tls_cert"]}),
]

BUILTIN_CONSTRAINTS_BY_ID: Dict[str, Constraint] = {
    c.id: c for c in BUILTIN_CONSTRAINTS
}
