# cfgdrift — Semantic-Level Configuration Drift Detection

**English** | [简体中文](./README.md)

`cfgdrift` is a **semantic-level** configuration drift detection tool. Unlike traditional file-level (hash/byte comparison) approaches, it parses JSON, YAML, TOML, and INI configuration files into structured semantic trees, **ignoring format noise such as comments, indentation, and key order**, focusing only on the real "meaning" changes of the configuration.

Core capabilities:

- **Precise detection**: detects four drift types — added / removed / modified / type-changed — graded by CRITICAL / WARN / INFO severity, with false positives near zero
- **Closed-loop and actionable**: collect → parse → baseline → diff → report in a single command; baselines support versioning and rollback, and SQLite persistence keeps history traceable
- **Unattended**: a daemon runs in the background for periodic scans and automatically triggers three alert channels (webhook / email / script) on detected drift, with debounce-dedup and failure retry (**rule-level retry configurable**); supports **auto-start on boot** (systemd / launchd / schtasks across three platforms)
- **Engineering-friendly**: the exit code 0/1/2 contract plugs directly into CI/CD; JSON / **single-file offline HTML** report export; optional local web dashboard (timeline, severity distribution, **environment comparison**)
- **Extensible**: **pluggable parser interface** (entry point `cfgdrift.parsers` + decorator registration), supporting any custom configuration file format
- **Install anywhere**: C-core parsing with a pure-Python fallback; installable without a compiler on any Python 3.8+; cross-platform on Windows / Linux / macOS

Suitable for configuration change audits, security compliance checks, CI gates, and routine ops inspection.

## Features

- `cfgdrift init`: initialize the database (default `~/.cfgdrift/cfgdrift.db`)
- `cfgdrift scan`: scan a single file or directory, record history / save baselines / `--watch` polling
- `cfgdrift baseline create|list|show|rollback`: versioned baseline management
- `cfgdrift diff --baseline NAME`: diff against a baseline and output a drift report (with `file:line` line numbers; `--no-line` disables them)
- `cfgdrift compare ENV1 ENV2...`: multi-environment baseline comparison, with both baseline versions shown in the header `compare A -> B (vX vs vY)`; supports `environments.yaml` mapping and `--json`
- `cfgdrift severity add|list|remove|enable|disable`: custom severity override rules (`severity.yaml`; invalid regex errors with exit 2). Note: `severity add --key-pattern` / `--value-pattern` / `--file-pattern` are all **regex**; the `patterns` in `masking.yaml` follow **glob** (fnmatch) semantics — never mix the two
- `cfgdrift report --json out.json`: export a JSON report; `cfgdrift report --html out.html`: export a **single-file offline HTML** report (summary cards + severity distribution + change list, zero external dependencies)
- `cfgdrift ignore add|list|remove`: ignore rules (exact / prefix / regex)
- `cfgdrift serve`: start a local web dashboard (`127.0.0.1:8080`, requires the `[web]` extra), supporting the **environment comparison** view and an "Export HTML" report button
- `cfgdrift daemon enable-autostart|disable-autostart|autostart-status`: auto-start management (systemd / launchd / schtasks; `--user` default / `--system` optional / `--dry-run` preview / idempotent semantics)
- `cfgdrift alert add --retry-count N --retry-delay ...`: rule-level alert retry (total attempts + wait between attempts; defaults fall back to the global 3 attempts at 1s/5s/30s)
- **Sensitive value masking**: five display channels — terminal / JSON report / HTML report / Web API / alert payload — automatically mask 13 sensitive key categories such as `password` / `token` / `secret` (`masking.yaml` customizable; the database always stores raw values)
- **Line-number locating**: diff / compare output marks `file:line` to quickly pinpoint drift sources
- **Consistency constraints (v0.6.0)**: superimposes a constraint-checking layer on semantic diff, running five constraint types (range / enum / conditional_required / correlation / mutual_exclusion) against the "changed configuration tree", reporting only constraint violations **associated with the current drift**, and outputting deterministic, explainable compound alerts (severity escalation + `constraint_violations`)
- **Baseline violations report (v0.7.0)**: `cfgdrift scan --report-violations` outputs a separate "Baseline violations" section (supported in both terminal and JSON; **off by default** for zero noise), taking severity directly from the constraint itself; violations associated with drift are not reported twice
- **corpus benchmark corpus (v0.7.0)**: `cfgdrift corpus init|fetch|export|validate` mines configuration change pairs from real project git history and standardizes them into `instances.jsonl` corpus (metadata + before/after semantic trees + diff + feature + labels reserved), integrated with the diff / constraint engines; supports `local_path` local git repositories for offline collection and incremental fetching
- **Constraint auto-mining (v0.7.0)**: `cfgdrift constraint mine` mines candidates from historical scans / corpus (value domains enum/range, co-occurrence conditional_required, mutual exclusion), writing `<home>/mined_candidates.yaml` (`enabled: false`, `status: pending`, **not auto-activated**); `constraint add --rule` promotes them after manual confirmation
- **Web constraint view (v0.7.0)**: the web dashboard adds a "Constraints" view (active constraint list + enable/disable toggles for user rules + paginated recent constraint violations); violations are persisted to the C-10 `constraint_violations` table (retained 90 days by default, `CFGDRIFT_CV_RETENTION_DAYS` configurable)
- **Custom parser plugins**: `--format <plugin>` accepts third-party parsing formats (see "Custom Parser Plugins" below)

