# cfgdrift v0.10.0 增量 PRD（基于 v0.9.0 基线）

> 文档性质：增量 PRD，描述方向 C/D「告警与运维闭环增强 + 数据导出」的变更。
> 基线：cfgdrift v0.9.0（已发布；894 test_* 用例；Python 3.8+；Python + C 双后端；时间线分页 + 告警 Web 闭环 + compare 约束对齐 + 报告 CSV 导出）。
> 产出方：产品经理 许清楚。转交：架构师。

---

## 1. 版本背景与目标

**一句话定位**：v0.10.0 把 v0.9.0 遗留需求池里的告警运维闭环（静默、趋势）与运维排查（报告对比）补齐，同时把论文素材（corpus kappa 导出）做成工具链闭环——「告警能安静、趋势能看见、漂移能对比、论文数据能导出」。

**设计原则**：
- 不做新能力，只做产品化补全；每项需求可独立验收、独立发布（沿用 v0.9.0 口径）。
- P0 共 4 项（参考 v0.9.0 体量），优先「值班/运维每天都会用」+「论文产出直接受益」两条线。
- 现有 894 用例不得回归；CLI exit code（0/1/2）与 Web API 响应包装（`{code,data,message}`）不变。
- 不新增第三方依赖：趋势图纯 SVG（原生 JS）、markdown/CSV 用 stdlib、静默用时间比较（无定时任务）。

**侦察修正（相对 v0.9.0 遗留需求池）**：
1. `alert_events` 表已有 `retried`/`retried_from` 列（v0.9.0 D4 迁移），**无 `acked` 列**；`AlertRule` 无 `mute_until` 字段。→ 静默/ack 需新增事件表列（幂等迁移）与 alerts.yaml 规则可选字段。
2. `alert_events` 表已有 `created_at`/`rule`/`status` 三索引，趋势图按天聚合有现成数据基础，无新索引压力。
3. v0.9.0 P2-3「corpus 图表/导出」实为两件事，本版**拆分**：kappa 结果导出升级为 P0（论文素材闭环，纯导出小功能）；`corpus stats --json` 留 P1。
4. `report` CLI 现有 `--json/--html/--csv` 三者互斥输出，**无 diff 形态**；`--diff` 走独立命令路径（`report --diff A B`），不影响既有三个输出。
5. `dispatcher._rule_matches` 只做 enabled/baseline/severity 过滤，静默拦截点应加在其前的规则筛选循环内（窗口内整条规则跳过）。

---

## 2. 用户故事

1. 作为值班工程师，发布窗口内已知问题反复触发告警，我希望在告警页**静默该规则 1h/24h**，期间 daemon 完全不打扰，窗口到期自动恢复，不用手改配置文件。
2. 作为值班工程师，深夜连续收到 failed 告警，我希望在告警页**一眼看到最近 14 天趋势**，判断是事件爆发还是通道故障，而不是翻事件表数行数。
3. 作为运维排查员，我怀疑某次变更引发漂移回归，希望**对比本次扫描与上次扫描**，直接看到新增/消失/严重度变化的漂移项，快速定位责任变更。
4. 作为平台工程师，我希望把两次扫描的 diff 结果**用脚本/CI 消费**（结构化输出、可判断 exit code），纳入巡检门禁。
5. 作为研究员，JOSS 论文需要标注一致性（kappa）结果，我希望**一键导出 markdown/CSV** 附到论文附录与审稿材料，而不是手抄终端输出。
6. 作为团队负责人（P1），我希望概览页看到 daemon 健康度（最近扫描时间、周期、错误率），判断巡检是否在正常运行。

---

## 3. 需求池

### 3.1 P0（4 项，小而精、可独立验收）

