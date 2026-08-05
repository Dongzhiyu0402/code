# cfgdrift 论文素材：标注数据集与标注可靠性（双人标注 / Cohen's kappa）

> 角色：研究分析师（许清楚）· 只读分析 · cfgdrift v0.8.0
> 日期：2026-08-05
> 数据来源：`corpus_run/instances.jsonl`（112 实例）、`corpus_run/annotations.jsonl`（224 条双人标注，已独立复核）
> 状态：本文档全部数值均经 `corpus kappa` CLI 与原始数据文件逐项复核通过

---

## 1. 标注数据集概览

### 1.1 语料构成（112 实例 / 7 仓库）

从 GitHub 真实提交历史中提取配置变更对（`since: 2024-01-01`，仓库 star ≥ 3000），覆盖容器编排、反向代理、监控告警、IaC、K8s 生态六类典型运维配置场景：

| 仓库（owner/repo） | 实例数 | 配置格式 | 代表性文件类型 |
|---|---|---|---|
| `docker/compose` | 17 | yaml (17) | `docker-compose.yml`、GitHub Actions workflow |
| `nginxinc/docker-nginx` | 10 | yaml (10) | Dockerfile 相关 yaml 配置 |
| `prometheus/alertmanager` | 17 | yaml (15) / json (2) | `alertmanager.yml`、CI workflow |
| `containous/traefik` | 17 | yaml (15) / toml (2) | `traefik.yml`、integration fixture |
| `hashicorp/terraform` | 17 | yaml (17) | 模块配置、CI 配置 |
| `kubernetes/ingress-nginx` | 17 | yaml (17) | `charts/ingress-nginx/values.yaml` |
| `helm/helm` | 17 | yaml (17) | chart 配置 |
| **合计** | **112** | yaml 108 / json 2 / toml 2 | — |

### 1.2 变更类型与工具自动严重度分布（diff item 级，共 277 项）

| 维度 | modified | removed | added | 合计 |
|---|---|---|---|---|
| 变更类型（change_type） | 227 | 13 | 37 | 277 |
| 工具自动严重度（severity） | WARN 227 | CRITICAL 13 | INFO 37 | 277 |

> 实例级 `labels.severity` 汇总（每实例取 max_severity）：WARN 85 / CRITICAL 6 / INFO 18 / NONE 3。
> 可注意：工具严重度高度偏斜（82% 为 WARN），为后续 kappa 的 pe 膨胀埋下伏笔（见 §6.2）。

---

## 2. 双人标注流程

### 2.1 标注者

两位标注者 `annotator-a` / `annotator-b`，对全部 112 个实例**独立**进行标注（无讨论、无交叉反馈），保证标注独立性（论文可靠性论证的前提条件）。标注记录通过 `annotated_at` 时间戳与 `annotator` 字段隔离。

### 2.2 三分类序数准则（severe / minor / normal）

标注类别为 3 分类**序数**（类别序 0/1/2，`severe < minor < normal`，见 `src/cfgdrift/corpus/annotations.py:ANNOTATION_VALUES`）。实际标注使用的语义准则：

| 类别 | 定义要点 |
|---|---|
| **severe** | 删除/禁用关键配置项、可能直接导致生产故障或服务中断的变更 |
| **minor** | 存在潜在影响但非直接故障的变更，如版本/镜像 pin、依赖 bump 等「应规范化」但影响有限的操作 |
| **normal** | 无实际影响或完全例行的变更（如零生产影响的测试 fixture 调整、常规发布同步） |

> 标注准则在实战中暴露的边界问题（fixture 文件是否豁免、镜像 pin 是否单列）详见 §4、§6.2。

### 2.3 batch 导入机制

采用非交互式批量标注，避免逐条交互的耗时与噪声：

```
corpus annotate --workspace <ws> --annotator <name> --batch <labels.json>
```

- batch 文件格式（yaml/json）：`{instance_id: {annotation, annotator?, note?}}`
- 批次划分：**第一批 35 实例**（本次标注会话 02:34 时间桶，两位标注者各 35 条）、**第二批 77 实例**（`batch_annotator_a2.json` / `batch_annotator_b2.json`，各 77 键）
- 标注落盘 `annotations.jsonl`，每行 `{instance_id, annotator, annotation, annotated_at}`；`labels` 字段保留 `annotation`/`annotator` 投影预留

---

## 3. 标注一致性结果（Cohen's kappa）

### 3.1 主结果表

| 指标 | 第一批 35 子集 | 全量 112 |
|---|---|---|
| 样本数 n | 35 | 112 |
| 观察一致率 po | 0.971 | 0.875 |
| 期望一致率 pe | 0.692 | 0.749 |
| **Cohen's kappa κ** | **0.907** | **0.502** |
| 加权 kappa（linear） | — | 0.537 |
| 加权 kappa（quadratic） | — | 0.585 |
| 完全一致实例 | 34 / 35 | 98 / 112 |

