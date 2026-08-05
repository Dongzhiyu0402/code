# cfgdrift v0.8.0 增量 PRD（基于 v0.7.0 基线）

> 文档性质：增量 PRD，仅描述四项变更（C-C5 / C-13 / D10 补全 / 方向 A）。
> 基线：cfgdrift v0.7.0（已发布，840 passed / 6 skipped；Python 3.8+；Python + C 双后端）。
> 产出方：产品经理 Alice。转交：架构师。

---

## 0. 变更概述

### 0.1 一句话目标
在 v0.7.0 语料与约束推理基础上补全「标注可靠性度量（kappa）、约束-严重度联动、compare 约束闭环、业务影响叙事」四块，形成可写论文的完整闭环。

### 0.2 用户故事（6 条）
1. 作为论文作者，我希望两名标注人**独立**标注同一批实例，以便用 Cohen's kappa 证明标注质量与可靠性（C-C5）。
2. 作为研究者，我希望随时查看 corpus 标注进度（已标/未标/双人完成/一致率），以便安排补标任务。
3. 作为运维工程师，我希望在 severity.yaml 中按 `constraint_id` 声明严重度，以便关键约束（如 `http_port_range`）一旦违反直接升级 CRITICAL。
4. 作为平台工程师，我希望 `compare` 多环境对比时同时跑约束检查，以便一次命令同时发现环境间漂移与环境内约束违反。
5. 作为安全负责人，我希望对检出的漂移得到「业务影响叙事 + 证据链」，以便非专家也能判断变更影响，且每个论断可溯源、不编造。
6. 作为离线环境用户，我希望无 LLM key 时 `explain` 仍可用（确定性模板叙事），以便沙箱/内网环境也能演示。

### 0.3 学术定位
- **C-C5**：论文「标注质量」必需——双人标注 + kappa 是标注可靠性的量化证据。
- **C-13 / D10 补全**：约束推理（方向 B）与 compare 的闭环补全，消除「约束检查只存在于 scan/diff、不出现在 compare」的断层。
- **方向 A**：论文体验亮点——LLM 业务影响叙事，**必须绑定证据链防幻觉**（叙事事实全部来自确定性引擎，LLM 只做语言组织）。

---

## 1. 功能一：corpus 双人标注 + kappa（C-C5，P0）

### 1.1 需求池

| 编号 | 优先级 | 需求 | 说明 |
|---|---|---|---|
| C5-P0-1 | P0 | `corpus annotate` 交互式标注子命令 | 遍历未标注实例，终端展示 diff 摘要（变更项、严重度、约束违反），输入标注标签与标注人标识；支持 `--annotator NAME`、`--skip-annotated`、`[s]` 跳过、`[q]` 保存退出 |
| C5-P0-2 | P0 | 标注独立存储 `annotations.jsonl`，export 时合并进 labels | **避免 instances.jsonl 重导覆盖**；annotate 只写独立文件；`corpus export` 时把 annotation/annotator 合并写入 instances.jsonl 的 `labels` 字段；validate 对合并结果仍通过 |
| C5-P0-3 | P0 | `corpus kappa` 计算 Cohen's kappa | 对「双人标注完成」的实例计算一致性；输出 kappa 值、一致率、样本数；标注字段口径见待确认 Q2（默认 3 分类序数：`severe`/`minor`/`normal`） |
| C5-P0-4 | P0 | `corpus stats` 标注进度统计 | 输出实例总数、未标注、单标注（按标注人拆分）、双人完成、一致率、kappa 可计算数 |
| C5-P1-1 | P1 | 批量标注文件导入 | `corpus annotate --batch labels.yaml/json`，标注表 `{instance_id: {annotation, annotator, note?}}` |
| C5-P1-2 | P1 | 加权 kappa 与混淆矩阵 | `corpus kappa --weighted linear\|quadratic\|none`；输出混淆矩阵 |

**标注字段设计**（写入 `labels`）：
- `labels.annotation`：3 分类序数标签，枚举 `severe`（漂移缺陷-业务影响严重）/ `minor`（漂移缺陷-一般）/ `normal`（正常变更，不构成缺陷）。
- `labels.annotator`：标注人标识字符串（如 `annotator1` / `alice`）。
- 存储记录建议追加 `annotated_at` 时间戳（非必须，P1 可加）。

**数据结构建议**（`annotations.jsonl`）：
```json
{"instance_id": "docker-compose-7e20f3b-0", "annotator": "alice", "annotation": "minor", "annotated_at": "2026-08-04T12:00:00Z"}
```

