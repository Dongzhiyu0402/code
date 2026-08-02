# cfgdrift — 语义级配置漂移检测系统

`cfgdrift` 是一个语义级（semantic-level）的配置漂移检测工具，区别于传统文件级（hash/字节比对）方案：它把 JSON、YAML、TOML、INI 配置文件解析为结构化语义树，**忽略注释、缩进、键顺序等格式噪音**，只关注配置"含义"的真实变化。

核心能力：

- **精准检测**：检出新增 / 删除 / 修改 / 类型变化四类漂移，按 CRITICAL / WARN / INFO 严重度分级，误报趋近于零
- **闭环可操作**：采集 → 解析 → 基线 → 比对 → 报告全流程一条命令打通；基线支持版本化与回滚，SQLite 持久化历史可追溯
- **无人值守**：守护进程（daemon）后台常驻定期扫描，检出漂移自动触发 webhook / 邮件 / 脚本三通道告警，含防抖去重与失败重试
- **工程友好**：退出码 0/1/2 契约可直接接入 CI/CD；JSON 报告导出；可选本地 Web 仪表盘（时间线、严重度分布）
- **随处可装**：C 核心解析 + 纯 Python 兜底，任意 Python 3.8+ 免编译器安装，Windows / Linux / macOS 跨平台

适合配置变更审计、安全合规检查、CI 门禁与日常运维巡检场景。

## 功能

- `cfgdrift init`：初始化数据库（默认 `~/.cfgdrift/cfgdrift.db`）
- `cfgdrift scan`：扫描单文件或目录，记录历史 / 保存基线 / `--watch` 轮询
- `cfgdrift baseline create|list|show|rollback`：基线版本化管理
- `cfgdrift diff --baseline NAME`：与基线比对，输出漂移报告
- `cfgdrift report --json out.json`：导出 JSON 报告
- `cfgdrift ignore add|list|remove`：忽略规则（exact / prefix / regex）
- `cfgdrift serve`：启动本地 Web 仪表盘（`127.0.0.1:8080`，需 `[web]` extra）

退出码：`0`=无漂移，`1`=检出漂移，`2`=错误。

## 安装

自 v0.2.0 起（当前版本 v0.3.0）`cfgdrift` 是**任何 Python 3.8+ 均可安装运行**
的通用包：C 扩展是可选加速器，未编译或安装失败时自动降级到纯 Python 解析器。

```bash
pip install cfgdrift            # 通用安装（pip 自动选件，见下）
pip install "cfgdrift[web]"     # 含 Web 仪表盘
pip install "cfgdrift[dev]"     # 含测试依赖
```

### 双 wheel 发布模型

| 发布件 | 适用用户 | 说明 |
|--------|----------|------|
| `cfgdrift-0.3.0-py3-none-any.whl` | 所有 Python 3.8+ | 纯 Python 通用 wheel（默认主发布件） |
| `cfgdrift-0.3.0-cp313-*-*.whl` | CPython 3.13 | 可选 C 加速平台 wheel（更快的 JSON/TOML/INI 解析） |
| `cfgdrift-0.3.0.tar.gz`（sdist） | 需要本地编译 | 携带 C 源码，`pip install` 时尝试编译，失败自动降级 |

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
