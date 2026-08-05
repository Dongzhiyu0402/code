# cfgdrift — 语义级配置漂移检测系统

`cfgdrift` 是一个语义级（semantic-level）的配置漂移检测工具，区别于传统文件级（hash/字节比对）方案：它把 JSON、YAML、TOML、INI 配置文件解析为结构化语义树，**忽略注释、缩进、键顺序等格式噪音**，只关注配置"含义"的真实变化。

核心能力：

- **精准检测**：检出新增 / 删除 / 修改 / 类型变化四类漂移，按 CRITICAL / WARN / INFO 严重度分级，误报趋近于零
- **闭环可操作**：采集 → 解析 → 基线 → 比对 → 报告全流程一条命令打通；基线支持版本化与回滚，SQLite 持久化历史可追溯
- **无人值守**：守护进程（daemon）后台常驻定期扫描，检出漂移自动触发 webhook / 邮件 / 脚本三通道告警，含防抖去重与失败重试（**规则级重试可配**）；支持**开机自启**（systemd / launchd / schtasks 三平台）
- **工程友好**：退出码 0/1/2 契约可直接接入 CI/CD；JSON / **单文件离线 HTML** 报告导出；可选本地 Web 仪表盘（时间线、严重度分布、**环境对比**）
- **可扩展**：**插件化解析器接口**（entry point `cfgdrift.parsers` + 装饰器注册），支持任意自定义配置文件格式
- **随处可装**：C 核心解析 + 纯 Python 兜底，任意 Python 3.8+ 免编译器安装，Windows / Linux / macOS 跨平台

适合配置变更审计、安全合规检查、CI 门禁与日常运维巡检场景。

## 功能

- `cfgdrift init`：初始化数据库（默认 `~/.cfgdrift/cfgdrift.db`）
- `cfgdrift scan`：扫描单文件或目录，记录历史 / 保存基线 / `--watch` 轮询
- `cfgdrift baseline create|list|show|rollback`：基线版本化管理
- `cfgdrift diff --baseline NAME`：与基线比对，输出漂移报告（含 `file:line` 行号，`--no-line` 可关闭）
- `cfgdrift compare ENV1 ENV2...`：多环境基线对比，头部展示双方基线版本 `compare A -> B (vX vs vY)`，支持 `environments.yaml` 映射与 `--json`
- `cfgdrift severity add|list|remove|enable|disable`：自定义严重度覆盖规则（`severity.yaml`，非法正则报错 exit 2）。注意：`severity add --key-pattern` / `--value-pattern` / `--file-pattern` 均为**正则**；`masking.yaml` 的 `patterns` 是 **glob**（fnmatch）语义，二者不可混用
- `cfgdrift report --json out.json`：导出 JSON 报告；`cfgdrift report --html out.html`：导出**单文件离线 HTML**（摘要卡 + 严重度分布 + 变更列表，零外部依赖）
- `cfgdrift ignore add|list|remove`：忽略规则（exact / prefix / regex）
- `cfgdrift serve`：启动本地 Web 仪表盘（`127.0.0.1:8080`，需 `[web]` extra），支持**环境对比**视图与报告「导出 HTML」按钮
- `cfgdrift daemon enable-autostart|disable-autostart|autostart-status`：开机自启管理（systemd / launchd / schtasks，`--user` 默认 / `--system` 可选 / `--dry-run` 预览 / 幂等语义）
- `cfgdrift alert add --retry-count N --retry-delay ...`：规则级告警重试（总尝试次数 + 尝试间等待，缺省回退全局默认 3 次 1s/5s/30s）
- **敏感值脱敏**：终端 / JSON 报告 / HTML 报告 / Web API / 告警 payload 五个显示出口对 `password` / `token` / `secret` 等 13 类敏感键自动打码（`masking.yaml` 可定制，数据库始终保存原始值）
- **行号定位**：diff / compare 输出标明 `file:line`，便于快速定位漂移源
- **一致性约束（v0.6.0）**：在语义 diff 之上叠加约束检查层，对「变更后配置树」跑 range / enum / conditional_required / correlation / mutual_exclusion 五类约束，仅报告**与本次漂移关联**的约束破坏，输出确定性可解释的复合告警（severity 升级 + `constraint_violations`）
- **存量违反报告（v0.7.0）**：`cfgdrift scan --report-violations` 输出独立的「Baseline violations」section（terminal/json 均支持，**默认关闭**零噪音），严重度直接取约束自身 severity，与漂移关联的违反不重复报
- **corpus 基准语料（v0.7.0）**：`cfgdrift corpus init|fetch|export|validate` 从真实项目 git 历史挖掘配置变更对，标准化为 `instances.jsonl` 语料（metadata + before/after 语义树 + diff + feature + labels 预留），与 diff / 约束引擎打通；支持 `local_path` 本地 git 仓库离线采集与增量拉取
- **约束自动挖掘（v0.7.0）**：`cfgdrift constraint mine` 从历史扫描 / 语料挖掘候选（值域 enum/range、共现 conditional_required、互斥 mutual_exclusion），输出 `<home>/mined_candidates.yaml`（`enabled: false`、`status: pending`，**不自动生效**），人工确认后 `constraint add --rule` 转正
- **Web 约束视图（v0.7.0）**：Web 仪表盘新增「约束」视图（生效约束列表 + 用户规则启用/禁用切换 + 最近约束违反分页），违反事件持久化到 C-10 `constraint_violations` 表（默认保留 90 天，`CFGDRIFT_CV_RETENTION_DAYS` 可配）
- **自定义解析器插件**：`--format <plugin>` 支持第三方解析格式（见下文「自定义解析器插件」）