### 1.2 验收标准
1. 对 30 个未标注实例以 `annotator1` 标注、再以 `annotator2` 标注同一批后，`corpus kappa` 输出 Cohen's kappa 值（-1~1，含样本数 n=30），`corpus stats` 显示双人完成=30。
2. annotate 结果仅写入独立 `annotations.jsonl`；执行 `corpus export` 后，instances.jsonl 对应实例 `labels.annotation` / `labels.annotator` 非 null；**重复 export 不丢失标注**（重导覆盖场景验证通过）。
3. 交互式 annotate 单实例可完成标注并保存退出（`[s]` 跳过不写、`[q]` 退出已标内容保留）。

### 1.3 UI 线框（ASCII）

```
$ cfgdrift corpus annotate --annotator alice --workspace .corpus

corpus annotate (C-C5) — 待标注: 112, 已完成: 0
[实例 1/112] docker-compose-7e20f3b-0
  repo: docker/compose      file: .github/workflows/scorecards.yml
  diff 项: 3   added:1  modified:2  removed:0
  最高严重度: WARN   约束违反: 1 (http_port_range)

  变更摘要:
    [MODIFIED] jobs.analysis.permissions.contents: read → none
    [ADDED]    jobs.analysis.steps[1].with.publish_results: true
  ------------------------------------------------------------------
  该实例是否构成漂移缺陷？
  [1] severe   [2] minor   [3] normal   [s] 跳过   [q] 保存并退出
  > 2
  已保存: annotation=minor, annotator=alice → annotations.jsonl
  进度: 1/112 (双人完成: 0)

$ cfgdrift corpus stats
  instances          : 112
  未标注             : 40
  单标注             : 42 (annotator1: 42, annotator2: 0)
  双人完成           : 30
  标注一致率         : 86.7% (26/30)

$ cfgdrift corpus kappa
  Cohen's kappa = 0.82 (annotator1 vs annotator2, n=30)
  加权 kappa(linear) = 0.84
  混淆矩阵:
          ann1\ann2   severe  minor  normal
          severe        18      2       0
          minor          1      6       1
          normal         0      1       1
```

---

## 2. 功能二：severity 引用 constraint_id（C-13，P0）

### 2.1 需求池

| 编号 | 优先级 | 需求 | 说明 |
|---|---|---|---|
| C13-P0-1 | P0 | `SeverityRule` 新增 `constraint_id` 匹配条件 | severity.yaml 规则可声明 `constraint_id: http_port_range`（支持单个或列表），与既有 `change_type`/`key_pattern`/`value_pattern`/`file_pattern` 并列；`SeverityRule.from_dict` / `make_rule` / `severity` 命令 add 均支持 |
| C13-P0-2 | P0 | severity 决策管线扩展：约束检查 → 含 constraint_id 规则覆盖 → 升级 | 在 diff/scan 流程中，规则匹配时若该规则带 `constraint_id`，仅当该项关联的约束违反命中该 constraint 才生效；覆盖后继续走既有升级制（max_severity、告警阈值不变） |
| C13-P0-3 | P0 | 向后兼容：未配置 constraint_id 规则时行为不变 | 现有 840 测试不回归；first-match-wins（文件顺序）语义保持 |
| C13-P1-1 | P1 | 规则冲突可诊断 | `severity list` 展示每条规则命中的 constraint_id；文档化 constraint_id 与 key_pattern 同时命中时的优先级（默认仍按文件顺序 first-match-wins） |

**severity.yaml 扩展示例**：
```yaml
version: 1
rules:
  - name: port-range-critical
    enabled: true
    severity: CRITICAL
    constraint_id: http_port_range      # 新增：约束违反联动
  - name: tls-critical
    enabled: true
    severity: CRITICAL
    key_pattern: '.*tls\.enabled'       # 既有：键模式
```

**联动顺序（架构师注意）**：约束检查（生成 ConstraintViolation）→ 逐项匹配 severity 规则（含 constraint_id 条件，复用 `violations_from_items` 的项→违反映射）→ 覆盖严重度 → 既有升级制（summary.max_severity / 告警阈值）。

### 2.2 验收标准
1. severity.yaml 增加 `constraint_id: http_port_range → severity: CRITICAL` 后，`scan`/`diff` 中违反该约束的项 severity 输出为 CRITICAL（覆盖内置默认严重度）。
2. 删除该规则后，同一配置输出与 v0.7.0 完全一致；无 constraint_id 规则的 severity.yaml 加载、校验、CRUD 行为不变。
3. 联动链路可观测：约束违反 → severity 覆盖 → max_severity/告警升级全链路生效（有违反时 max_severity 取覆盖后值）。