Exit codes: `0`=no drift, `1`=drift detected, `2`=error.

## License

This project is licensed under the **MIT License** (see the [`LICENSE`](./LICENSE) file in the repository root).

Under the MIT License, you are free to use, modify, and distribute this project, including for commercial purposes, as long as you retain the original copyright notice and license text.

## Installation

Since v0.2.0 (current version v0.8.0), `cfgdrift` is a general package **installable and runnable on any Python 3.8+**:
the C extension is an optional accelerator — when not compiled or when the build fails, it automatically falls back to the pure-Python parsers.

```bash
pip install cfgdrift            # general install (pip auto-selects the wheel; see below)
pip install "cfgdrift[web]"     # includes the web dashboard
pip install "cfgdrift[dev]"     # includes test dependencies
```

### Dual-wheel release model

| Artifact | Target users | Description |
|--------|----------|------|
| `cfgdrift-0.5.0-py3-none-any.whl` | all Python 3.8+ | pure-Python universal wheel (default primary release) |
| `cfgdrift-0.5.0-cp313-*-*.whl` | CPython 3.13 | optional C-accelerated platform wheel (faster JSON/TOML/INI parsing) |
| `cfgdrift-0.5.0.tar.gz` (sdist) | requires local compilation | ships C sources; `pip install` tries to compile and auto-falls back on failure |

pip's tag priority naturally delivers the accelerated wheel to CPython 3.13 users and the universal one to everyone else.

### Local build recipes

```bash
# pure-Python universal wheel (deterministic, no C compilation)
CFGDRIFT_NO_C=1 python -m build --wheel

# C-accelerated platform wheel (optional; requires a C99 compiler)
python -m build --wheel

# sdist (ships C sources; do not set CFGDRIFT_NO_C)
python -m build --sdist
```

### Environment variables

| Variable | Value | Description |
|------|------|------|
| `CFGDRIFT_BACKEND` | `auto` (default) / `pure` / `c` | parser backend selection. `auto` uses C when available and silently falls back to pure Python; `pure` forces pure mode; `c` forces C and raises `RuntimeError` at import when unavailable |
| `CFGDRIFT_DEBUG` | `1` | enables `logging.DEBUG`, logging the current parse backend (`parser backend: c/pure`) |
| `CFGDRIFT_NO_C` | `1` / `true` / `yes` | skips C extension compilation at build time (produces a pure wheel) |
| `CFGDRIFT_HOME` | path | overrides the data directory (default `~/.cfgdrift/`) |
| `GITHUB_TOKEN` | token | GitHub token for the corpus star check (takes precedence over the `token` field in corpus.yaml) |
| `CFGDRIFT_CV_RETENTION_DAYS` | integer | retention days for the C-10 `constraint_violations` table (default 90; lazy cleanup every 200 inserts, capped at 20000 rows) |

### Dual-mode consistency

Valid input produces **semantically equivalent** trees under both the C and pure-Python backends (type-sensitive, key order not considered);
invalid input raises `ValueError` in both backends, with messages starting with
`parse error at line L, column C` (text after the colon may differ). `tests/test_dual_mode.py` runs consistency regression on both backends using the same corpus.

Known documented differences (see the v0.2.0 section of `docs/system_design.md`):

- Unpaired surrogate pairs in JSON (e.g. `"\ud83d"`): pure mode accepts (stdlib behavior), C mode rejects;
- fractional seconds of timezone-less TOML datetimes: C outputs the literal (`...00.5`), pure mode outputs
  zero-padded 6-digit `isoformat()` (`...00.500000`);
- local times with a UTC offset (`07:32:00Z`) violate TOML v1.0 syntax: pure mode rejects, C mode accepts as a literal;
- INI trailing content after section headers (`[s] junk`) and section names with spaces (`[ s ]`) are normalized differently by the two backends;
- INI multi-line continuations: C rejects indented continuations, while pure mode's configparser accepts them as multi-line values (more lenient).

## Quick Start