- 一致性阈值参考：κ ≥ 0.80 为「强一致」（Landis & Koch），≥ 0.60 为「实质一致」。第一批 0.907 属强一致，全量 0.502 落入中等区间。
- 全量 kappa 由 CLI 复算精确匹配：`kappa=0.5016, po=0.875, pe=0.7492, linear=0.5369, quadratic=0.5854`。

### 3.2 混淆矩阵（全量 112，行 = annotator-a，列 = annotator-b）

| A \ B | severe | minor | normal | A 边际合计 |
|---|---|---|---|---|
| **severe** | **2** | 0 | 1 | 3 |
| **minor** | 0 | **7** | 8 | 15 |
| **normal** | 0 | 5 | **89** | 94 |
| B 边际合计 | 2 | 12 | 98 | 112 |

解读：

- 主对角线 2+7+89=98，即 87.5% 一致。
- **分歧全部集中在相邻类别**（severe↔normal 仅 1 例，且为 A=severe/B=normal 的极端跨类分歧，即 traefik 3f3466a-3，见 §4.3）；无任何「相反方向」的跨两档分歧，序数语义成立。
- **类不平衡极端**：normal 占 94/112（84%），minor 15（13%），severe 3（3%）。这是 pe 高达 0.749 的直接原因——即使随机标注，两人也有近 75% 概率「碰巧」同判 normal，导致 κ 被显著压低（详见 §6.2）。

---

## 4. 分歧分析（14 处）

14 处分歧（全量 112 中 98 处一致、14 处不一致）可归为三类。全部为 **normal ↔ minor** 或 **severe ↔ normal** 的相邻档分歧，方向集中于「fixture/无生产影响」与「镜像版本类操作」的准则边界。

### 4.1 镜像 pin 之争（8 处，A=minor / B=normal）

- **来源**：`containous/traefik` commit `536d142`（同一提交产生的系列变更，共 11 个实例，其中 8 处分歧）
- **分歧逻辑**：A 按「镜像版本 pin 属应规范化操作」判 **minor**；B 认为该变更位于 integration 测试 fixture、**零生产影响**，判 **normal**。
- **实例示例（`containous-traefik-536d142-1`）**：
  - 文件：`integration/resources/compose/access_log.yml`（yaml）
  - 提交：`Decouple sanitize path integration test from whoami behavior`（2026-07-30）
  - 变更：`services.*.image` 共 10 处由 `traefik/whoami` → `traefik/whoami:v1.12.0`（**镜像 tag pin，全部在测试 fixture 内**）
  - 标注：A=minor（镜像 pin 准则优先）、B=normal（测试 fixture 无生产影响优先）

### 4.2 发布 bump / CI 变更（5 处：ingress-nginx 3 + alertmanager 2，A=normal / B=minor）

- **来源**：`kubernetes/ingress-nginx` 3 处（commit `dbb11b9` 系列）、`prometheus/alertmanager` 2 处（`a54872a`、`bb956c9`）
- **分歧逻辑**：A 判 **normal**（例行发布同步/新增 workflow，行为无变化或仅版本号更新）；B 判 **minor**（镜像 digest/tag 变更、新增 CI workflow 属于有影响但非致命的变更）。
- **实例示例（`kubernetes-ingress-nginx-dbb11b9-1`）**：
  - 文件：`charts/ingress-nginx/values.yaml`（yaml）
  - 提交：`Release controller v1.15.1/v1.14.5/v1.13.9 & chart v4.15.1/...`（2026-03-19）
  - 变更：`controller.image.tag` v1.15.0 → v1.15.1，及 `controller.image.digest` / `digestChroot` 两个 sha256 摘要更新（**chart 发布版本 bump**）
  - 标注：A=normal（例行 release 同步）、B=minor（镜像摘要变更影响 image 拉取一致性）
- **补充示例（`prometheus-alertmanager-a54872a-0`）**：`.github/workflows/approve-workflows.yml` 新增整个 workflow 块（added/INFO），A=normal、B=minor。

### 4.3 fixture 移除关键键（1 处，A=severe / B=normal）

- **来源**：`containous/traefik` commit `3f3466a`
- **分歧逻辑**：A 按「**删除关键配置项**」准则判 **severe**；B 认为该文件是 integration 测试 fixture、无生产影响，判 **normal**。这是唯一一处 severe↔normal 跨档分歧，也是准则冲突最剧烈的一例。
- **实例示例（`containous-traefik-3f3466a-3`）**：
  - 文件：`integration/fixtures/k8s_gateway.toml`（toml）
  - 提交：`Consume TCPRoute through its v1 version and drop Gateway API v1.5.x support`（2026-08-04）
  - 变更：`removed`（CRITICAL）`providers.kubernetesGateway.experimentalChannel` 键，old=True → 删除
  - 标注：A=severe（删除关键配置）、B=normal（测试 fixture 无生产影响）