退出码：`0`=无漂移，`1`=检出漂移，`2`=错误。

## 安装

自 v0.2.0 起（当前版本 v0.8.0）`cfgdrift` 是**任何 Python 3.8+ 均可安装运行**
的通用包：C 扩展是可选加速器，未编译或安装失败时自动降级到纯 Python 解析器。

```bash
pip install cfgdrift            # 通用安装（pip 自动选件，见下）
pip install "cfgdrift[web]"     # 含 Web 仪表盘
pip install "cfgdrift[dev]"     # 含测试依赖
```

### 双 wheel 发布模型

| 发布件 | 适用用户 | 说明 |
|--------|----------|------|
| `cfgdrift-0.5.0-py3-none-any.whl` | 所有 Python 3.8+ | 纯 Python 通用 wheel（默认主发布件） |
| `cfgdrift-0.5.0-cp313-*-*.whl` | CPython 3.13 | 可选 C 加速平台 wheel（更快的 JSON/TOML/INI 解析） |
| `cfgdrift-0.5.0.tar.gz`（sdist） | 需要本地编译 | 携带 C 源码，`pip install` 时尝试编译，失败自动降级 |

pip 的标签优先级天然让 CPython 3.13 用户拿到加速件、其他版本拿到通用件。

### 本地构建配方

```bash
# 纯 Python 通用 wheel（确定性，不编译 C）
CFGDRIFT_NO_C=1 python -m build --wheel

# C 加速平台 wheel（可选；需要 C99 编译器）
python -m build --wheel

# sdist（携带 C 源码；勿设 CFGDRIFT_NO_C）
python -m build --sdist
```

### 环境变量

| 变量 | 取值 | 说明 |
|------|------|------|
| `CFGDRIFT_BACKEND` | `auto`（默认）/ `pure` / `c` | 解析后端选择。`auto` 有 C 用 C、无 C 静默降级纯 Python；`pure` 强制纯模式；`c` 强制 C，无 C 时 import 抛 `RuntimeError` |
| `CFGDRIFT_DEBUG` | `1` | 开启 `logging.DEBUG`，日志输出当前解析后端（`parser backend: c/pure`） |
| `CFGDRIFT_NO_C` | `1` / `true` / `yes` | 构建时跳过 C 扩展编译（产出纯 wheel） |
| `CFGDRIFT_HOME` | 路径 | 覆盖数据目录（默认 `~/.cfgdrift/`） |
| `GITHUB_TOKEN` | token | corpus star 检查的 GitHub token（优先级高于 corpus.yaml `token` 字段） |
| `CFGDRIFT_CV_RETENTION_DAYS` | 整数 | C-10 `constraint_violations` 表保留天数（默认 90；每 200 次插入惰性清理，行数上限 20000） |

### 双模式一致性

合法输入在 C 与纯 Python 两种后端下产出**语义等价**的树（类型敏感、键顺序不计）；
非法输入两种后端均抛 `ValueError`，消息以 `parse error at line L, column C`
开头（冒号后的文本允许差异）。`tests/test_dual_mode.py` 用同一语料库对双后端做
一致性回归。

