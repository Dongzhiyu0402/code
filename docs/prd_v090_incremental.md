# cfgdrift v0.9.0 增量 PRD（基于 v0.8.0 基线）

> 文档性质：增量 PRD，仅描述方向 C/D「产品功能完善」的变更。
> 基线：cfgdrift v0.8.0（已发布；883 个 test_* 用例；Python 3.8+；Python + C 双后端；9 个 Web 视图 + 3 通道告警 + corpus/约束/explain 全链路）。
> 产出方：产品经理 许清楚。转交：架构师。

---

## 1. 版本背景与目标

**一句话定位**：v0.9.0 把 v0.8.0 已具备的检测 / 告警 / 约束 / 报告能力做成「每日巡检真正可依赖的产品」——补齐 Web 仪表盘与 CLI 之间的功能断层，让高频操作（查历史扫描、管告警规则、环境对比、报告导出）在仪表盘内闭环。

**设计原则**：
- 不做新能力，只做产品化补全；每项需求可独立验收、独立发布。
- P0 共 4 项，参考 v0.8.0 体量；优先「每天都会用到」的操作路径。
- 现有 883 用例不得回归；CLI exit code 约定（0/1/2）与 Web API 响应包装（`{code,data,message}`）不变。

**侦察修正（相对任务描述）**：
1. alert CLI 实际只有 `add / list / remove / test`，**不存在 enable/disable**；`AlertConfig` 也无 `set_enabled`。→ 任务描述中"enable/disable"为误记，告警启停本身就是一个真实缺口（P0-2 覆盖）。
2. `htmlreport.py` 已在变更列表逐项渲染约束违反（`constraint_violations` 列），**HTML 报告约束区块已具备**，无需新增。
3. Web `/api/compare` 未传入 constraints（CLI `compare` 在 v0.8.0 D10 已支持），Web 与 CLI 行为不一致（P0-3 覆盖）。

---

## 2. 用户故事

1. 作为运维巡检员，daemon 每 60s 扫描一次，一天产生上千条扫描记录，我希望在时间线视图**搜索到某次扫描并翻页查看**，而不是只能看最近 50 条。
2. 作为值班工程师，收到 webhook 告警后怀疑通道配置变化，我希望在仪表盘上**一键「测试发送」**验证通道连通性，并直接**停用/启用**规则，不必切到终端。
3. 作为平台工程师，我在 Web 上对比 dev/prod 环境，希望约束违反与 CLI `compare` 显示一致（标出 env 侧与 key_path），避免「CLI 有、Web 没有」的困惑。
4. 作为团队负责人，我希望把某次关键扫描的漂移明细**导出 CSV** 附到周报/复盘，含严重度、键路径、约束违反，且敏感值已脱敏。
5. 作为告警负责人（P1），维护窗口期间我希望**静默**某条告警规则一段时间，避免反复打扰，窗口结束自动恢复。
6. 作为研究员（P1），约束挖掘产出候选规则后，我希望在约束视图**直接看到候选及其支持度/置信度**，确认后一键提升为正式约束。

---

## 3. 需求池

### 3.1 P0（4 项，小而精、可独立验收）

