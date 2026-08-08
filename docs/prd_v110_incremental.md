# cfgdrift v0.11.0 增量 PRD（基于 v0.10.0 基线）

> 文档性质：增量 PRD，描述方向 E「运维可观测性 + 约束治理闭环」的变更。
> 基线：cfgdrift v0.10.0（已发布；告警静默/ack、趋势图、报告对比 report --diff、kappa 导出）。v0.10.0 需求池遗留 P1×5、P2×2，本版经代码核实后重排。
> 产出方：产品经理 许清楚。转交：架构师。

---

## 1. 版本背景与目标

**一句话定位**：v0.11.0 把 v0.10.0 遗留需求池里的运维健康可观测（daemon 健康度、基线版本对比、严重度联动）与约束治理闭环（挖掘候选转正）补齐，同时把论文素材工具链做完整（explain 导出、corpus 分布）——「巡检健不健康看得见、配置变化查得清、挖掘的约束转得正、论文数据出得全」。

**设计原则**：
- 不做新能力，只做产品化补全；每项需求可独立验收、独立发布（沿用 v0.10.0 口径）。
- P0 共 4 项（延续 v0.10.0 体量），优先「值班/运维每天都会看」+「约束治理闭环」两条线。
- 既有测试用例不得回归；CLI exit code（0/1/2）与 Web API 响应包装（`{code,data,message}`）不变。
- 不新增第三方依赖：错误率聚合用 SQL/日志计数、版本对比复用 `CompareEngine.compare_snapshots`、候选转正复用 `constraint add` 写路径、联动是纯前端。

**侦察修正（v0.10.0 遗留需求池核实结论）**：
1. **P1-4 `report --diff --json` 已闭环**：`cli.py` 中 `report --diff A B --json PATH` 已实现（`_report_diff` 写脱敏 diff JSON，exit 0/1 正确，与 `--scan-id/--html/--csv` 互斥、`--json` 允许）→ **从本版需求池移除**。
2. **P2-2 `corpus stats --json` 参数已闭环**：`corpus stats --json` 已输出 `{code,data,message}`；但 stats 仅标注进度（instances/unannotated/single/double/agreement_rate/kappa_ready），**不含 PRD 原描述的严重度/类别分布字段** → 参数闭环，字段缺口降为 P1 小项（V11-P1-2）。
3. **P1-1 daemon 状态增强部分闭环**：`/api/daemon-status` 已有 interval（worker info 文件）、`last_scan`（app.py 挂载）、概览卡片已展示 interval+最近扫描（app.js）；**唯一缺口 = 错误率（最近 N 次扫描成功/失败比例）**，后端与前端均无 → 收窄为「daemon 健康增强」，作 P0。
4. **P1-2 基线版本对比未闭环**：Web 现有 `/api/compare`（环境对比）与 `/api/reports/compare`（扫描漂移项 diff），均非「同一基线版本间对比」；`baselines` 表已有 `name+version`（UNIQUE 约束），`show_baseline(name, version)` 可取指定版本，`CompareEngine.compare_snapshots` 可做树级对比 → 作 P0。
5. **P1-3 约束挖掘候选展示未闭环**：`rules/mining.py` 已有 `load_candidates`（读 `mined_candidates.yaml`），但 Web 无端点/视图 → 作 P0（升级为含转正入口）。
6. **P1-5 严重度分布联动未闭环**：严重度饼图已渲染（`#severitySvg`），无点击联动；时间线已有 `severity` 筛选参数（`/api/scans?severity=`）可复用 → 作 P0（低成本）。
7. **P2-1 explain 缓存 + markdown 导出未闭环**：`explain --format` 仅 `text/json`，无缓存 → 作 P1。

---

## 2. 用户故事