### 2.3 UI 线框（ASCII）
```
# severity.yaml 配置后
$ cfgdrift diff --baseline prod ./config
[MODIFIED] services.web.ports[0]: "8080:80" → "9090:80"
  severity: CRITICAL     # 由约束 http_port_range 违反联动覆盖（原 WARN）
  constraint_violations: [http_port_range]
summary: 3 changes, max_severity=CRITICAL
```

---

## 3. 功能三：compare 跑约束（D10 补全，P0）

### 3.1 需求池

| 编号 | 优先级 | 需求 | 说明 |
|---|---|---|---|
| D10-P0-1 | P0 | `CompareEngine.compare_snapshots` 增加约束检查 | 对 `baseline_a.data` / `baseline_b.data` 分别执行 `ConstraintEngine.check_tree`；`CompareReport` 新增 `constraint_violations` 字段（按环境侧拆分，如 `{"env_a": [...], "env_b": [...]}`），`to_dict` 同步输出 |
| D10-P0-2 | P0 | `compare` CLI 透传约束选项 | `--constraints FILE` 自定义约束、`--no-builtin` 关闭内置约束库；复用 `ConstraintConfig.resolve` |
| D10-P0-3 | P0 | 无违反时输出与 v0.7.0 一致 | 向后兼容：未配置/无违反时，CompareReport 结构与 CLI 输出不变化（约束区块为空/不渲染） |
| D10-P1-1 | P1 | 违反定位到具体环境侧与 key_path | 输出中每条违反标明 env_a/env_b 侧及 key_path；`compare --json` 包含约束违反区块 |

**CompareReport 扩展**：
```python
@dataclass
class CompareReport:
    ...
    constraint_violations: Dict[str, List[dict]] = field(default_factory=dict)
    # {"env_a": [ConstraintViolation.to_dict()...], "env_b": [...]}
```

### 3.2 验收标准
1. `compare dev prod`（dev/prod 基线一方违反某内置约束）输出包含该违反及其所属环境侧（env_a/env_b），并标注 key_path。
2. `compare --no-builtin` 后内置约束违反消失；`compare --constraints my.yaml` 后自定义约束生效。
3. 两环境均无约束违反时，CLI 输出与 v0.7.0 完全一致（无新增区块、exit code 语义不变）。

### 3.3 UI 线框（ASCII）
```
$ cfgdrift compare dev prod
Comparing dev → prod
  baseline_a: dev (v3)   baseline_b: prod (v2)
  漂移: 5 changes
  --- 约束检查 (D10 补全) ---
  [env_b: prod] CRITICAL http_port_range
      key_path: services.web.ports[0]  value: "9090:80"
      message: 端口 9090 超出允许范围 8000-9000
  [env_a: dev] WARN docker_tag_pinned
      key_path: services.api.image  value: "nginx:latest"
exit 1
```

---

## 4. 功能四：方向 A LLM 业务影响叙事（P0 基础版）

### 4.1 需求池

| 编号 | 优先级 | 需求 | 说明 |
|---|---|---|---|
| A-P0-1 | P0 | `explain` 子命令 + `diff --explain` | 输入结构化漂移摘要（key_path/change_type/severity/constraint_violations）+ 键语义字典；输出每条漂移的结构化影响叙事 |
| A-P0-2 | P0 | 确定性模板叙事引擎（离线可用，P0 核心） | 内置键语义字典（port→监听端口、tls→传输安全、image→容器镜像…）+ 变更类型模板库，无 LLM key 时生成确定性解释 |
| A-P0-3 | P0 | 证据链防幻觉机制 | 输出结构化 JSON `{key, change_type, severity, impact, evidence[], source}`；`impact` 中的每个论断必须对应输入事实，`evidence` 只含输入中的键/值/约束，**不允许 LLM 编造不存在的键或值**；LLM 输出经证据校验，未通过则回退模板 |
| A-P0-4 | P0 | 可插拔 LLM 后端 + 降级 | OpenAI 兼容 REST API（有 key 启用增强叙事）；无 key / 超时 / HTTP 错误 / 证据校验失败 → 自动降级模板叙事并标记 `source: template` |
| A-P1-1 | P1 | 输出格式 | `--format text\|json\|markdown`；`--explain` 与 `diff` 输出联动 |
| A-P1-2 | P1 | 模板库可扩展 | 用户可提供自定义键语义字典 / 模板文件 |

**输入 → 输出契约**：
- 输入：`explain` 内部复用 diff 摘要（等价 `DiffSummary` 项列表），每项含 `key_path / change_type / old_value / new_value / severity / constraint_violations`；另加载内置键语义字典。
- 输出（每项）：
```json
{
  "key": "services.web.ports[0]",
  "change_type": "modified",
  "severity": "CRITICAL",
  "impact": "服务监听端口从 8080 改为 9090，且暴露端口超出约束 http_port_range(8000-9000) 允许范围，可能导致外部访问中断与安全组策略失配。",
  "evidence": ["services.web.ports[0]: 8080 -> 9090", "constraint=http_port_range 违反"],
  "source": "template"
}
```