| 编号 | 需求 | 需求描述 | 验收标准（完成=） | 涉及模块 |
|---|---|---|---|---|
| **V10-P0-1** | 告警静默（规则级 mute_until + 事件级 ack） | `AlertRule` 新增可选 `mute_until`（ISO 时间，写入 alerts.yaml，缺省不静默）；`dispatcher` 规则筛选循环内按 `mute_until > now` 跳过整条规则（**不投递、不产生事件、不写 cooldown**），窗口到期靠时间比较自动恢复，无定时任务。事件级 ack：`alert_events` 表新增 `acked` 列（幂等迁移，参照 v0.9.0 retried 迁移）+ `acked_at`，Web 事件行「ack」按钮，仅展示语义。Web 告警视图规则行「静默 1h / 24h / 取消静默」；CLI 新增 `alert mute NAME --until ISO` / `alert unmute NAME`（与 Web 共用写路径）；概览卡片展示当前静默规则数。 | ① 窗口内 daemon 触发该规则不投递，事件表无新行；② 窗口到期或 Web/CLI 取消后，下次触发正常投递；③ Web 发起/取消静默后 `alerts.yaml` 实际写入 `mute_until`，刷新后保持；④ 事件 ack 后列表显示已确认标记，刷新后持久；⑤ CLI `alert mute/unmute` 与 Web 操作等价且可互操作；⑥ 概览显示静默规则数，无静默时显示 0 或省略；⑦ 静默不影响其它规则与非静默行为（零噪音） | alert/config.py、alert/models.py、alert/dispatcher.py、storage/store.py、web/app.py、static/app.js、cli.py |
| **V10-P0-2** | 告警历史趋势图 | 新增 `GET /api/alert-events/trend?days=14&rule=`：按天×status 聚合（`sent`/`failed` 两组，rule 为空=全部规则），返回 `{"days": [{"date","sent","failed"}], "total": N}`；告警视图事件表上方新增纯 SVG 趋势图（堆叠柱或双折线），数据与事件表同源（同一 store 聚合函数）。 | ① 有事件数据时渲染最近 14 天趋势，日期连续（无事件日期补 0）；② 支持「全部规则 / 单规则」切换，切换后图与表一致；③ 无数据显示空态而非报错；④ 图与事件表数据同源（同 store 层聚合）；⑤ 既有 `GET /api/alert-events` 响应不变 | web/app.py、static/app.js、storage/store.py |
| **V10-P0-3** | 报告对比（两次扫描 diff，CLI + Web） | CLI 新增 `report --diff SCAN_A SCAN_B`：按漂移项指纹分组输出三组——新增（A 有 B 无）、消失（B 有 A 无）、变化（双方有但严重度或值不同，展示严重度升降与值变化明细）；输出含键路径/文件/行号/严重度/旧新值。Web 报告视图新增「对比」入口：选两个 scan_id 生成 diff 视图（三组卡片，复用 CLI 同一 diff 函数）。diff 使用脱敏数据。 | ① CLI `report --diff` 输出三组且字段完整，全同输出「无差异」；② 有差异 exit code=1、无差异 exit code=0、参数错误/扫描不存在 exit code=2；③ Web 可选两个 scan 生成对比视图，分组与 CLI 完全一致；④ diff 中敏感值已脱敏（与 report 口径一致）；⑤ 既有 `report`（单次）输出与三个导出选项完全不变 | cli.py、core/report.py（或新建 core/comparediff.py）、web/app.py、static/app.js |
| **V10-P0-4** | corpus kappa 结果导出（markdown / CSV） | `corpus kappa` 新增 `--export PATH`（按扩展名自动选 .md/.csv）或 `--format markdown\|csv`：markdown 输出 kappa 汇总表（对比对、kappa、加权 kappa、n）+ 混淆矩阵表，可直接渲染为论文附录；CSV 输出逐 instance 对比行（instance_id、annotator_a、annotator_b、是否一致、A 类别、B 类别），供附录/审稿材料。 | ① `--export x.md` 生成可渲染 markdown，含 kappa 值与混淆矩阵表；② `--export x.csv` 生成逐 instance 对比行，UTF-8 BOM、Excel/WPS 可打开；③ 标注不足（<2 名标注人或无可比对）时给出错误提示并 exit 2；④ 不改变现有 `corpus kappa` 人类可读终端输出；⑤ 不改变 kappa 计算逻辑 | corpus/annotations.py、corpus/exporter.py、cli.py |

### 3.2 P1