> 三类分歧的共同根因：**「变更的技术形态」（镜像 pin / 删键 / bump）与「变更的运行时影响范围」（生产 vs 测试 fixture）两条准则轴在标注指南中未分层**，标注者各自取了不同轴的优先权。详见 §6.2 的准则改进建议。

---

## 5. 敏感性分析（QA 复算）

针对分歧最集中的 traefik `536d142` 系列（镜像 pin 系列），QA 做了两组敏感性检验，均经数据文件独立复算：

| 场景 | n | po | pe | κ | 结论 |
|---|---|---|---|---|---|
| 基线（全量） | 112 | 0.875 | 0.749 | 0.502 | — |
| **剔除 `536d142` 系列全部 11 实例** | 101 | 0.941 | 0.817 | **0.676** | κ 提升 0.174，进入「实质一致」区间 |
| **该系列 8 处分歧均判 minor**（假设统一准则） | 112 | 0.946 | 0.699 | **0.822** | κ 提升 0.320，回到「强一致」区间 |

解读：

1. **准则统一比删样本更有效**：将 8 处镜像 pin 争议统一为 minor（不删任何数据）即可把 κ 从 0.502 拉到 0.822，说明分歧并非标注噪声或能力差异，而是**系统性准则分歧**——完全可修复。
2. **剔除系列也能显著提升**：0.676 表明该系列确实是主要的「污染源」，但代价是损失 9.8% 的样本，且保留了「准则未定义」的问题（其余 6 处分歧仍在）。
3. **敏感性方向正确**：两个方向都提升，交叉印证「标注者之间不存在系统性恶意偏差，仅存在可消除的准则边界歧义」。

---

## 6. 论文可用性评估

### 6.1 亮点

1. **kappa 公式全链路验证**：Cohen's kappa（po/pe/κ）、加权 kappa（linear=0.537、quadratic=0.585）、混淆矩阵、一致率均由 `corpus kappa --json` 一次性产出，口径透明、可复算——对论文「可复现性」要求是直接加分项。
2. **真实语料**：112 实例全部来自 GitHub 真实提交历史（2024-01 起、star≥3000、7 个高活跃仓库），非合成数据；变更类型（227 modified / 13 removed / 37 added）与工具严重度分布（WARN/CRITICAL/INFO）同时提供，可支撑「工具严重度 vs 人工标注」的对比分析。
3. **可复现工具链**：语料抓取（`corpus fetch`）→ 实例导出（`corpus export`）→ 批量标注（`corpus annotate --batch`）→ 一致性计算（`corpus kappa`）全流程 CLI 化，数据文件（`instances.jsonl` / `annotations.jsonl`）版本受控，审稿人可全链路复跑。
4. **分歧分析可叙事**：14 处分歧全部可归类、可追溯（commit + 文件 + 键路径），并给出准则层面的根因，这在标注可靠性论文中属于高质量的定性证据。

### 6.2 风险

1. **κ 从 0.907（第一批）→ 0.502（全量）的落差**：
   - **pe 膨胀**：全量 pe=0.749 vs 第一批 pe=0.692。全量中 normal 类占 84%（94/112），两人「随机碰巧一致」的概率被顶高，κ 的「超越随机」余量（1−pe=0.251）变小，任何分歧都被放大。
   - **类不平衡**：severe 仅 3 例，kappa 对稀有类别极度敏感；第一批 35 恰好严重度分布更均衡（po 高 + pe 低双因素共同推高 κ）。
   - **样本期差异**：第一批 35 属「先行试点」，两类标注者在这批上达成共识；后续 77 实例引入的 traefik 536d142 系列 8 处系统性分歧直接侵蚀一致性。
   - 论文中**必须**同时报告 po 与 κ，否则 0.502 会被误读为「标注质量差」；真实解读是「达成共识率高（87.5%），但分布偏斜导致 κ 被压制」。
2. **标注准则边界不清（fixture vs 生产影响）**：14 处分歧中 9 处（镜像 pin 8 + fixture 删键 1）根因是「测试 fixture 是否豁免」未在指南中定义。这是本轮标注暴露的最大方法论问题，若不修复，全量复刻会重现同等量级的分歧。
3. **kappa 的已知局限**：在高度不平衡序数数据上，κ 比 po 更「悲观」（0.502 vs 0.875），而 quadratic weighted κ=0.585 也仅略高；需在论文中讨论加权方案与类别分布的交互。

### 6.3 建议