**架构建议**：新增 `explain/` 模块（或 `core/explain.py`）——`ExplainEngine.generate(drift_items, schema_dict, llm_backend=None) -> List[NarrativeItem]`；模板引擎为默认实现，LLM 后端实现同一接口，输出经 `evidence` 校验器兜底。

### 4.2 验收标准
1. 对真实漂移执行 `explain`，每条输出含非空 `impact` 与 `evidence`，且 `evidence` 中每个引用键/值/约束均存在于输入漂移摘要（脚本校验通过，无编造）。
2. 无 LLM key（或离线）时 `explain` 可用并输出模板化叙事（`source: template`），命令不报错。
3. 配置 LLM key 后输出为 LLM 增强叙事（`source: llm`），仍满足证据链约束；模拟 LLM 返回编造键时自动降级模板。

### 4.3 UI 线框（ASCII）
```
$ cfgdrift explain --baseline prod ./config
漂移业务影响分析（cfgdrift v0.8.0 · 模板模式，未配置 LLM key）
================================================================
[1] services.web.ports[0]  (MODIFIED, CRITICAL)
    before: "8080:80"   after: "9090:80"
    impact: 服务监听端口从 8080 改为 9090，暴露端口超出约束
            http_port_range(8000-9000) 允许范围，可能导致外部访问
            中断、安全组策略失配。
    evidence:
      - key: services.web.ports[0]
      - value: 8080 -> 9090
      - constraint: http_port_range 违反
    source: template

[2] services.api.image  (MODIFIED, WARN)
    impact: 容器镜像由固定 tag 改为 latest，可能导致生产环境
            部署不可复现、升级不受控。
    evidence:
      - key: services.api.image
      - value: "nginx:1.25" -> "nginx:latest"
    source: template

$ cfgdrift explain --baseline prod ./config --format json
[{"key": "services.web.ports[0]", ..., "source": "template"}, ...]
```

---

## 5. 全局约束与兼容性
- 测试基线 840 passed / 6 skipped 不得回归（Python 3.8+ 双后端）。
- 四项功能均为增量：不修改既有 CLI 语义与 exit code 约定（0 无差异 / 1 有差异 / 2 错误）。
- 新增文件建议：`src/cfgdrift/corpus/annotations.py`（标注存储+kappa）、`src/cfgdrift/explain/`（叙事引擎+模板+LLM 后端）、扩展 `model.py`（SeverityRule.constraint_id、CompareReport.constraint_violations）。

---

## 6. 待确认问题（需团队/用户拍板）

| # | 问题 | 我的建议（默认） |
|---|---|---|
| Q1 | annotations 存储方案：独立 `annotations.jsonl` + export 合并，还是直接写回 instances.jsonl？ | **独立存储 + export 合并**（instances.jsonl 由 fetch/export 重导会覆盖，独立存储防丢失；validate 保证合并结果合法） |
| Q2 | kappa 计算的标注字段口径：`annotation` 3 分类（severe/minor/normal）还是按 severity 等级？序数是否用加权 kappa？ | 默认 `annotation` 3 分类，`kappa` 输出 Cohen's kappa；P1 提供 `--weighted linear/quadratic` 加权 kappa |
| Q3 | severity 的 constraint_id 与既有规则优先级：constraint_id 规则与 change_type/key_pattern 规则同时命中时，first-match-wins 顺序如何定？ | 保持文件顺序 first-match-wins；constraint_id 仅是额外匹配条件，不引入独立优先级层级；冲突行为文档化并在 `severity list` 展示 |
| Q4 | compare 约束违反的呈现位置：仅 CLI 文本区块，还是同时进 CompareReport 结构化字段与 --json？ | 结构化字段（CompareReport.constraint_violations）+ CLI 区块 + --json 同步输出（P0 做结构化，CLI 渲染） |
| Q5 | LLM 后端抽象与降级策略：仅 OpenAI 兼容 REST，还是也支持本地模型（ollama）？降级触发条件统一为哪些？ | P0 仅 OpenAI 兼容 REST；无 key / 超时 / HTTP 错误 / 证据校验失败 四类统一降级模板并标记 source；本地模型留 P1 接口扩展 |
| Q6 | explain 与 diff 的关系：独立命令为主，还是 `diff --explain` 为主？是否共享同一叙事管线？ | 独立 `explain` 为主（复用 diff 摘要），`diff --explain` 为同一管线的便捷入口；共享 ExplainEngine |