```bash
cfgdrift init
cfgdrift scan ./config --save-as-baseline prod
# …修改配置…
cfgdrift diff ./config --baseline prod          # 退出码 1 = 有漂移
cfgdrift serve                                   # 打开 http://127.0.0.1:8080
```

## Directory Scan Conventions

- Extension recognition: `.json` / `.yaml|.yml` / `.toml` / `.ini|.cfg|.conf`; unknown extensions are skipped with a warning
- A single file with an unknown extension requires an explicit `--format`, otherwise it errors out
- Snapshot structure `{relpath: tree}`; file-level addition=INFO, deletion=CRITICAL
- List diffs are compared by index; element reordering is not detected (known limitation)

## Data Directory

Default `~/.cfgdrift/`, overridable via the `CFGDRIFT_HOME` environment variable or the CLI global option `--store PATH`.

## daemon Auto-start on Boot (v0.5.0)

```bash
# Linux: 写 ~/.config/systemd/user/cfgdrift.service（--user 默认）并 systemctl --user enable
# macOS: 写 ~/Library/LaunchAgents/com.cfgdrift.daemon.plist 并 launchctl load -w
# Windows: 创建计划任务 schtasks /Create /TN cfgdrift-daemon /SC ONLOGON
cfgdrift daemon enable-autostart --target /etc/nginx --baseline prod --interval 300
cfgdrift daemon enable-autostart --target /etc/nginx --baseline prod --dry-run   # 预览，零落盘
cfgdrift daemon disable-autostart
cfgdrift daemon autostart-status        # 退出码 0=enabled / 1=disabled / 2=error
```

- The single source of truth for auto-start config is `<home>/autostart.json`, dual-written and dual-cleaned together with platform artifacts
- Validation before `enable`: target exists, baseline exists, `--interval >= 60`, `--format` valid
- Idempotent: already enabled with identical params → no-op (exit 0); different params → `--force` required to override (otherwise exit 2)
- `--system` is explicitly optional (requires root/admin privileges)

## Configurable Alert Retry (v0.5.0)

```bash
# 规则级：总尝试次数 5，间隔用全局默认 (1,5,30)
cfgdrift alert add --name nginx-webhook --type webhook --url http://x --retry-count 5
# 规则级：只给间隔 → 尝试次数 = len(delays)+1（此处 4 次）
cfgdrift alert add --name ops-email --type email --smtp-host ... --retry-delay 2,10,60
# 逗号分隔与重复两种写法等价
cfgdrift alert add --name x --type webhook --url http://x --retry-delay 1 --retry-delay 5 --retry-delay 30
```

- `retry_count` = total attempts (default 3, ≥1); `retry_delays` = wait-seconds list between attempts (elements ≥0)
- Rule-level overrides the global default; `alert list` shows `retry=3/1,5,30` or `retry=default`; debouncing (600s cooldown) unchanged

## HTML Report Export (v0.5.0)

```bash
cfgdrift report --scan-id 3 --html out.html   # 单文件离线 HTML，可直接浏览器打开
```

- Summary cards + severity distribution bar + change list (severity badge / key path / change type / file:line / old→new value / rule / constraint violations)
- Items with `masked=true` show a "masked" badge; severity colors match the web dashboard
- The web report page also provides an "Export HTML" button (`GET /api/reports/{scan_id}/html`)

## Consistency Constraints (v0.6.0)

Consistency constraints superimpose a **constraint-checking layer** on top of semantic diff: after `diff` / `scan` / `daemon` detect drift, they run constraint checks against the "changed configuration tree", **reporting only constraint violations associated with the current drift** (Q2: pre-existing P0 violations are not reported),
outputting deterministic, explainable compound alerts (severity escalation + `constraint_violations`). No dependency on external business truth.

```bash
# diff / scan 默认启用内置约束库（20 条，四域：web/db/log/auth × 五类）
cfgdrift diff ./config --baseline prod
cfgdrift diff ./config --baseline prod --no-builtin          # 关闭内置库
cfgdrift diff ./config --baseline prod --constraints extra.yaml   # 追加额外约束文件（可重复）

# constraint 子命令：管理 <home>/constraints.yaml（version: 1）
cfgdrift constraint add --rule '{"id":"my_port","type":"range","keys":["server.port"],"min":1,"max":65535,"message":"server.port 必须 <= 65535"}'
cfgdrift constraint list                  # 生效视角（builtin + user 合并，同 id 后者覆盖）
cfgdrift constraint list --source builtin # 只看内置库；--source user 只看 constraints.yaml
cfgdrift constraint remove my_port
cfgdrift constraint disable my_port       # / enable my_port
```

- **Five constraint types**: `range` (numeric range), `enum` (whitelist), `conditional_required`
  (required key when the condition holds), `correlation` (numeric/string relation when the condition holds), `mutual_exclusion`
  (two keys are mutually exclusive). Full schema: `examples/constraints.yaml.example`.
