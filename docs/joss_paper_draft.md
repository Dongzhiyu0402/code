# cfgdrift: Semantic-Level Configuration Drift Detection

[Author Name 1], [Author Name 2] ([corresponding author placeholder — one name required for JOSS])

**Affiliations:** [Affiliation 1 — institution, city, country]; [Affiliation 2 — institution, city, country]

---

## Summary

cfgdrift is a semantic-level configuration drift detection system for modern infrastructure. Unlike textual diff tools, cfgdrift parses configuration files (JSON, TOML, INI, and YAML) into normalized semantic trees before differencing, so that formatting, whitespace, and key-order noise are ignored and only structural and value changes are reported as one of four change types: `added`, `removed`, `modified`, and `type_changed`. Every change is automatically classified into `CRITICAL` / `WARN` / `INFO` severity, sensitive values (passwords, tokens, secrets, and 10 other key families) are masked consistently across all outputs, and a rule-based constraint engine (v0.6.0) detects cross-key consistency violations such as out-of-range values, enum conflicts, conditional requirements, correlated keys, and mutual exclusions, with 20 built-in constraint libraries for the web, database, logging, and authentication domains. A daemon mode (v0.3.0) performs periodic scans and alerts through webhook, email, and script channels. The `corpus` toolchain (v0.7.0–0.8.0) extracts real configuration change pairs from GitHub repository history into a reproducible benchmark corpus and supports dual-annotator labeling with Cohen's kappa inter-rater reliability analysis. An optional LLM-backed `explain` command (v0.8.0) generates business-impact narratives grounded by an evidence validator. The system has been validated on a corpus of 112 instances from 7 real-world repositories, achieving 950 passing tests, and guarantees byte-identical output to the previous version when a change is legitimate — a zero-noise contract that makes the tool safely regressible.

## Statement of need

Configuration drift — the silent divergence of deployed configuration from its intended, reviewed, or baseline state — is a leading cause of production incidents in modern infrastructure. Configuration files for containers, reverse proxies, monitoring systems, and infrastructure-as-code are typically version-controlled, but a change of a single value such as a port, an image tag, or a certificate path can break a service in ways that are invisible to conventional `diff` output. Textual comparison tools are poorly suited to this problem: they report formatting and ordering differences as first-class changes, cannot classify the *kind* of a change, and provide no sense of whether a change is benign, risky, or critical.

Existing drift detection tools largely operate on one of two extremes. Some monitor only the *deployed state* versus a declared state (e.g., infrastructure-as-code reconciliation), and are therefore blind to version-history changes that precede deployment. Others treat configuration as opaque key-value blobs and diff them textually, producing noisy output that operators quickly learn to ignore. What is missing is a tool that (i) understands configuration *semantics*, (ii) works on the version history itself rather than only live state, (iii) assigns actionable severity and explains the *business impact* of each drift, and (iv) can be held to a verifiable quality bar. cfgdrift addresses this gap: it is a pure-analysis, history-aware drift detector with semantic diffing, severity classification, consistency-constraint reasoning, and a benchmark corpus that quantifies how well drift detection agrees with human judgment.

## Software description

cfgdrift is a command-line tool (Python 3.8+) organized around a small set of composable subcommands. The core workflow is *scan against a baseline*: a baseline snapshot is stored locally (SQLite under `~/.cfgdrift/`), and subsequent scans report the semantic delta.

```
$ cfgdrift init                      # initialize workspace and default baseline
$ cfgdrift scan app/ --baseline prod # compare current files against the prod baseline
$ cfgdrift diff before.yaml after.yaml
$ cfgdrift compare dev prod          # compare two environments
```

### Semantic diffing

All supported formats are parsed into a normalized semantic tree; diffing operates on trees, not text. A reformatted file with reordered keys produces no changes, while a changed value produces a precisely located `modified` item with line numbers. The four change classes (`added`, `removed`, `modified`, `type_changed`) are emitted with per-key paths and old/new values. Custom formats can be plugged in via `register_plugin` or Python entry points, so the same semantic pipeline extends to project-specific configuration dialects.

### Severity classification and masking

Each diff item is automatically graded `CRITICAL`, `WARN`, or `INFO`. Users can override grading with custom severity rules based on key patterns, and rule engines can raise a violated `WARN` to `CRITICAL` automatically. Sensitive keys — `password`, `token`, `secret`, and 10 related families — are masked in four output surfaces (CLI, reports, web dashboard, explain narratives) so that drift reports are safe to share.

### Consistency-constraint reasoning (v0.6.0)