| 编号 | 需求 | 需求描述 | 验收标准（完成=） | 涉及模块 |
|---|---|---|---|---|
| V10-P1-1 | daemon 状态增强 | `/api/daemon-status` 增加：扫描周期 interval（读 worker info 文件）、最近扫描时间（现有 `last_scan` 补格式化字段）、最近 20 次扫描成功/失败比例（复用 `list_scans_paged` 或专用聚合）；概览守护进程卡片展示上述字段，未运行时字段置灰/省略不报错 | 卡片展示周期/最近扫描/错误率；daemon 未运行时置灰不报错 | web/app.py、static/app.js、daemon/daemon.py、storage/store.py |
| V10-P1-2 | 基线版本对比 Web 化 | 基线管理视图展开版本列表（v0/v1/…），选择两个版本展示差异（复用 `compare_snapshots`，无环境语义） | 同一基线任意两版本可做差异视图，展示漂移项与严重度 | web/app.py、static/app.js |
| V10-P1-3 | 约束挖掘候选展示 | 新增 `GET /api/constraint-candidates`（读 `~/.cfgdrift/mined_candidates.yaml`，缺失返回空态）；约束视图增加「挖掘候选」卡片（kind/support/confidence/keys），候选提供「复制提升命令」（`constraint add --rule` 文案引导） | 有候选文件时 Web 渲染候选列表；无文件显示空态与挖掘引导；提升命令可复制 | web/app.py、static/app.js、rules/mining.py |
| V10-P1-4 | `report --diff --json` 结构化输出 | `report --diff` 增加 `--json`：输出脱敏后 diff JSON（新增/消失/变化三组，schema 与 `report --json` 同思路），exit code 仍 0/1/2 | 合法 JSON；exit code 语义与文本输出一致 | cli.py |
| V10-P1-5 | 严重度分布联动 | 严重度视图柱/饼可点击，跳转时间线视图并预置对应严重度筛选（复用 `/api/scans?severity=`） | 点击 CRITICAL 柱后时间线视图自动筛选 CRITICAL | static/app.js |

### 3.3 P2

| 编号 | 需求 | 需求描述 | 验收标准（完成=） | 涉及模块 |
|---|---|---|---|---|
| V10-P2-1 | explain 缓存与叙事导出 | `explain` 按漂移指纹缓存 LLM 结果（TTL 可配）；新增 `--format markdown` 导出 | 相同输入二次执行命中缓存不重复调用 LLM；markdown 导出含 impact/evidence/source | explain/、cli.py |
| V10-P2-2 | corpus stats 图表数据 | `corpus stats` 增加 `--json`（图表友好，含严重度/类别分布） | 合法 JSON，字段完整 | corpus/exporter.py、cli.py |

---

## 4. UI 设计稿（文本线框）

### 4.1 告警管理视图（P0-1 静默 / ack）

```
告警管理
┌─ card：告警规则（alerts.yaml）──────────────────────────┐
│ 名称     类型      阈值      基线     状态  静默       操作      │
│ drift-wx webhook [CRITICAL] all     启用  至 08-06 09:00 [静默1h][静默24h][取消]│
│ drift-em email   [WARN]    prod     启用  -          [静默1h][静默24h]      │
│   （静默中的规则状态列高亮为「静默中 · 剩余 3h」，悬浮显示 mute_until）       │
└────────────────────────────────────────────────────────┘
┌─ card：告警事件 ────────────────────────────────────────┐
│ ID   规则     严重度  状态    目标        错误  ack  操作 │
│ #48  drift-wx CRITICAL sent   https://…   -     -    [ack]│
│ #47  drift-wx CRITICAL failed https://…   502   -    [ack][重试]│
│ #46  drift-wx CRITICAL sent   https://…   -   ✓已确认 -   │
│   （ack 后行内显示 ✓已确认；静默期间产生的事件带「静默中」角标）   │
└────────────────────────────────────────────────────────┘
概览卡片：当前静默规则 1 条
```

### 4.2 告警历史趋势图（P0-2）

```
告警管理 → 事件表上方
┌─ card：近 14 天告警趋势 ────────────────────────────────┐
│ 规则 [全部规则 ▾]                                        │
│  sent ▓   failed ▒                                       │
│  14 │         ▓                                           │
│  10 │   ▓  ▓  ▓▓  ▒    ▓                                  │
│   5 │ ▓▒▓  ▓▓▒▒▓▒▓▓▓▓ ▓                                  │
│   0 └──────────────────────────────▶ 日期                │
│     07-23 07-25 07-27 07-29 07-31 08-02 08-04            │
│  （纯 SVG；无数据时显示空态「暂无告警事件」）                │
└────────────────────────────────────────────────────────┘
```