- **Escalation rule (Q1)**: `new = min(CRITICAL, max(item.rank+1, max(violated_constraint.rank)))`,
  reusing `Severity.rank` (NONE=0 / INFO=1 / WARN=2 / CRITICAL=3) without introducing a separate CONSTRAINT level;
  each item is escalated only once (computed uniformly after all violations are attached).
- **Association rule (D5)**: per file, `involved_keys ∩ drift keys ≠ ∅` means associated; the violation attaches to all
  drift items with `key_path ∈ involved_keys`; constraints with missing keys / unmet `when` are always skipped (basis of zero noise).
- **Five presentation channels**: terminal (appends `constraint <id> [<type>]: <message>` to the item), JSON
  (`DriftItem.to_dict()` outputs `constraint_violations` only when non-empty), HTML (new "Constraint Violations" column),
  web dashboard (badge+message on the report page / alert list), alert payload (per-item `constraint` field; the first
  item is ordered by `constraint_id`).
- **Active constraint resolution (D8)**: built-in library (`--builtin` on by default) → `<home>/constraints.yaml` (if present)
  → `--constraints` extra files (repeatable, in order); same id, the latter overrides the former (built-ins can be overridden).
- **daemon activation timing (D9)**: the worker reloads constraint files each cycle; `constraint add` takes effect the next cycle;
  `severity_rules` stay loaded at startup.
- **Zero-noise contract (D7)**: legitimate changes (e.g. `server.port` 8080→9090 in range) produce output byte-identical to v0.5.0 —
  no `constraint_violations` field, no new terminal lines, the new HTML column shows `-`,
  and the alert payload has no `constraint` field.
- compare (baseline-to-baseline) does not run constraint checks in this version (D10).

## Custom Parser Plugins (v0.5.0)

Besides the built-in `auto/json/yaml/toml/ini`, `--format` also accepts **registered plugin names**.
Plugins return a **raw tree** (dict/list/scalar), which the engine normalizes into a semantic tree uniformly; an optional
`build_line_map` provides a `{key_path: line number}` mapping (if absent, the line number is
`None` and diff rendering omits `:N`). A `ValueError` /
`RuntimeError` / `OSError` raised by the plugin's `parse` is caught by the CLI as `error: <message>` and exits with **exit 2**.
Error messages should carry 1-based line numbers (e.g. `unbalanced '{' at line 1`).

### Plugin Protocol (real signatures)

```python
# cfgdrift.core.plugins
class ParserPlugin:
    def __init__(self, name: str, extensions: Tuple[str, ...] = (),
                 parse: Optional[Callable[[str], Any]] = None,
                 build_line_map: Optional[Callable[[str], Dict[str, int]]] = None): ...

def register_plugin(name: Optional[str] = None, extensions: Tuple[str, ...] = (),
                    line_map: Optional[Callable[[str], Dict[str, int]]] = None,
                    registry: Optional[PluginRegistry] = None): ...   # 装饰器
```

Key points:

- After `parse(text)` returns a raw tree, the engine runs the **exact same**
  `_normalize` / `_wrap_top_level` normalization as built-in formats: keys converted to `str`; non-dict top levels wrapped as
  `{"$": value}`; `datetime/date/time` converted to ISO-8601 strings; `None` treated as an empty
  dict; other non-conventional objects converted to strings.
- The **key_path convention** for line mappings matches built-in formats (`cfgdrift.core.model.join_path`):
  dict segments joined with `.`, list indices appended as `[i]`, and segments containing `.` / `[` / `]` / `\`
  backslash-escaped. Plugins should call `join_path` to build keys (see the example below), never hand-concatenate strings.
- Without `build_line_map`: `parse_text_lines` returns empty `{}`, diff
  `item.line` is `None`, and output contains no `:N`. Exceptions raised by `build_line_map` itself are also
  degraded to a warning + empty mapping (line numbers are an enhancement and never block parsing).
- `extensions` must be lowercase with a dot prefix (e.g. `(".dsl",)`); `detect_format` falls back to plugin extensions after built-ins.

### Method A: Decorator Registration (in-process, effective on import)

```python
# mydsl_plugin.py
from cfgdrift.core.plugins import register_plugin
from cfgdrift.core.model import join_path

def parse_mydsl(text: str):
    tree = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            tree[k.strip()] = v.strip()
    return tree

def line_map_mydsl(text: str):
    return {join_path([("key", k.strip())]): i + 1
            for i, line in enumerate(text.splitlines())
            if "=" in line
            for k in [line.split("=", 1)[0]]}

@register_plugin("mydsl", extensions=(".dsl",), line_map=line_map_mydsl)
def _registered(text: str):
    return parse_mydsl(text)