A declarative constraint engine encodes cross-key invariants that a single-file diff cannot see. Five constraint types are supported: `range`, `enum`, `conditional_required`, `correlation`, and `mutual_exclusion`, plus 20 built-in libraries covering web, database, logging, and authentication domains. Constraints are managed through a dedicated subcommand:

```
$ cfgdrift constraint list
$ cfgdrift constraint add --rule '{"type":"enum","key_pattern":"log.level","values":["debug","info","warn","error"]}'
$ cfgdrift constraint enable <id>
```

A constraint-mining feature (v0.7.0) proposes candidate rules from observed co-changes, but candidates are never auto-activated — they must be reviewed and promoted explicitly.

### Daemon, alerts, and reporting

`cfgdrift daemon` performs periodic scans and forwards findings through three alert channels (webhook, email, script) with configurable retry. Auto-start registration is supported for systemd, launchd, and Windows Task Scheduler. `cfgdrift serve` launches a local web dashboard; `cfgdrift report --html` renders a self-contained offline HTML report covering drift, constraints, and alerts.

### Benchmark corpus toolchain and annotation reliability (v0.7.0–0.8.0)

The `corpus` subcommand family builds a reproducible benchmark from real GitHub history: `fetch` clones repositories and extracts configuration change pairs since 2024-01, `export` normalizes them into JSONL instances (metadata + semantic trees + diff features + constraint violations), `annotate` supports batch dual-annotation, and `kappa` computes Cohen's kappa with linear/quadratic weighting and confusion matrices:

```
$ cfgdrift corpus fetch --workspace corpus_run
$ cfgdrift corpus annotate --workspace corpus_run --annotator annotator-a --batch batch_a.json
$ cfgdrift corpus kappa --workspace corpus_run --json
```

### LLM business-impact narratives (v0.8.0)

`cfgdrift explain` converts a drift into a natural-language story of its business impact using a deterministic template engine with a 27-entry key-semantics dictionary, and optionally an OpenAI-compatible LLM backend implemented with `urllib` only (zero new dependencies). An `EvidenceValidator` grounds every claim in the actual diff items, falling back to the deterministic template whenever evidence cannot be verified — preventing hallucination.

### Zero-noise contract

A distinguishing design guarantee: when a change is legitimate and no constraint is violated, the output is byte-for-byte identical to the previous run. This makes cfgdrift safe to run in CI and regression pipelines without generating spurious churn.

## Implementation and architecture

cfgdrift is implemented with a dual-mode parser architecture. A C extension (`csrc/parser_*.c`, C99) accelerates JSON, TOML, and INI parsing, while pure-Python parsers (`core/pure_parsers.py`) provide identical output; the wheel set ships both a universal `py3-none-any` wheel and a `cp313-win_amd64` accelerated wheel, and `pip` selects the appropriate one automatically. A property-style test matrix guarantees key-for-key output equivalence between the two modes. The differ is a pure function that never writes state; persistence is owned by the calling layer (SQLite-backed storage). The CLI is built with click and exposes grouped subcommands (`init`, `scan`, `diff`, `baseline`, `compare`, `severity`, `ignore`, `constraint`, `alert`, `daemon`, `corpus`, `explain`, `report`, `serve`).

Dependencies are deliberately minimal: `click`, `PyYAML`, and `tomli` (for Python < 3.11); the optional web extra adds only FastAPI and uvicorn. Performance is a first-class concern: a targeted path-lookup constraint check evaluates 10,000 keys × 20 constraints in under 10 ms, without traversing the whole tree — enabled by directed lookup rather than full traversal.

## Quality

cfgdrift is validated by **950 passing tests, 6 skipped, 0 failing** at v0.8.0. CI runs a matrix over Python 3.8/3.10/3.11/3.13, each against both the C-accelerated and pure-Python backends. The zero-noise contract is itself enforced by tests that assert byte-identical regression output. Performance targets (10k keys × 20 constraints < 10 ms) are covered by dedicated benchmarks in the test suite.

## Benchmark corpus and annotation reliability

To quantify how well drift detection agrees with human judgment — and to give researchers a reproducible starting point — we built a corpus of 112 real configuration-change instances drawn from the git history (since 2024-01) of 7 high-activity GitHub repositories (Table 1). The 277 diff items span all change classes and severity grades (Table 2). The full pipeline — fetch → export → annotate → kappa — is CLI-driven and re-runnable in one command.

**Table 1. Corpus composition (112 instances, 7 repositories).**