### 4.3 报告对比视图（P0-3）

```
报告浏览 → [对比] 按钮 → 对比面板
┌─ card：扫描对比 ────────────────────────────────────────┐
│ 扫描 A [#1293 ▾ 2026-08-05 09:00]  ↔  扫描 B [#1281 ▾ 2026-08-04 22:00]  [对比] │
├────────────────────────────────────────────────────────┤
│ 新增（A 有 B 无，3 项）                                    │
│  [CRITICAL] services.web.ports[0]  "9090:80"  server.conf:12 │
│  [WARN]     api.timeout           "30s"       api.yml:7     │
├────────────────────────────────────────────────────────┤
│ 消失（B 有 A 无，1 项）                                    │
│  [INFO] debug.enabled "true"      app.yaml:3               │
├────────────────────────────────────────────────────────┤
│ 变化（双方有但严重度/值变，1 项）                            │
│  [WARN→CRITICAL] logging.level  "info"→"debug"  app.yaml:5  │
└────────────────────────────────────────────────────────┘
（全同时：卡片显示「两次扫描无差异」）
```

### 4.4 corpus kappa 导出（P0-4，CLI 为主）

```
$ cfgdrift corpus kappa --export kappa_results.md
→ 生成 kappa_results.md：汇总表（对比对 | kappa | 加权 kappa | n）+ 混淆矩阵表
$ cfgdrift corpus kappa --export kappa_rows.csv
→ 生成 kappa_rows.csv：instance_id | annotator_a | annotator_b | 一致 | 类别A | 类别B（UTF-8 BOM）
```

---

## 5. 全局约束与兼容性

- 测试基线 894 test_* 用例不得回归（Python 3.8+ 双后端）；新增测试建议独立成 `test_alert_v100.py` / `test_web_v100.py` / `test_report_diff.py` / `test_kappa_export.py`，不并入既有文件。
- 既有 API 响应结构 `{code, data, message}`、CLI exit code（0 无差异 / 1 有差异 / 2 错误）不变；新增接口均为增量。
- 脱敏口径统一：所有新导出/新展示（diff、kappa CSV）复用 `SensitiveMasker.mask_payload`。
- P0-1 事件表新增 `acked`/`acked_at` 列采用幂等迁移（参照 v0.9.0 `retried`/`retried_from` 迁移模式）；`AlertRule` 新增 `mute_until` 为可选字段，旧 alerts.yaml 无需迁移即可加载。
- 零噪音契约：无趋势数据不渲染；静默仅影响被静默规则；`report --diff` 不改变既有 `report` 输出；`corpus kappa --export` 不改变终端输出。
- 不新增第三方依赖：趋势图纯 SVG（原生 JS）、markdown/CSV 用 stdlib `csv`/字符串拼接。
- 建议新增文件：`core/comparediff.py`（diff 函数，CLI 与 Web 共用）；无新模块。

---

## 6. 待确认问题（需团队/用户拍板）

| # | 问题 | 我的建议（默认） |
|---|---|---|
| Q1 | 静默的持久化载体与 CLI 范围：`mute_until` 写入 alerts.yaml 规则字段（vs 独立 mute 文件）？ack 状态持久化在哪？CLI 是否支持 mute 操作（vs 仅 Web）？ | 采纳 v0.9.0 Q3 拍板方向：规则级 `mute_until` 写入 alerts.yaml（可选字段）+ 事件级 ack 持久化到 `alert_events` 新列（仅展示语义）；CLI 支持 `alert mute/unmute` 与 Web 互操作（值班终端常用），ack 仅 Web |
| Q2 | 趋势图聚合口径：按天×status（sent/failed）聚合、最近 14 天默认是否够？是否需要自定义时间范围参数？ | 默认 14 天按天聚合 + 「全部/单规则」切换（规则下拉复用事件表规则列）；时间范围参数留 P1，本版不做，避免接口膨胀 |
| Q3 | 报告对比的 diff 分组语义与 exit code：是否仅「新增/消失/变化」三组（变化含严重度与值）？有差异时 exit code 是否沿用 1？Web 入口位置？ | 三组为新增/消失/变化（值变化并入变化组明细，不单列第四组）；有差异 exit=1 沿用既有约定；Web 入口放报告视图「对比」按钮，扫描选择器复用时间线行的 select 机制 |