```

- A bare `@register_plugin` is also valid (the plugin name takes the function name); `registry=reg` registers the plugin
  to a custom `PluginRegistry` (default: the global shared registry).
- Note: if a same-named plugin was already registered via an entry point (Method B activated with `pip install -e`),
  copying this example as a decorator registration in the same process raises
  `ValueError: parser plugin 'mydsl' is already registered (use replace=True to overwrite)`
  — `register_plugin` internally registers with `replace=False`, the opposite of entry points'
  `replace=True`. In that case use a different plugin name, or rely only on the import
  side-effect of `import mydsl_parser` (the equivalent Method A registration is already done at import time).
- After importing the module in a script/test process, it is usable:

```bash
cfgdrift scan app.dsl --format mydsl        # 扩展名 .dsl 也可自动识别
```

### Method B: Entry Point Registration (pip-packaged distribution, takes precedence over same-name decorators)

```toml
# pyproject.toml（插件包）
[project.entry-points."cfgdrift.parsers"]
mydsl = "mydsl_plugin:plugin"
```

Entry point values support four forms (normalized by `_coerce_entry_point`):

1. **`ParserPlugin` instance** (recommended): `plugin = ParserPlugin(name="mydsl",
   extensions=(".dsl",), parse=parse_mydsl, build_line_map=line_map_mydsl)`
2. A `(parse_fn, {"extensions": [...], "line_map": fn})` tuple — note the option key is
   `line_map` (not `build_line_map`); `name` defaults to the entry point name;
3. A bare `parse(text)` function (plugin name takes the entry point name);
4. A `{"parse": fn, "extensions": [...], "line_map": fn}` mapping.

On same-name conflicts, **entry points override decorator registrations** (`replace=True`); a single plugin failing
to load is only a warning and does not affect built-in parsers; plugin discovery uses the Python 3.8+ standard library
`importlib.metadata`, with zero new dependencies.

### Complete Runnable Example: examples/mydsl-parser

The repository ships a complete runnable nginx-like DSL plugin package
`examples/mydsl-parser/` (with pyproject.toml, parser source, line-number mapping, example config
`examples/demo/nginx-like.dsl`, pytest tests, and a package-internal README). End-to-end demo:

```bash
# 0) 安装示例插件包（entry point 注册；无第三方依赖）
pip install -e examples/mydsl-parser

# 1) 建基线：显式 --format mydsl（或省略，按 .dsl 扩展名自动识别）
cfgdrift scan examples/demo/nginx-like.dsl --format mydsl --save-as-baseline mydsl-demo

# 2) 修改配置：把 examples/demo/nginx-like.dsl 第 4 行 listen 8080 改成 8081

# 3) diff：行号精确定位到真实文件行
cfgdrift diff examples/demo/nginx-like.dsl --baseline mydsl-demo
# [WARN] server[0].listen (nginx-like.dsl:4): 修改 8080 -> 8081
# Summary: added=0 removed=0 modified=1 type_changed=0 ignored=0 total=1 max=WARN
```

In-process usage of Method A (wrapper script, no entry point dependency):

```python
import mydsl_parser                       # import 即通过装饰器注册 "mydsl"
from cfgdrift.cli import main
raise SystemExit(main(["scan", "app.dsl", "--format", "mydsl"]))
```

The example plugin's line mapping demonstrates the `join_path` convention under multi-level nesting + array indices
(`server[0].location./api.proxy_pass` → line 9), and repeated `server` blocks automatically
becoming arrays (`server[0]` / `server[1]`).

### Plugin Test Example (pytest)

```python
# tests/test_my_plugin.py
from cfgdrift.core import parser as parser_mod
from cfgdrift.core.plugins import ParserPlugin
from mydsl_plugin import parse_mydsl, line_map_mydsl   # 方式 A 里的两个函数

PLUGIN = ParserPlugin(
    name="mydsl", extensions=(".dsl",),
    parse=parse_mydsl, build_line_map=line_map_mydsl,
)

def test_dispatch_and_line_map():
    parser_mod._PLUGIN_REGISTRY.register(PLUGIN, replace=True)
    try:
        tree, lm = parser_mod.parse_text_lines("a=1\nb=2", "mydsl")
        assert tree == {"a": "1", "b": "2"}
        assert lm == {"a": 1, "b": 2}
    finally:
        # 清理共享 registry，避免影响其他用例
        parser_mod._PLUGIN_REGISTRY._plugins.pop("mydsl", None)
        parser_mod._PLUGIN_REGISTRY._ext_index.pop(".dsl", None)