| Repository | Instances | Formats |
|---|---|---|
| `docker/compose` | 17 | yaml (17) |
| `nginxinc/docker-nginx` | 10 | yaml (10) |
| `prometheus/alertmanager` | 17 | yaml (15) / json (2) |
| `containous/traefik` | 17 | yaml (15) / toml (2) |
| `hashicorp/terraform` | 17 | yaml (17) |
| `kubernetes/ingress-nginx` | 17 | yaml (17) |
| `helm/helm` | 17 | yaml (17) |
| **Total** | **112** | yaml 108 / json 2 / toml 2 |

**Table 2. Change-type and auto-severity distribution (277 diff items).**

| | modified | removed | added | Total |
|---|---|---|---|---|
| Change type | 227 | 13 | 37 | 277 |
| Auto severity | WARN 227 | CRITICAL 13 | INFO 37 | 277 |

All 112 instances were independently annotated by two annotators on a three-class ordinal scale (`severe` / `minor` / `normal`). Inter-rater reliability results are summarized in Table 3.

**Table 3. Dual-annotation agreement (Cohen's kappa, v0.8.0).**

| Metric | Pilot subset (n=35) | Full set (n=112) |
|---|---|---|
| Observed agreement po | 0.971 | 0.875 |
| Expected agreement pe | 0.692 | 0.749 |
| **Cohen's kappa κ** | **0.907** | **0.502** |
| Weighted κ (linear) | — | 0.537 |
| Weighted κ (quadratic) | — | 0.585 |
| Fully agreeing instances | 34 / 35 | 98 / 112 |

The full-set κ = 0.502 must be read together with po = 0.875: the discrepancy stems from extreme class imbalance (84% `normal`), which inflates expected agreement (pe = 0.749) and suppresses κ. All 14 disagreements are confined to adjacent categories, and every one is traceable to a systematic guideline boundary rather than annotator noise. Sensitivity analysis confirms this: excluding the single traefik fixture-pinning series (11 instances) raises κ to 0.676, and assuming a unified rubric for the 8 pinning disagreements raises κ to 0.822 without dropping any data. The artifact is fully reproducible — `instances.jsonl` (112 rows) and `annotations.jsonl` (224 rows) are committed in `corpus_run/`, and `cfgdrift corpus kappa --workspace corpus_run --json` recomputes every figure.

## Availability

- **Repository:** https://github.com/Dongzhiyu0402/code
- **Version:** v0.8.0 (GitHub release; universal and `cp313-win_amd64` wheels)
- **Installation:** `pip install cfgdrift` (optional: `pip install "cfgdrift[web]"` for the dashboard, `"cfgdrift[dev]"` for development)
- **Language/Environment:** Python 3.8+ core; optional C99 extension; PyYAML for YAML support
- **License:** MIT (declared in `pyproject.toml`; a `LICENSE` file must be added to the repository before submission)
- **Documentation:** README, `docs/system_design_v080.md`, and the annotation-reliability report `docs/paper_annotations_material.md`

## Acknowledgements

[Funding sources and contributors — placeholder; e.g., this work was supported by …]

---

## 作者修改提示（中文，仅供作者，勿随论文投稿）

以下为投稿前必须处理的占位符与事项：

1. **作者/单位占位符**：`[Author Name 1]` 等需替换为真实姓名、邮箱与单位；JOSS 要求通讯作者姓名（至少一位）。
2. **致谢占位符**：`[Funding sources …]` 需替换或删除。
3. **LICENSE 文件缺失（重要）**：`pyproject.toml` 声明 MIT，但仓库根目录无 `LICENSE` 文件；JOSS 投稿前**必须**补充 MIT LICENSE 文件，否则会被期刊要求驳回。建议在正文 Availability 中保留该提醒直至文件补齐后删除该句。
4. **可替换/待更新数据**：
   - 测试数 `950 passed / 6 skipped` 为 v0.8.0 快照；若 v0.8.0 后改动代码，请更新。
   - 语料数据（112 实例 / 277 items / kappa 各值）来自 `corpus_run/`，全部可用 `cfgdrift corpus kappa --workspace corpus_run --json` 复算；如需重新标注或扩充语料（论文建议扩展至 300+ 并分层采样），数据需更新。
   - 性能基准（10k 键 × 20 约束 < 10 ms）来自测试套件，投稿前可补充独立基准脚本的复现说明。
5. **仓库 URL 谨慎**：正文仓库地址 `github.com/Dongzhiyu0402/code` 为作者提供；若实际发布仓库不同，请统一替换正文与 Availability 中的链接。
6. **可选的英文润色**：正文按 JOSS 惯例为英文；如需我协助润色或中英双语版本可再提出。