已知文档化差异（详见 `docs/system_design.md` v0.2.0 章节）：

- JSON 未配对代理对（如 `"\ud83d"`）：纯模式接受（stdlib 行为），C 模式拒绝；
- 无时区 TOML 日期时间的小数秒：C 输出字面量（`...00.5`），纯模式输出
  `isoformat()` 的 6 位补零（`...00.500000`）；
- 本地时间带 UTC 偏移（`07:32:00Z`）不符合 TOML v1.0 语法：纯模式拒绝，
  C 模式按字面量接受；
- INI 节头尾随内容（`[s] junk`）与节名带空格（`[ s ]`）两种后端归一化不同；
- INI 多行续行：C 拒绝缩进续行，纯模式 configparser 接受为多行值（更宽松）。

## 快速上手

```bash
cfgdrift init
cfgdrift scan ./config --save-as-baseline prod
# …修改配置…
cfgdrift diff ./config --baseline prod          # 退出码 1 = 有漂移
cfgdrift serve                                   # 打开 http://127.0.0.1:8080
```

## 目录扫描约定

- 扩展名识别：`.json` / `.yaml|.yml` / `.toml` / `.ini|.cfg|.conf`；未知扩展名跳过并告警
- 单文件未知扩展名需显式 `--format`，否则报错
- 快照结构 `{relpath: tree}`；文件级新增=INFO、删除=CRITICAL
- 列表 diff 按索引比较，不检测元素重排（已知限制）

## 数据目录

默认 `~/.cfgdrift/`，可用环境变量 `CFGDRIFT_HOME` 或 CLI 全局选项 `--store PATH` 覆盖。

## daemon 开机自启（v0.5.0）

```bash
# Linux: 写 ~/.config/systemd/user/cfgdrift.service（--user 默认）并 systemctl --user enable
# macOS: 写 ~/Library/LaunchAgents/com.cfgdrift.daemon.plist 并 launchctl load -w
# Windows: 创建计划任务 schtasks /Create /TN cfgdrift-daemon /SC ONLOGON
cfgdrift daemon enable-autostart --target /etc/nginx --baseline prod --interval 300
cfgdrift daemon enable-autostart --target /etc/nginx --baseline prod --dry-run   # 预览，零落盘
cfgdrift daemon disable-autostart
cfgdrift daemon autostart-status        # 退出码 0=enabled / 1=disabled / 2=error
```

- 自启配置唯一真源为 `<home>/autostart.json`，与平台工件双写双清
- `enable` 前校验：target 存在、baseline 存在、`--interval >= 60`、`--format` 合法
- 幂等：已启用且参数一致 → no-op（exit 0）；参数不同 → 需 `--force` 覆盖（否则 exit 2）
- `--system` 为显式可选（需要 root/管理员权限）

## 告警重试可配（v0.5.0）

```bash
# 规则级：总尝试次数 5，间隔用全局默认 (1,5,30)
cfgdrift alert add --name nginx-webhook --type webhook --url http://x --retry-count 5
# 规则级：只给间隔 → 尝试次数 = len(delays)+1（此处 4 次）
cfgdrift alert add --name ops-email --type email --smtp-host ... --retry-delay 2,10,60
# 逗号分隔与重复两种写法等价
cfgdrift alert add --name x --type webhook --url http://x --retry-delay 1 --retry-delay 5 --retry-delay 30
```

- `retry_count` = 总尝试次数（默认 3，≥1）；`retry_delays` = 尝试间等待秒数列表（元素 ≥0）
- 规则级 > 全局默认；`alert list` 显示 `retry=3/1,5,30` 或 `retry=default`；防抖（冷却 600s）不变

## HTML 报告导出（v0.5.0）

```bash
cfgdrift report --scan-id 3 --html out.html   # 单文件离线 HTML，可直接浏览器打开
```

- 摘要卡 + 严重度分布条 + 变更列表（严重度徽标 / 键路径 / 变更类型 / 文件:行 / 旧值→新值 / 规则 / 约束违反）
- `masked=true` 的项显示「已脱敏」徽标；严重度配色与 Web 仪表盘一致
- Web 报告页同样提供「导出 HTML」按钮（`GET /api/reports/{scan_id}/html`）

## 一致性约束（v0.6.0）