1. 作为值班工程师，我希望概览页一眼看到守护进程健康度（扫描周期、最近扫描时间、错误率），巡检挂掉或反复失败时能第一时间发现，而不是翻日志。
2. 作为运维排查员，基线更新/回滚后，我希望在 Web 上选同一基线的两个版本（v0/v1）对比，直接看到配置树级差异，确认这次变更影响面。
3. 作为平台治理工程师，`constraint mine` 挖出的候选约束，我希望在 Web 上看到支持度/置信度并一键转正为正式约束，形成「挖掘→确认→落地」治理闭环，而不是手抄 YAML。
4. 作为团队负责人，我希望点击严重度分布里的 CRITICAL 直接跳到已筛选的时间线，快速定位高危漂移，少点几下。
5. 作为研究员（P1），explain 的 LLM 叙事结果我希望按漂移指纹缓存并导出 markdown，作为审计与论文附录材料，且不重复烧 LLM 额度。

---

## 3. 需求池

### 3.1 P0（4 项，小而精、可独立验收）

| 编号 | 需求 | 需求描述 | 验收标准（完成=） | 涉及模块 |
|---|---|---|---|---|
| **V11-P0-1** | daemon 健康增强（错误率聚合 + 概览展示） | daemon worker 每周期在 info 文件（或新增 `daemon_cycles` 轻量表）滚动记录最近 20 次周期 `ok/fail`（周期内 scan 抛异常记为 fail，否则 ok）；`/api/daemon-status`（与 `/api/overview` 内嵌的 `daemon_status`）增加 `error_rate`（最近 20 次失败占比，0~1 浮点）与 `cycles_total`；概览守护进程卡片在既有 interval/最近扫描基础上新增「错误率」行，daemon 未运行时该字段置灰/省略不报错。 | ① daemon 运行中，概览卡片展示周期/最近扫描/错误率三项；② 错误率 = 最近 20 次周期失败占比，构造 fail 场景后数值正确；③ daemon 停止时错误率字段省略、卡片正常渲染不报错；④ `/api/daemon-status` 既有字段（running/pid/stale/info/error/last_scan）形状不变，仅增字段；⑤ 错误率与时间线扫描数互不干扰（扫描失败不产生 scan 行，以周期记录为准） | daemon/daemon.py、daemon/worker.py、storage/store.py、web/app.py、static/app.js |
| **V11-P0-2** | 基线版本对比 Web 化 | 基线管理视图新增「版本对比」入口：新增 `GET /api/baselines/{name}/versions`（返回该 name 全部版本 v0/v1/…，含 created_at/description）；前端选两个版本 → `GET /api/baselines/compare?name=&va=&vb=` → 复用 `CompareEngine.compare_snapshots`（树级语义 diff，无环境语义）→ 对比视图展示差异项（键路径/文件/行号/严重度/旧新值，含新增/消失/变化分组，与 `compare` CLI 同源）。 | ① 版本列表正确列出同一基线的全部版本，含缺失版本数校验；② 任意两版本可生成对比视图，分组/字段与 `compare` CLI 一致（同 `compare_snapshots` 纯函数）；③ 对比结果经 `SensitiveMasker.mask_payload` 脱敏；④ 版本不存在 → 404（`code:2` 包装）；⑤ 全同显示「两版本无差异」空态；⑥ 既有 `/api/baselines`、`/api/compare` 响应不变 | web/app.py、core/compare.py、storage/store.py、static/app.js |
| **V11-P0-3** | 约束挖掘候选展示与一键转正 | 新增 `GET /api/constraint-candidates`：读 `~/.cfgdrift/mined_candidates.yaml`（复用 `mining.load_candidates`，缺失/损坏返回空态 + 引导文案「运行 constraint mine 生成候选」）；约束视图新增「挖掘候选」卡片（kind/support/confidence/keys/示例值）；每条候选提供「转正」按钮 → 确认弹窗 → 复用 `constraint add` 写路径（candidate 转 rule JSON，原子写，失败不写坏文件）→ 成功标记该候选已转正（写入 `promoted` 状态，刷新后不可重复转正）；保留「复制命令」（`constraint add --rule` 文案）供 CLI 派。 | ① 有候选文件时渲染候选列表（字段完整）；② 无候选文件显示空态 + 挖掘引导，不报错；③ 一键转正写入正式约束文件，刷新后约束列表可见、候选标记已转正；④ 转正失败不写坏文件（原子写/回滚），错误信息可读；⑤ 已转正候选重复点击被禁用/提示；⑥ 「复制命令」可复制合法 `constraint add` 命令 | web/app.py、rules/mining.py、rules/constraints.py（或 cli constraint add 共用写函数）、static/app.js |
| **V11-P0-4** | 严重度分布联动 | 严重度分布饼图扇区（`#severitySvg`）可点击：点击某严重度 → 跳转时间线视图并预置 `severity` 筛选（复用 `/api/scans?severity=`，前端设 `timelineState.severity` 后刷新）；再次点击同扇区或切换「全部」取消筛选。 | ① 点击 CRITICAL 扇区 → 时间线视图筛选器显示 CRITICAL，列表仅含 CRITICAL 扫描；② 点击不同严重度可切换筛选；③ 取消筛选后恢复全部扫描；④ 无数据视图下点击无报错；⑤ 既有 `/api/overview` 严重度分布字段与饼图渲染不变 | static/app.js |