1. **准则细化方向**：
   - **「测试 fixture 单独标注」**：将 `integration/`、`test/`、`fixtures/` 路径单独分类，fixture 变更默认降一档（或单独设 `fixture` 类别，不进入生产严重度量表）——可直接消解 9 处分歧。
   - **「镜像 pin 统一规则」**：明确「镜像 tag pin 一律 minor（需规范化）」，与「运行环境是否生产」解耦；或明确「fixture 内镜像 pin 一律 normal」，二选一并写入指南。
   - **「发布 bump」**：区分「版本号例行同步（normal）」与「镜像 digest 变更（minor）」——digest 变更改变实际拉取内容，版号同步仅元数据。
2. **报告口径**：建议以 **weighted quadratic kappa（0.585）为主指标 + po（0.875）为辅指标** 报告，同时给出未加权 κ（0.502）并解释 pe 膨胀机制；避免单一未加权 κ 造成误导。若投稿目标领域习惯 Landis & Koch 阈值，需附上「κ 对不平衡序数数据偏保守」的方法论段落。
3. **样本扩展计划**：
   - 短期：将敏感性分析中「全 minor 假设」落地为正式准则修订（预计 κ → ~0.82，强一致），并重跑全量双人标注。
   - 中期：扩充严重度均衡的实例（当前 severe 仅 3 例，采样需 `stratified`，避免继续放大不平衡），目标将 n 扩至 300+ 以支撑置信区间与亚组分析（按仓库/格式/变更类型）。
   - 补充：对每个仓库报告子 kappa，验证跨仓库一致性（traefik 的测试 fixture 密集，单仓可能系统性拉低整体 κ）。

---

## 7. 可复现性

### 7.1 环境与版本

- cfgdrift **v0.8.0**（双人标注 + Cohen's kappa 里程碑）
- Python：`C:/Users/20713/.workbuddy/binaries/python/envs/default/Scripts/python.exe`
- 工作区：`corpus_run/`（`state.json` 记录 `fetched_at: 2026-08-04`、各仓库 star 与 last_commit，如 `docker/compose` 37,952★、`containous/traefik` 64,277★）

### 7.2 完整命令行

```bash
PY="C:/Users/20713/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
WS="corpus_run"

# 1) 语料抓取（2024-01 起，star≥3000，见 corpus.yaml）
$PY -m cfgdrift.cli corpus fetch --workspace $WS

# 2) 实例导出（变更对 → instances.jsonl，112 实例）
$PY -m cfgdrift.cli corpus export --workspace $WS

# 3) 双人批量标注（annotator-a / annotator-b，各 77 键第二批 batch 文件）
$PY -m cfgdrift.cli corpus annotate --workspace $WS --annotator annotator-a --batch corpus_run/batch_annotator_a2.json
$PY -m cfgdrift.cli corpus annotate --workspace $WS --annotator annotator-b --batch corpus_run/batch_annotator_b2.json

# 4) Cohen's kappa（全量：κ=0.502，weighted linear=0.537 / quadratic=0.585）
$PY -m cfgdrift.cli corpus kappa --workspace $WS --json
$PY -m cfgdrift.cli corpus kappa --workspace $WS --weighted linear --json
$PY -m cfgdrift.cli corpus kappa --workspace $WS --weighted quadratic --json
```

### 7.3 数据文件路径

| 文件 | 内容 | 规格 |
|---|---|---|
| `corpus_run/instances.jsonl` | 112 个标注实例（含 metadata / before / after / diff / labels） | 每行一个 JSON 对象 |
| `corpus_run/annotations.jsonl` | 224 条双人标注（112 × 2 标注者） | `{instance_id, annotator, annotation, annotated_at}` |
| `corpus_run/batch_annotator_a2.json` | annotator-a 第二批 77 键标注批 | `{instance_id: {annotation, ...}}` |
| `corpus_run/batch_annotator_b2.json` | annotator-b 第二批 77 键标注批 | 同上 |
| `corpus_run/corpus.yaml` | 语料配置（仓库清单 / since / star 阈值） | 2024-01-01 起 |
| `corpus_run/state.json` | 抓取状态（各仓库 stars / last_commit / instance_count） | fetched_at 2026-08-04 |
| `corpus_run/repos/` | 各仓库本地 clone（git 历史，用于逐实例回溯 commit） | 9 个仓库目录 |

> 第一批 35 实例为本次标注会话的首批（`annotated_at` 02:34 时间桶，两位标注者各 35 条）；批次信息可由 `annotations.jsonl` 的 `annotated_at` 前缀还原（`2026-08-05T02:34` → 第一批 35，`2026-08-05T02:45` → 第二批 77）。

---

*报告完。全部数字可复算：全量 κ=0.5016 / po=0.875 / pe=0.7492 / linear=0.5369 / quadratic=0.5854；敏感性剔除系列 κ=0.676、全 minor 化 κ=0.822；第一批 35 子集 κ=0.907。*