一致性约束在语义 diff 之上叠加一层**约束检查**：`diff` / `scan` / `daemon` 检出漂移后，
对「变更后配置树」执行约束检查，**仅报告与本次漂移关联**的约束破坏（Q2：存量违反 P0 不报），
输出确定性可解释的复合告警（severity 升级 + `constraint_violations`）。不依赖外部业务真值。

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

- **五类约束**：`range`（数值区间）、`enum`（白名单）、`conditional_required`
  （条件满足时必填键）、`correlation`（条件满足时数值/字符串关系）、`mutual_exclusion`
  （两键互斥）。完整 schema 见 `examples/constraints.yaml.example`。
- **升级制（Q1）**：`new = min(CRITICAL, max(item.rank+1, max(违反约束.rank)))`，
  复用 `Severity.rank`（NONE=0 / INFO=1 / WARN=2 / CRITICAL=3），不引入独立 CONSTRAINT 级别；
  每个 item 只升级一次（全部 violation 附加后统一计算）。
- **关联判定（D5）**：逐文件，`involved_keys ∩ 漂移 keys ≠ ∅` 即关联；violation 附加到所有
  `key_path ∈ involved_keys` 的漂移项；缺失键 / 不满足 when 的约束一律跳过（零噪音基础）。
- **五处呈现**：terminal（项后追加 `constraint <id> [<type>]: <message>`）、JSON
  （`DriftItem.to_dict()` 仅非空输出 `constraint_violations`）、HTML（新增「约束违反」列）、
  Web 仪表盘（报告页/告警列表徽标+消息）、alert payload（每项 `constraint` 字段，首条按
  `constraint_id` 排序确定）。
- **生效约束解析（D8）**：内置库（`--builtin` 默认 on）→ `<home>/constraints.yaml`（若存在）
  → `--constraints` 额外文件（可重复，按序）；同 id 后者覆盖前者（可覆盖内置库）。
- **daemon 生效时机（D9）**：worker 每周期重载约束文件，`constraint add` 下个周期生效；
  `severity_rules` 维持启动时加载。
- **零噪音契约（D7）**：合法变更（如 `server.port` 8080→9090 在范围内）输出与 v0.5.0
  逐字节一致——无 `constraint_violations` 字段、terminal 不新增行、HTML 新列显示 `-`、
  alert payload 无 `constraint` 字段。
- compare（基线间对比）本版不跑约束检查（D10）。

## 自定义解析器插件（v0.5.0）

`--format` 除内置 `auto/json/yaml/toml/ini` 外，还接受**已注册的插件名**。
插件返回**原始树**（dict/list/scalar），由引擎统一归一化为语义树；可选的
`build_line_map` 提供 `{key_path: 行号}` 行号映射（未提供则行号为
`None`，diff 渲染不输出 `:N`）。插件 `parse` 抛出的 `ValueError` /
`RuntimeError` / `OSError` 会被 CLI 捕获为 `error: <消息>` 并以 **exit 2**
结束，建议错误消息自带 1-based 行号（如 `unbalanced '{' at line 1`）。

### 插件协议（真实签名）

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

要点：

- `parse(text)` 返回原始树后，引擎走与内置格式**完全相同**的
  `_normalize` / `_wrap_top_level` 归一化：键转 `str`；顶层非 dict 包装为
  `{"$": value}`；`datetime/date/time` 转 ISO-8601 字符串；`None` 视作空
  dict；其余非常规对象转字符串。