| 编号 | 需求 | 需求描述 | 验收标准（完成=） | 涉及模块 |
|---|---|---|---|---|
| **V9-P0-1** | 时间线视图增强：搜索 / 筛选 / 分页 | 新增 `GET /api/scans` 分页查询接口（支持 `q` 关键字模糊匹配 scan_id/基线名/mode、`severity` 最高严重度筛选、`mode` 筛选、`limit`/`offset`，含 `total`）；时间线视图改为分页表格 + 搜索框 + 严重度下拉 + 模式筛选。概览视图仍走 `/api/overview`（不受影响）。 | ① 库中 ≥100 条扫描时，时间线可分页浏览任意页，页码与总数正确；② 输入 `q`（如 `#123` 或基线名）过滤出匹配项；③ 选 CRITICAL 只显示最高严重度为 CRITICAL 的扫描；④ 分页/筛选状态在视图间切换后保留；⑤ 无匹配时给出空态提示而非报错 | web/app.py、storage/store.py、static/app.js |
| **V9-P0-2** | 告警规则 Web 操作闭环：启用 / 停用 / 测试发送 / 重试 | 新增 `AlertConfig.set_enabled` + CLI `alert enable/disable`（与 severity/constraint 的 enable 命令同风格）；Web 新增 `PUT /api/alerts/{name}/enabled`、`POST /api/alerts/{name}/test`、`POST /api/alert-events/{id}/retry`；告警视图规则表加「启用/停用」开关与「测试」按钮，事件表 failed 行加「重试」按钮。重试绕过 cooldown 直接投递，并写一条新事件（`retried=true`）。 | ① Web 切换规则启用状态后 `alerts.yaml` 实际变更，刷新后状态保持；② Web「测试」发送 `event=cfgdrift.test`，成功/失败在按钮旁即时反馈，不写告警事件表；③ failed 事件点「重试」后产生一条新事件（状态为 sent 或 failed，带 `retried=true`），原事件保留；④ CLI `alert enable/disable` 与 Web 操作等价且可互操作 | alert/config.py、cli.py、web/app.py、static/app.js |
| **V9-P0-3** | Web 环境对比对齐 CLI 约束检查 | `/api/compare` 加载内置 + 用户约束库（复用 `rules/constraints.resolve(home, [], builtin_enabled=True)` 与 `--constraints` 等价的自定义约束文件）并传入 `CompareEngine.compare(constraints=...)`；compare 结果卡片增加「约束违反」区块，按 env_a / env_b 分组渲染（约束 id、严重度、key_path、消息），item 级违反沿用现有 `constraint_violations` 列。 | ① 当对比环境一方存在内置约束违反时，Web 结果出现约束违反区块且与 CLI `compare` 输出一致（含环境侧与 key_path）；② 用户 constraints.yaml 中自定义约束同样生效；③ 两环境均无违反时页面与 v0.8.0 完全一致（无新区块）；④ 违反信息不改变对比的漂移统计与页面布局 | web/app.py、static/app.js |
| **V9-P0-4** | 报告导出 CSV | 新增 `report --csv PATH`（列：scan_id、严重度、键路径、变更类型、文件、行号、旧值、新值、规则、约束违反 id 列表）；数据经 `SensitiveMasker.mask_payload` 后导出，行内约束违反以 `;` 分隔；Web 报告视图新增「导出 CSV」按钮（走同一渲染函数）。 | ① `report --csv` 生成可被 Excel/WPS 直接打开的文件，UTF-8 BOM + 表头完整；② 敏感键值已脱敏（`已脱敏` 标记，与 HTML/JSON 口径一致）；③ 含约束违反的项其违反 id 出现在 CSV 对应单元格；④ Web 导出文件内容与 CLI 完全一致 | cli.py、web/app.py、static/app.js |

### 3.2 P1