### 3.2 P1

| 编号 | 需求 | 需求描述 | 验收标准（完成=） | 涉及模块 |
|---|---|---|---|---|
| V11-P1-1 | explain 缓存与 markdown 导出（原 V10-P2-1） | `explain` 按漂移指纹缓存结果（`~/.cfgdrift/explain_cache.json`，键 = 指纹 + 模板/LLM 配置哈希，TTL 可配默认 24h，命中不调用 LLM）；新增 `--format markdown`：输出含 impact/evidence/source 的 md 文档（离线模板与 LLM 叙事均支持） | 相同指纹二次执行命中缓存不重复调用 LLM；TTL 到期后重新生成；markdown 合法且字段完整；既有 text/json 输出不变 | explain/、cli.py |
| V11-P1-2 | corpus stats 严重度/类别分布字段（原 V10-P2-2 字段缺口） | `corpus stats --json` 在既有标注进度字段基础上新增 `severity_distribution` 与 `change_type`（或类别）分布，图表友好 | `--json` 输出含分布字段，合法 JSON；既有字段（instances/unannotated/…/kappa_ready）不变 | corpus/annotations.py、cli.py |

### 3.3 P2

| 编号 | 需求 | 需求描述 | 验收标准（完成=） | 涉及模块 |
|---|---|---|---|---|
| V11-P2-1 | 约束候选批量操作 | 挖掘候选卡片支持按 kind 过滤、批量忽略（写入忽略清单或删除候选），已忽略可撤销 | 批量忽略后不再展示；撤销后恢复；过滤不落库 | web/app.py、rules/mining.py、static/app.js |
| V11-P2-2 | daemon 扫描超时提示 | 概览卡片当最近扫描时间距当前超过 k×interval（k 默认 3）时显示「巡检可能中断」警示 | 超时显示警示，未超时不显示；daemon 停止时沿用置灰逻辑 | static/app.js |

---

## 4. UI 设计稿（文本线框）

### 4.1 概览 · 守护进程卡片（P0-1）

```
概览
┌─ card：守护进程 ─────────────────────────────────────────┐
│  运行中 · pid=3821 · baseline=prod · interval=300s          │
│  最近守护扫描 #1293 · 08-05 09:00:12                        │
│  错误率 5.0% (1/20)    ← 新增行；daemon 未运行时整卡置灰省略 │
│   （最近 20 次周期：19 次成功 / 1 次失败）                    │
└──────────────────────────────────────────────────────────┘
```

### 4.2 基线管理 · 版本对比（P0-2）

```
基线管理 → 基线行「版本对比」按钮 → 对比面板
┌─ card：基线 prod 版本对比 ───────────────────────────────┐
│ 版本 A [v2 ▾ 08-03 18:00]  ↔  版本 B [v1 ▾ 07-28 10:00]  [对比] │
├────────────────────────────────────────────────────────┤
│ 新增（A 有 B 无，3 项）                                    │
│  [CRITICAL] services.web.ports[0]  "9090:80"  server.conf:12 │
│  [WARN]     api.timeout           "30s"       api.yml:7     │
├────────────────────────────────────────────────────────┤
│ 消失（B 有 A 无，1 项）                                    │
│  [INFO] debug.enabled "true"      app.yaml:3               │
├────────────────────────────────────────────────────────┤
│ 变化（严重度/值变，1 项）                                  │
│  [WARN→CRITICAL] logging.level  "info"→"debug"  app.yaml:5  │
└────────────────────────────────────────────────────────┘
（全同显示「两版本无差异」）
```

