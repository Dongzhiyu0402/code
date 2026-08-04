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
- **自定义解析器插件**：`--format <plugin>` 支持第三方解析格式（见下文「自定义解析器插件」）

退出码：`0`=无漂移，`1`=检出漂移，`2`=错误。

## 安装

自 v0.2.0 起（当前版本 v0.5.0）`cfgdrift` 是**任何 Python 3.8+ 均可安装运行**
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

- 摘要卡 + 严重度分布条 + 变更列表（严重度徽标 / 键路径 / 变更类型 / 文件:行 / 旧值→新值 / 规则）
- `masked=true` 的项显示「已脱敏」徽标；严重度配色与 Web 仪表盘一致
- Web 报告页同样提供「导出 HTML」按钮（`GET /api/reports/{scan_id}/html`）

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