- 行号映射的 **key_path 约定**与内置格式一致（`cfgdrift.core.model.join_path`）：
  dict 段用 `.` 连接，list 下标追加 `[i]`，含 `.` / `[` / `]` / `\` 的段做
  反斜杠转义。插件应直接调用 `join_path` 构造 key（见下示例），不要手拼字符串。
- 未提供 `build_line_map`：`parse_text_lines` 返回空 `{}`，diff 的
  `item.line` 为 `None`，输出不含 `:N`。`build_line_map` 自身抛异常也会被
  降级为 warning + 空映射（行号只是增强，永不阻断解析）。
- `extensions` 必须是小写、带点前缀（如 `(".dsl",)`）；`detect_format` 在
  内置扩展名之后按插件扩展名兜底识别。

### 方式 A：装饰器注册（进程内，import 即生效）

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

- 裸 `@register_plugin` 也合法（插件名取函数名）；`registry=reg` 可把插件
  注册到自定义 `PluginRegistry`（默认注册到全局共享 registry）。
- 注意：若同名插件已通过 entry point 注册（方式 B 已 `pip install -e` 激活），
  同一进程内再照抄本示例做装饰器注册会抛
  `ValueError: parser plugin 'mydsl' is already registered (use replace=True to overwrite)`
  ——`register_plugin` 内部以 `replace=False` 注册，与 entry point 的
  `replace=True` 相反。此时可换用不同插件名，或仅依赖 `import mydsl_parser`
  的 import 副作用（方式 A 的等价注册已随导入完成）。
- 脚本/测试进程内 import 该模块后即可用：

```bash
cfgdrift scan app.dsl --format mydsl        # 扩展名 .dsl 也可自动识别
```

### 方式 B：entry point 注册（pip 打包分发，优先于同名装饰器）

```toml
# pyproject.toml（插件包）
[project.entry-points."cfgdrift.parsers"]
mydsl = "mydsl_plugin:plugin"
```

entry point 值支持四种形态（`_coerce_entry_point` 归一化）：

1. **`ParserPlugin` 实例**（推荐）：`plugin = ParserPlugin(name="mydsl",
   extensions=(".dsl",), parse=parse_mydsl, build_line_map=line_map_mydsl)`
2. `(parse_fn, {"extensions": [...], "line_map": fn})` 元组——注意选项键是
   `line_map`（不是 `build_line_map`），`name` 缺省取 entry point 名；
3. 裸 `parse(text)` 函数（插件名取 entry point 名）；
4. `{"parse": fn, "extensions": [...], "line_map": fn}` 映射。

同名冲突时 **entry point 覆盖装饰器注册**（`replace=True`）；单个插件加载
失败仅 warning，不影响内置解析器；发现使用 Python 3.8+ 标准库
`importlib.metadata`，零新增依赖。

### 完整可运行示例：examples/mydsl-parser

仓库自带一个完整可运行的 nginx-like DSL 插件包
`examples/mydsl-parser/`（含 pyproject.toml、解析器源码、行号映射、示例配置
`examples/demo/nginx-like.dsl`、pytest 测试、包内 README）。端到端演示：

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

方式 A 的进程内用法（wrapper 脚本，不依赖 entry point）：

```python
import mydsl_parser                       # import 即通过装饰器注册 "mydsl"
from cfgdrift.cli import main
raise SystemExit(main(["scan", "app.dsl", "--format", "mydsl"]))
```

示例插件的行号映射展示了 `join_path` 约定下的多层嵌套 + 数组下标
（`server[0].location./api.proxy_pass` → 第 9 行），以及重复 `server` 块自动
转数组（`server[0]` / `server[1]`）。

### 插件测试示例（pytest）

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

完整 17 个用例见 `examples/mydsl-parser/tests/test_mydsl_parser.py`（解析正确性、
行号映射精确行号、`parse_text`/`parse_text_lines` 分发、diff 行号落位、装饰器与
entry point 两种注册形态）。

- 未注册的 `--format` 报错包含注册指引（`cfgdrift.parsers` entry point 组 / `register_plugin`）
- 同名冲突 entry point 覆盖装饰器；单个插件加载失败仅 warning，不影响内置解析器
- 插件发现使用 Python 3.8+ 标准库 `importlib.metadata`，零新增依赖


## v0.7.0 增量：corpus 语料 / 约束挖掘 / Web 约束视图 / 存量违反报告

v0.7.0 在 v0.6.0 一致性约束之上新增四项能力，全部以**新模块承载 + 既有接口可选参数/条件输出**接入；`core/differ.py` 与约束引擎保持**纯函数零 DB 依赖**（违反持久化由调用层完成）。

### 1. corpus 基准语料工具链（方向 E）

```bash
cfgdrift corpus init --workspace <dir>          # 生成 corpus.yaml + state.json + repos/
cfgdrift corpus fetch --workspace <dir>          # 拉取 git 历史 -> 变更对 -> 全量重写 instances.jsonl
cfgdrift corpus export --workspace <dir>         # 幂等全量重写（确定性）
cfgdrift corpus validate --workspace <dir>       # JSONL schema 校验 + 统计（损坏 exit 2）
```

- 配置 `corpus.yaml`（version:1）：`since` / `min_stars` / `max_instances`(默认 200) / `token` / `repositories[]`（`owner`+`repo` 或 `local_path` 二选一；`glob` / 仓库级 `since` 可选）。模板见 `examples/corpus.yaml.example`
- 采集：非 local 仓库 `git clone --filter=blob:none --no-checkout` + 增量 `git fetch`；`local_path` 直用本地 git 仓库（**离线 / CI 安全**）；star 检查走 GitHub API（best-effort，失败 warning 不阻塞）
- 增量：`state.json` 记录每仓库 `last_commit` / `stars` / `instance_count`，fetch 只处理上次之后的新提交
- 实例 schema（`instances.jsonl` 每行一个变更实例）：`schema_version` / `instance_id` / `metadata` / `file` / `before` / `after`（语义树 + parse_ok + present）/ `diff`（items + summary + constraint_violations + feature{changed_keys, changed_values, co_change_pairs, co_change_capped}）/ `labels`（severity + annotation/annotator 预留）；**text 原文不落盘**（防膨胀）

### 2. 约束自动挖掘（C-08）

```bash
cfgdrift constraint mine --min-support 5 --source scans        # 默认：当前 store 的 scan_items
cfgdrift constraint mine --source corpus --corpus instances.jsonl
cfgdrift constraint mine --json                                # 输出完整 JSON（含 metrics）
```

- 三类候选：**值域**（enum：distinct 属于 [2,8]；range：全数值，port 键建议 [1,65535]，其余标 `observed:true`）、**共现**（conditional_required：co/cnt >= 0.8）、**互斥**（mutual_exclusion：零交集且每侧样本 >= min_support，每键对 top-5）
- 输出 `<home>/mined_candidates.yaml`（version:1，候选 `enabled: false`、`status: pending`），**候选区永不自动生效**；转正 = `cfgdrift constraint add --rule '<constraint JSON>'`（+ `constraint enable`）。模板见 `examples/mined_candidates.yaml.example`

### 3. Web 约束视图（C-09）+ C-10 违反持久化

- 新表 `constraint_violations`（drift / baseline 两 kind），默认保留 90 天（`CFGDRIFT_CV_RETENTION_DAYS` 可配），每 200 次插入惰性清理 + 行数上限 20000
- Web 新端点：`GET /api/constraints`（生效视角，与 `constraint list --source all` 一致）、`PUT /api/constraints/{id}/enabled`（用户规则切换；内置约束 400）、`GET /api/constraint-events`（分页）
- SPA 新增「约束」视图（生效约束表格 + 用户规则启用/禁用 + 最近违反分页）

### 4. 存量违反报告（C-07）

```bash
cfgdrift scan PATH --baseline B --report-violations    # 默认关闭
```

- `ConstraintEngine.check_tree` 对 new_snapshot 逐文件跑全部启用约束；`baseline_violations` 与漂移关联违反按签名 `(constraint_id, file, frozenset(involved_keys))` 差集去重，severity 直取约束自身
- terminal 输出「Baseline violations:」section（items 后、Summary 前）；json 输出 `baseline_violations` 字段；**默认关闭时与 v0.6.0 逐字节一致**；HTML 报告不渲染该 section（`htmlreport.py` 零改动）
- C-10 写入：`scan --report-violations` 写 drift + baseline 两类；daemon 只写 drift 违反

> 注意：`instances.jsonl` 由 `corpus fetch/export` 生成并**全量重写**（幂等）；`corpus fetch` 的 git 操作依赖 PATH 中的 `git` 可执行文件；离线/CI 请使用 `local_path` 本地 git 仓库。


## v0.8.0 增量：双人标注+kappa / severity×constraint_id / compare 约束闭环 / 业务影响叙事

v0.8.0 在 v0.7.0 之上新增四项能力，全部以**新模块承载 + 既有接口可选参数/条件输出**接入（无新增第三方依赖；版本三处同步 `0.8.0 / 0.8.0 / 0.8.0-c`）。

### 1. corpus 双人标注 + kappa（C-C5）

```bash
cfgdrift corpus annotate --workspace <dir> --annotator alice            # 交互标注（[1]severe [2]minor [3]normal [s]跳过 [q]保存退出）
cfgdrift corpus annotate --workspace <dir> --annotator alice --batch labels.yaml   # 非交互批量导入（CI 友好）
cfgdrift corpus kappa --workspace <dir> [--annotator-a A --annotator-b B] [--weighted linear|quadratic] [--json]
cfgdrift corpus stats --workspace <dir> [--json]
```

- **独立存储**：标注写入 `<workspace>/annotations.jsonl`（`{instance_id, annotator, annotation, annotated_at}`，3 分类序数 `severe|minor|normal`），与 `instances.jsonl` 分离——后者由 export 全量重写，独立存储防丢失
- **export 合并（D3）**：`corpus export` 把每实例**最新一条**标注（`annotated_at` 排序，同刻按 annotator 字典序）投影进 `labels.annotation` / `labels.annotator`；**重复 export 不丢失标注**
- **kappa（Q2）**：Cohen's kappa（`po=Σn_ii/n`、`pe=Σrow·col/n²`、`κ=(po−pe)/(1−pe)`）+ 一致率 + 混淆矩阵（行=A 列=B）；`--weighted linear|quadratic` 输出加权 kappa；无参自动配对重叠样本最多的两人（D4）；不足 2 名标注人 / 重叠 < 2 条 → exit 2
- **stats（§6.3）**：实例总数 / 未标注 / 单标注（按人拆分）/ 双人完成 / 一致率 / kappa 可计算数
- 模板见 `examples/annotations.jsonl.example`

### 2. severity 引用 constraint_id（C-13）

```bash
cfgdrift severity add --name port-critical --severity CRITICAL --constraint-id http_port_range   # 单个或逗号分隔/多次
cfgdrift severity list        # 输出追加 constraint=<ids>（无则 -）
```

- `SeverityRule.constraint_id`（str/list 归一化 `List[str]`）是**额外 AND 条件**：规则仅当该项关联的约束违反命中该 id 时才生效；与 `key_pattern` 等并存时全部满足才命中；文件顺序 first-match-wins 不变
- **管线顺序（D1）**：约束 attach（只挂不升级）→ severity 覆盖（constraint_id 规则可读 `item.constraint_violations`）→ 统一升级 `min(3, max(rank+1, max_c_rank))` → summary.max_severity → 告警阈值；升级公式关于 severity 单调，**无 constraint_id 规则时与 v0.7.0 输出逐字节一致**
- `to_dict` 仅非空输出 `constraint_id`（旧规则 yaml 字节不变）

### 3. compare 跑约束（D10 补全）

```bash
cfgdrift compare dev prod --constraints my.yaml     # 自定义约束
cfgdrift compare dev prod --no-builtin              # 关闭内置约束库
cfgdrift compare dev prod --json                    # constraint_violations 进 JSON
```

- `CompareReport.constraint_violations`：`{"env_a": [...], "env_b": [...]}`（`check_tree` 形状，severity 直取约束）；terminal 在 items 后、Summary 前渲染「约束检查 (D10 补全)」区块（**仅非空**）
- 违反为**信息性**：exit code 保持 drift-based（0/1/2 语义不变，D6）；无违反时输出与 v0.7.0 逐字节一致

### 4. 业务影响叙事（方向 A）

```bash
cfgdrift explain ./config --baseline prod [--format text|json] [--schema schema.yaml] [--no-llm]
cfgdrift diff ./config --baseline prod --explain       # diff 末尾追加同一叙事区块
```

- 每条漂移输出 `{key, change_type, severity, impact, evidence[], source}`；**确定性模板引擎**离线可用（内置 24+ 键语义字典 + 四类变更模板 + 约束违反/`latest` 特判/severity 兜底）
- **LLM 增强（Q5）**：OpenAI 兼容 REST（stdlib `urllib`，`CFGDRIFT_LLM_URL/KEY/MODEL/TIMEOUT`），`temperature=0`；无 key / 超时 / HTTP 错误 / JSON 解析失败 / **证据校验失败** → 统一回退模板并标记 `source: template`
- **证据链防幻觉（A-P0-3）**：`evidence` 只取输入事实三型（`key:` / `value:` / `constraint: …违反`）；LLM 输出经 `EvidenceValidator` 校验（evidence⊆facts、constraint_id ∈ facts、编造 key 检测），失败即整项回退
- **脱敏（D7）**：explain 是显示出口，先 `SensitiveMasker` 脱敏再叙事，敏感值不泄漏
- 模板见 `examples/explain_schema.yaml.example`（`{patterns: {regex: 描述}}` 用户字典 merge，用户条目优先）


## v0.7.0 增量：corpus 语料 / 约束挖掘 / Web 约束视图 / 存量违反报告

v0.7.0 在 v0.6.0 一致性约束之上新增四项能力，全部以**新模块承载 + 既有接口可选参数/条件输出**接入；`core/differ.py` 与约束引擎保持**纯函数零 DB 依赖**（违反持久化由调用层完成）。

### 1. corpus 基准语料工具链（方向 E）

```bash
cfgdrift corpus init --workspace <dir>          # 生成 corpus.yaml + state.json + repos/
cfgdrift corpus fetch --workspace <dir>          # 拉取 git 历史 -> 变更对 -> 全量重写 instances.jsonl
cfgdrift corpus export --workspace <dir>         # 幂等全量重写（确定性）
cfgdrift corpus validate --workspace <dir>       # JSONL schema 校验 + 统计（损坏 exit 2）
```

- 配置 `corpus.yaml`（version:1）：`since` / `min_stars` / `max_instances`(默认 200) / `token` / `repositories[]`（`owner`+`repo` 或 `local_path` 二选一；`glob` / 仓库级 `since` 可选）。模板见 `examples/corpus.yaml.example`
- 采集：非 local 仓库 `git clone --filter=blob:none --no-checkout` + 增量 `git fetch`；`local_path` 直用本地 git 仓库（**离线 / CI 安全**）；star 检查走 GitHub API（best-effort，失败 warning 不阻塞）
- 增量：`state.json` 记录每仓库 `last_commit` / `stars` / `instance_count`，fetch 只处理上次之后的新提交
- 实例 schema（`instances.jsonl` 每行一个变更实例）：`schema_version` / `instance_id` / `metadata` / `file` / `before` / `after`（语义树 + parse_ok + present）/ `diff`（items + summary + constraint_violations + feature{changed_keys, changed_values, co_change_pairs, co_change_capped}）/ `labels`（severity + annotation/annotator 预留）；**text 原文不落盘**（防膨胀）

### 2. 约束自动挖掘（C-08）

```bash
cfgdrift constraint mine --min-support 5 --source scans        # 默认：当前 store 的 scan_items
cfgdrift constraint mine --source corpus --corpus instances.jsonl
cfgdrift constraint mine --json                                # 输出完整 JSON（含 metrics）
```

- 三类候选：**值域**（enum：distinct 属于 [2,8]；range：全数值，port 键建议 [1,65535]，其余标 `observed:true`）、**共现**（conditional_required：co/cnt >= 0.8）、**互斥**（mutual_exclusion：零交集且每侧样本 >= min_support，每键对 top-5）
- 输出 `<home>/mined_candidates.yaml`（version:1，候选 `enabled: false`、`status: pending`），**候选区永不自动生效**；转正 = `cfgdrift constraint add --rule '<constraint JSON>'`（+ `constraint enable`）。模板见 `examples/mined_candidates.yaml.example`

### 3. Web 约束视图（C-09）+ C-10 违反持久化

- 新表 `constraint_violations`（drift / baseline 两 kind），默认保留 90 天（`CFGDRIFT_CV_RETENTION_DAYS` 可配），每 200 次插入惰性清理 + 行数上限 20000
- Web 新端点：`GET /api/constraints`（生效视角，与 `constraint list --source all` 一致）、`PUT /api/constraints/{id}/enabled`（用户规则切换；内置约束 400）、`GET /api/constraint-events`（分页）
- SPA 新增「约束」视图（生效约束表格 + 用户规则启用/禁用 + 最近违反分页）

### 4. 存量违反报告（C-07）

```bash
cfgdrift scan PATH --baseline B --report-violations    # 默认关闭
```
## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

- `ConstraintEngine.check_tree` 对 new_snapshot 逐文件跑全部启用约束；`baseline_violations` 与漂移关联违反按签名 `(constraint_id, file, frozenset(involved_keys))` 差集去重，severity 直取约束自身
- terminal 输出「Baseline violations:」section（items 后、Summary 前）；json 输出 `baseline_violations` 字段；**默认关闭时与 v0.6.0 逐字节一致**；HTML 报告不渲染该 section（`htmlreport.py` 零改动）
- C-10 写入：`scan --report-violations` 写 drift + baseline 两类；daemon 只写 drift 违反

> 注意：`instances.jsonl` 由 `corpus fetch/export` 生成并**全量重写**（幂等）；`corpus fetch` 的 git 操作依赖 PATH 中的 `git` 可执行文件；离线/CI 请使用 `local_path` 本地 git 仓库。