### 4.3 约束视图 · 挖掘候选卡片（P0-3）

```
约束 → 挖掘候选
┌─ card：挖掘候选（mined_candidates.yaml）────────────────┐
│ 空态：暂无候选 · 运行 `cfgdrift constraint mine` 生成     │
│ kind=regex    support=12  confidence=0.94  keys: api.*_key │
│   示例: api_key  →  [转正] [复制命令] [✓已转正]             │
│ kind=enum     support=8   confidence=0.82  keys: log.level  │
│   示例: log.level ∈ {info,warn,debug} → [转正] [复制命令]    │
│   （转正弹窗：确认写入 constraints.yaml？[取消][确认]）      │
└────────────────────────────────────────────────────────┘
```

### 4.4 严重度分布联动（P0-4）

```
严重度分布
┌─ card ──────────────────────────────────────────────┐
│  CRITICAL 12 ▓▓▓  ← 点击扇区 → 时间线视图           │
│  WARN      8   ▓▓                                    │
│  INFO      5   ▓    跳转后时间线筛选器 = CRITICAL     │
└─────────────────────────────────────────────────────┘
```

---

## 5. 全局约束与兼容性

- 测试基线（v0.10.0 全部 test_* 用例）不得回归（Python 3.8+ 双后端）；新增测试建议独立成 `test_daemon_health.py` / `test_baseline_compare_web.py` / `test_candidates.py` / `test_severity_link.py`，不并入既有文件。
- 既有 API 响应结构 `{code, data, message}`、CLI exit code（0 无差异 / 1 有差异 / 2 错误）不变；新增接口均为增量（`/api/baselines/{name}/versions`、`/api/baselines/compare`、`/api/constraint-candidates`）。
- 脱敏口径统一：版本对比复用 `SensitiveMasker.mask_payload`；候选转正/explain 导出不涉及敏感值（约束规则与标注类别），不 mask。
- 零噪音契约：错误率仅在 daemon 运行且有过周期记录时展示；版本列表/候选无数据渲染空态；严重度联动不改变饼图本身；`explain --format markdown` 不改变 text/json 输出。
- 不新增第三方依赖：错误率滚动计数用 info 文件/轻量表；版本对比复用现有 `CompareEngine`；候选转正复用 `constraint add` 写函数（抽公共写路径，CLI/Web 共用）；联动为纯前端。
- 建议新增文件：`web/candidates.py`（候选转正写函数，若抽公共层）或直接复用 `rules/mining.py`/`cli.py` 现有函数；无新模块。
- 版本三处同步 + QA 断言更新（`__init__.py` / `pyproject.toml` / `parser_core.c`，以及 `test_qa_v090.py`/`test_qa_v110.py` 版本契约断言），沿用 v0.10.0 教训（改源码必改断言）。

---

## 6. 待确认问题（需团队/用户拍板）

| # | 问题 | 我的建议（默认） |
|---|---|---|
| Q1 | 基线版本对比的语义与入口：对比同一基线两个版本的**原始快照树**（`compare_snapshots`，与 `compare` CLI 同源，含文件/行号）？入口放哪？ | 树级快照对比（复用 `compare_snapshots`，无环境语义）；入口放**基线管理视图**（版本列表 + 对比面板），报告视图的扫描对比（v0.10.0）不动 |
| Q2 | 候选转正的交互深度：Web 一键写入 constraints.yaml（写操作）vs 仅复制提升命令？写失败如何处理？ | 一键转正 + 确认弹窗 + 原子写（失败不写坏文件、可读错误）；保留「复制命令」作为旁路；转正走抽出的公共写函数（CLI `constraint add` 共用），避免两套写路径 |
| Q3 | daemon 错误率口径：用什么载体滚动记录最近 20 次周期 ok/fail？失败判定？未运行时展示策略？ | info 文件扩展（或轻量 `daemon_cycles` 表）滚动记录；周期内 scan 抛异常即 fail；`error_rate` 为 0~1 浮点，未运行/无记录时省略字段、前端置灰，不报错 |