```

All 17 test cases are in `examples/mydsl-parser/tests/test_mydsl_parser.py` (parse correctness,
precise line-number mapping, `parse_text`/`parse_text_lines` dispatch, diff line-number placement, and the two
decorator / entry point registration forms).

- Unregistered `--format` errors include registration guidance (`cfgdrift.parsers` entry point group / `register_plugin`)
- Same-name conflicts: entry points override decorators; a single plugin loading failure is only a warning and does not affect built-in parsers
- Plugin discovery uses the Python 3.8+ standard library `importlib.metadata`, with zero new dependencies

## v0.7.0 Increments: Corpus / Constraint Mining / Web Constraint View / Baseline Violations Report

v0.7.0 adds four capabilities on top of the v0.6.0 consistency constraints, all connected via **new modules + optional parameters/conditional output on existing interfaces**; `core/differ.py` and the constraint engine stay **pure functions with zero DB dependencies** (violation persistence is done by the caller layer).

### 1. corpus Benchmark Corpus Toolchain (Direction E)

```bash
cfgdrift corpus init --workspace <dir>          # 生成 corpus.yaml + state.json + repos/
cfgdrift corpus fetch --workspace <dir>          # 拉取 git 历史 -> 变更对 -> 全量重写 instances.jsonl
cfgdrift corpus export --workspace <dir>         # 幂等全量重写（确定性）
cfgdrift corpus validate --workspace <dir>       # JSONL schema 校验 + 统计（损坏 exit 2）
```

- `corpus.yaml` config (version:1): `since` / `min_stars` / `max_instances` (default 200) / `token` / `repositories[]` (either `owner`+`repo` or `local_path`; `glob` / repository-level `since` optional). Template: `examples/corpus.yaml.example`
- Collection: non-local repos use `git clone --filter=blob:none --no-checkout` + incremental `git fetch`; `local_path` uses a local git repo directly (**offline / CI-safe**); star checks go through the GitHub API (best-effort; failure warns but does not block)
- Incremental: `state.json` records each repo's `last_commit` / `stars` / `instance_count`; fetch only handles new commits since last time
- Instance schema (`instances.jsonl`, one change instance per line): `schema_version` / `instance_id` / `metadata` / `file` / `before` / `after` (semantic tree + parse_ok + present) / `diff` (items + summary + constraint_violations + feature{changed_keys, changed_values, co_change_pairs, co_change_capped}) / `labels` (severity + annotation/annotator reserved); **raw text is not persisted** (prevents bloat)

### 2. Constraint Auto-mining (C-08)

```bash
cfgdrift constraint mine --min-support 5 --source scans        # 默认：当前 store 的 scan_items
cfgdrift constraint mine --source corpus --corpus instances.jsonl
cfgdrift constraint mine --json                                # 输出完整 JSON（含 metrics）
```

- Three candidate types: **value domains** (enum: distinct in [2,8]; range: all-numeric, port keys suggested [1,65535], others marked `observed:true`), **co-occurrence** (conditional_required: co/cnt >= 0.8), **mutual exclusion** (mutual_exclusion: zero intersection and >= min_support samples per side, top-5 per key pair)
- Outputs `<home>/mined_candidates.yaml` (version:1; candidates `enabled: false`, `status: pending`), **the candidate zone never auto-activates**; promotion = `cfgdrift constraint add --rule '<constraint JSON>'` (+ `constraint enable`). Template: `examples/mined_candidates.yaml.example`

### 3. Web Constraint View (C-09) + C-10 Violation Persistence

- New `constraint_violations` table (drift / baseline kinds), retained 90 days by default (`CFGDRIFT_CV_RETENTION_DAYS` configurable), lazy cleanup every 200 inserts + 20000-row cap
- New web endpoints: `GET /api/constraints` (effective view, same as `constraint list --source all`), `PUT /api/constraints/{id}/enabled` (toggle user rules; built-in constraints → 400), `GET /api/constraint-events` (paginated)
- The SPA adds a "Constraints" view (active constraint table + user-rule enable/disable + paginated recent violations)

### 4. Baseline Violations Report (C-07)

```bash
cfgdrift scan PATH --baseline B --report-violations    # 默认关闭
```

- `ConstraintEngine.check_tree` runs all enabled constraints per file on the new_snapshot; `baseline_violations` and drift-associated violations are deduplicated by set-difference on the signature `(constraint_id, file, frozenset(involved_keys))`, with severity taken directly from the constraint
- Terminal outputs a "Baseline violations:" section (after items, before Summary); JSON outputs the `baseline_violations` field; **byte-identical to v0.6.0 when off by default**; the HTML report does not render this section (`htmlreport.py` untouched)
- C-10 writes: `scan --report-violations` writes both drift and baseline kinds; daemon writes drift violations only

> Note: `instances.jsonl` is generated by `corpus fetch/export` with a **full rewrite** (idempotent); the git operations of `corpus fetch` depend on the `git` executable in PATH; for offline/CI use the `local_path` local git repository.

## v0.8.0 Increments: Dual Annotation + kappa / severity×constraint_id / compare Constraint Loop / Business-Impact Narrative

v0.8.0 adds four capabilities on top of v0.7.0, all connected via **new modules + optional parameters/conditional output on existing interfaces** (no new third-party dependencies; versions synced in three places `0.8.0 / 0.8.0 / 0.8.0-c`).

### 1. corpus Dual Annotation + kappa (C-C5)

```bash
cfgdrift corpus annotate --workspace <dir> --annotator alice            # 交互标注（[1]severe [2]minor [3]normal [s]跳过 [q]保存退出）
cfgdrift corpus annotate --workspace <dir> --annotator alice --batch labels.yaml   # 非交互批量导入（CI 友好）
cfgdrift corpus kappa --workspace <dir> [--annotator-a A --annotator-b B] [--weighted linear|quadratic] [--json]
cfgdrift corpus stats --workspace <dir> [--json]
```

- **Separate storage**: annotations are written to `<workspace>/annotations.jsonl` (`{instance_id, annotator, annotation, annotated_at}`, 3-class ordinal `severe|minor|normal`), separated from `instances.jsonl` — the latter is fully rewritten by export, so separate storage prevents loss
- **Export merge (D3)**: `corpus export` projects each instance's **latest** annotation (sorted by `annotated_at`; ties broken by annotator lexicographic order) into `labels.annotation` / `labels.annotator`; **repeated exports never lose annotations**
- **kappa (Q2)**: Cohen's kappa (`po=Σn_ii/n`, `pe=Σrow·col/n²`, `κ=(po−pe)/(1−pe)`) + agreement rate + confusion matrix (rows=A columns=B); `--weighted linear|quadratic` outputs weighted kappa; without arguments, auto-pairs the two annotators with the most overlap (D4); fewer than 2 annotators / overlap < 2 instances → exit 2
- **stats (§6.3)**: total instances / unannotated / single-annotated (per annotator) / dual complete / agreement rate / computable kappa count
- Template: `examples/annotations.jsonl.example`

### 2. severity References constraint_id (C-13)

```bash
cfgdrift severity add --name port-critical --severity CRITICAL --constraint-id http_port_range   # 单个或逗号分隔/多次
cfgdrift severity list        # 输出追加 constraint=<ids>（无则 -）
```

- `SeverityRule.constraint_id` (str/list normalized to `List[str]`) is an **extra AND condition**: the rule applies only when the constraint violation associated with the item hits that id; combined with `key_pattern` etc., all must hold to match; file-order first-match-wins unchanged
- **Pipeline order (D1)**: constraint attach (attach only, no escalation) → severity override (constraint_id rules can read `item.constraint_violations`) → unified escalation `min(3, max(rank+1, max_c_rank))` → summary.max_severity → alert threshold; the escalation formula is monotonic in severity, **byte-identical to v0.7.0 when no constraint_id rules exist**
- `to_dict` outputs `constraint_id` only when non-empty (old rule YAML bytes unchanged)

### 3. compare Runs Constraints (D10 completed)

```bash
cfgdrift compare dev prod --constraints my.yaml     # 自定义约束
cfgdrift compare dev prod --no-builtin              # 关闭内置约束库
cfgdrift compare dev prod --json                    # constraint_violations 进 JSON
```

- `CompareReport.constraint_violations`: `{"env_a": [...], "env_b": [...]}` (`check_tree` shape, severity taken directly from the constraint); terminal renders a "Constraint Check (D10 completed)" block after items and before Summary (**only when non-empty**)
- Violations are **informational**: the exit code stays drift-based (0/1/2 semantics unchanged, D6); byte-identical to v0.7.0 when no violations

### 4. Business-Impact Narrative (Direction A)

```bash
cfgdrift explain ./config --baseline prod [--format text|json] [--schema schema.yaml] [--no-llm]
cfgdrift diff ./config --baseline prod --explain       # diff 末尾追加同一叙事区块
```

- Each drift outputs `{key, change_type, severity, impact, evidence[], source}`; the **deterministic template engine** works offline (built-in 24+ key semantic dictionary + four change-type templates + constraint-violation / `latest` special cases / severity fallback)
- **LLM enhancement (Q5)**: OpenAI-compatible REST (stdlib `urllib`, `CFGDRIFT_LLM_URL/KEY/MODEL/TIMEOUT`), `temperature=0`; missing key / timeout / HTTP error / JSON parse failure / **evidence validation failure** → uniformly falls back to templates and marks `source: template`
- **Evidence-chain anti-hallucination (A-P0-3)**: `evidence` only takes three input-fact forms (`key:` / `value:` / `constraint: …violated`); LLM output is validated by `EvidenceValidator` (evidence⊆facts, constraint_id ∈ facts, fabricated-key detection); any failure falls back the entire item
- **Masking (D7)**: explain is a display channel; `SensitiveMasker` masks first, then narrates; sensitive values never leak
- Template: `examples/explain_schema.yaml.example` (`{patterns: {regex: description}}` user-dictionary merge, user entries take precedence)

## v0.7.0 Increments: Corpus / Constraint Mining / Web Constraint View / Baseline Violations Report

v0.7.0 adds four capabilities on top of the v0.6.0 consistency constraints, all connected via **new modules + optional parameters/conditional output on existing interfaces**; `core/differ.py` and the constraint engine stay **pure functions with zero DB dependencies** (violation persistence is done by the caller layer).

### 1. corpus Benchmark Corpus Toolchain (Direction E)

```bash
cfgdrift corpus init --workspace <dir>          # 生成 corpus.yaml + state.json + repos/
cfgdrift corpus fetch --workspace <dir>          # 拉取 git 历史 -> 变更对 -> 全量重写 instances.jsonl
cfgdrift corpus export --workspace <dir>         # 幂等全量重写（确定性）
cfgdrift corpus validate --workspace <dir>       # JSONL schema 校验 + 统计（损坏 exit 2）
```

- `corpus.yaml` config (version:1): `since` / `min_stars` / `max_instances` (default 200) / `token` / `repositories[]` (either `owner`+`repo` or `local_path`; `glob` / repository-level `since` optional). Template: `examples/corpus.yaml.example`
- Collection: non-local repos use `git clone --filter=blob:none --no-checkout` + incremental `git fetch`; `local_path` uses a local git repo directly (**offline / CI-safe**); star checks go through the GitHub API (best-effort; failure warns but does not block)
- Incremental: `state.json` records each repo's `last_commit` / `stars` / `instance_count`; fetch only handles new commits since last time
- Instance schema (`instances.jsonl`, one change instance per line): `schema_version` / `instance_id` / `metadata` / `file` / `before` / `after` (semantic tree + parse_ok + present) / `diff` (items + summary + constraint_violations + feature{changed_keys, changed_values, co_change_pairs, co_change_capped}) / `labels` (severity + annotation/annotator reserved); **raw text is not persisted** (prevents bloat)

### 2. Constraint Auto-mining (C-08)

```bash
cfgdrift constraint mine --min-support 5 --source scans        # 默认：当前 store 的 scan_items
cfgdrift constraint mine --source corpus --corpus instances.jsonl
cfgdrift constraint mine --json                                # 输出完整 JSON（含 metrics）
```

- Three candidate types: **value domains** (enum: distinct in [2,8]; range: all-numeric, port keys suggested [1,65535], others marked `observed:true`), **co-occurrence** (conditional_required: co/cnt >= 0.8), **mutual exclusion** (mutual_exclusion: zero intersection and >= min_support samples per side, top-5 per key pair)
- Outputs `<home>/mined_candidates.yaml` (version:1; candidates `enabled: false`, `status: pending`), **the candidate zone never auto-activates**; promotion = `cfgdrift constraint add --rule '<constraint JSON>'` (+ `constraint enable`). Template: `examples/mined_candidates.yaml.example`

### 3. Web Constraint View (C-09) + C-10 Violation Persistence

- New `constraint_violations` table (drift / baseline kinds), retained 90 days by default (`CFGDRIFT_CV_RETENTION_DAYS` configurable), lazy cleanup every 200 inserts + 20000-row cap
- New web endpoints: `GET /api/constraints` (effective view, same as `constraint list --source all`), `PUT /api/constraints/{id}/enabled` (toggle user rules; built-in constraints → 400), `GET /api/constraint-events` (paginated)
- The SPA adds a "Constraints" view (active constraint table + user-rule enable/disable + paginated recent violations)

### 4. Baseline Violations Report (C-07)

```bash
cfgdrift scan PATH --baseline B --report-violations    # 默认关闭
```

- `ConstraintEngine.check_tree` runs all enabled constraints per file on the new_snapshot; `baseline_violations` and drift-associated violations are deduplicated by set-difference on the signature `(constraint_id, file, frozenset(involved_keys))`, with severity taken directly from the constraint
- Terminal outputs a "Baseline violations:" section (after items, before Summary); JSON outputs the `baseline_violations` field; **byte-identical to v0.6.0 when off by default**; the HTML report does not render this section (`htmlreport.py` untouched)
- C-10 writes: `scan --report-violations` writes both drift and baseline kinds; daemon writes drift violations only

> Note: `instances.jsonl` is generated by `corpus fetch/export` with a **full rewrite** (idempotent); the git operations of `corpus fetch` depend on the `git` executable in PATH; for offline/CI use the `local_path` local git repository.