| 编号 | 需求 | 需求描述 | 验收标准（完成=） | 涉及模块 |
|---|---|---|---|---|
| V9-P1-1 | 报告对比（两次扫描 diff） | `report --diff SCAN_A SCAN_B` 输出两次扫描的漂移项 diff（新增/消失/严重度升降）；Web 报告视图可选两个 scan 生成对比视图 | 对比结果含三组：A 有 B 无、B 有 A 无、双方有但严重度/值变化；全同则提示无差异 | cli.py、storage/store.py、web |
| V9-P1-2 | 约束视图挖掘候选展示 | 新增 `GET /api/constraint-candidates`（读 `~/.cfgdrift/mined_candidates.yaml`）；约束视图增加「挖掘候选」卡片，展示 kind/support/confidence/keys，候选提供「复制提升命令」按钮（文案引导 `constraint add --rule`） | 有候选文件时 Web 渲染候选列表；无文件时显示空态与挖掘引导；提升命令可复制 | web/app.py、static/app.js、rules/mining.py |
| V9-P1-3 | 告警静默机制（ack / mute） | 规则级 mute（`alerts.yaml` 规则新增可选 `mute_until` 或独立 `alert mute` 文件，指定窗口内 dispatcher 跳过该规则）+ 事件级 ack（事件标记 `acked`，仅展示层语义）；Web 告警视图提供「静默 1h/24h」「ack」按钮，概览展示当前静默规则数 | 静默窗口内 daemon 触发不投递、不产生事件；Web 可发起/取消静默；事件 ack 状态在列表可见 | alert/、daemon/、web |
| V9-P1-4 | 告警历史趋势图 | 新增 `GET /api/alert-events/trend`（按天×规则×status 聚合，取最近 14 天）；告警视图增加纯 SVG 趋势图（sentin/failed 两条线或堆叠柱） | 有事件数据时渲染趋势图；无数据显示空态；图与事件表数据同源 | web/app.py、static/app.js、storage/store.py |
| V9-P1-5 | 基线版本对比可视化 | 基线管理视图展开基线版本列表（v0/v1/…），选择两个版本展示其差异（复用 `compare_snapshots`，无环境语义） | 可对同一基线任意两版本做差异视图，展示漂移项与严重度 | web/app.py、static/app.js |
| V9-P1-6 | daemon 状态增强 | Web 概览守护进程卡片增加：最近扫描时间、扫描周期（interval）、目标路径、自启动状态、最近 N 次扫描的成功/失败比例 | 卡片展示上述字段；daemon 未运行时字段置灰/省略，不报错 | web/app.py、static/app.js、daemon/daemon.py |

### 3.3 P2

| 编号 | 需求 | 需求描述 | 验收标准（完成=） | 涉及模块 |
|---|---|---|---|---|
| V9-P2-1 | `diff --json` 结构化输出 | diff 增加 `--json`，直接输出掩码后的漂移项 JSON（与 `report --json` 同 schema） | `diff --json` 输出合法 JSON，exit code 仍为 0/1 | cli.py |
| V9-P2-2 | explain 结果缓存与叙事导出 | `explain` 按漂移指纹缓存 LLM 结果（TTL 可配）；新增 `--format markdown` 导出 | 相同输入二次执行命中缓存不重复调用 LLM；markdown 导出含 impact/evidence/source | explain/、cli.py |
| V9-P2-3 | corpus 数据产品化 | `corpus stats` 增加 `--json`（图表友好）；`corpus kappa` 增加 `--export markdown/csv` | 两种导出文件可打开且字段完整 | corpus/exporter.py、cli.py |
| V9-P2-4 | 严重度分布联动 | 严重度视图的柱/饼可点击，跳转时间线视图并预置对应严重度筛选 | 点击 CRITICAL 柱后时间线视图自动筛选 CRITICAL | static/app.js |

---

## 4. UI 设计稿（文本线框）

### 4.1 时间线视图（P0-1）

```
┌─ cfgdrift ─────────────────────────────────────────────┐
│ 概览 │ 时间线 │ 严重度分布 │ …        [搜索 #id/基线/模式] │
└────────────────────────────────────────────────────────┘
时间线
┌─ card ─────────────────────────────────────────────────┐
│ 搜索 [#123 | my-prod | daemon]  严重度[▾全部]  模式[▾全部] │
│  共 1,204 次扫描 · 显示 41–60                           │
├────────────────────────────────────────────────────────┤
│ #1293  2026-08-05 09:00  daemon  vs prod v12  [CRITICAL]│  total=3 added=0 removed=1 …
│ #1292  2026-08-05 08:00  daemon  vs prod v12  [WARN]    │  total=1 …
│   …（每行可点击 → 报告浏览）                              │
│      [上一页]  第 3 页 / 共 61 页   [下一页]              │
└────────────────────────────────────────────────────────┘
```

### 4.2 告警管理视图（P0-2）

```
告警管理
┌─ card：告警规则（alerts.yaml）──────────────────────────┐
│ 名称     类型      阈值      基线     状态   操作             │
│ drift-wx webhook [CRITICAL] all     启用  [停用][测试发送]  │
│ drift-em email   [WARN]    prod     停用  [启用][测试发送]  │
│                     …（测试发送成功 → 按钮旁绿字「已发送 cfgdrift.test」）│
└────────────────────────────────────────────────────────┘
┌─ card：告警事件 ────────────────────────────────────────┐
│ ID   规则     严重度  状态     目标          错误     操作   │
│ #48  drift-wx CRITICAL sent    https://…    -       -    │
│ #47  drift-wx CRITICAL failed  https://…    502     [重试]│
│          （重试后插入新行 #49，状态 sent/failed，标注「重试」） │
└────────────────────────────────────────────────────────┘
```

### 4.3 环境对比视图约束区块（P0-3）

```
环境对比  参考[prod v12 ▾]  对比[dev v8 ▾]  严重度[▾全部]  [对比]
prod v12 → dev v8
┌─ 约束违反 ──────────────────────────────────────────────┐
│ [env_a: prod] CRITICAL http_port_range                 │
│     key: services.web.ports[0]  value: "9090:80"       │
│     message: 端口 9090 超出允许范围 8000-9000            │
│ [env_b: dev]  WARN     docker_tag_pinned               │
│     key: services.api.image     value: "nginx:latest"  │
└────────────────────────────────────────────────────────┘
（无违反时该卡片不渲染）
```

### 4.4 报告视图导出（P0-4）

报告浏览 → 报告正文右上角新增 `[导出 HTML] [导出 CSV]` 两个按钮，CSV 经 Blob 下载 `report-{scan_id}.csv`，内容为 UTF-8 BOM + 表头 + 漂移项行（脱敏后）。

### 4.5 约束视图挖掘候选区（P1-2，示意）

```
约束
┌─ card：一致性约束（生效视角）…（现状不变）───────────────┐
┌─ card：挖掘候选（mined_candidates.yaml，status=pending）─┐
│ kind                 keys              support conf 操作 │
│ range                x.ports[0]        12      1.00 [复制提升命令]│
│ conditional_required y.require_z       9       0.90 [复制提升命令]│
│ （无候选时：空态文案 + 引导「运行 cfgdrift constraint mine」）│
└────────────────────────────────────────────────────────┘
```

---

## 5. 全局约束与兼容性

- 测试基线 883 test_* 用例不得回归（Python 3.8+ 双后端）。
- 既有 API 响应结构 `{code, data, message}`、CLI exit code（0 无差异 / 1 有差异 / 2 错误）不变；新增接口均为增量。
- 脱敏口径统一：所有新导出（CSV、重试事件、compare 约束区块）复用 `SensitiveMasker.mask_payload`。
- P0-1 建议在 store 层新增分页查询方法（如 `list_scans_paged`），不动现有 `list_scans(limit=50)` 以保回归。
- 新增文件建议：无新模块；扩展 `alert/config.py`（set_enabled）、`web/app.py`、`static/app.js`、`cli.py`。

---

## 6. 待确认问题（需团队/用户拍板）

| # | 问题 | 我的建议（默认） |
|---|---|---|
| Q1 | 告警「重试发送」的记录口径：绕过 cooldown 重投，是否写入新事件？是否需独立 `retried` 标记？ | 绕过 cooldown 投递并写一条新事件（`retried=true`），原事件保留——保证审计链完整、不重复计数 |
| Q2 | 时间线搜索字段范围：仅扫描元数据（scan_id/基线名/mode）模糊匹配，还是需要按漂移项 key_path 全文搜索？ | P0 只做扫描元数据搜索（高频、代价低）；key_path 全文检索留 P1 再评估索引方案 |
| Q3 | 告警静默（P1-3）的载体与粒度：规则级「静默窗口」+ 事件级 ack，还是仅规则级？静默期间 daemon 是否完全跳过（不产生事件）？ | 先做规则级 `mute_until` 窗口（daemon 跳过、不产生事件）+ 事件级 ack（仅展示语义）；窗口到期自动恢复，不引入定时清理任务 |
