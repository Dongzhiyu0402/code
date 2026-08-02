# cfgdrift v0.3.0 发布产物验证报告

- 验证人：工程师 寇豆码（software-engineer-4）
- 验证时间：2026-08-03 01:00（初版）；2026-08-03 01:07（README 0.2.0 残留修复后重建）
- Python：3.13.12（`C:/Users/20713/.workbuddy/binaries/python/versions/3.13.12/python.exe`）
- 构建后端：setuptools 83.0.0 / wheel 0.47.0 / build 1.5.0

## 〇、收尾修复：README 版本号残留（QA 复核发现）

修改文件：`README.md`
- 第 35-37 行「双 wheel 发布模型」表格文件名 `cfgdrift-0.2.0-*` → `cfgdrift-0.3.0-*`
- 第 22 行历史说明补充当前版本（`自 v0.2.0 起（当前版本 v0.3.0）`）
- 第 70 行 `docs/system_design.md` v0.2.0 章节引用**保留**（附录 A 确为 v0.2.0 增量设计，改之断链）

修复后全局检查：README 中发布相关文件名/版本号均已为 0.3.0，无 0.2.0 残留。

因 `pyproject.toml` 配置 `readme = "README.md"`，README 变更进入 wheel METADATA Description 与 sdist PKG-INFO，故三件产物全部重建。重建后快速验证（详见第七节）：METADATA/PKG-INFO 均含 `cfgdrift-0.3.0-*` 文件名且不含 `cfgdrift-0.2.0`。

## 一、产物清单（dist-v030/，重建后）

| 产物 | 大小 | sha256(前16位) |
|---|---|---|
| `cfgdrift-0.3.0-py3-none-any.whl` | 81,570 B | 重建后 |
| `cfgdrift-0.3.0.tar.gz` | 111,723 B | 重建后 |
| `cfgdrift-0.3.0-cp313-cp313-win_amd64.whl` | 100,965 B | 重建后 |

完整路径：
- `C:\Users\20713\WorkBuddy\2026-08-02-22-02-27\dist-v030\cfgdrift-0.3.0-py3-none-any.whl`
- `C:\Users\20713\WorkBuddy\2026-08-02-22-02-27\dist-v030\cfgdrift-0.3.0.tar.gz`
- `C:\Users\20713\WorkBuddy\2026-08-02-22-02-27\dist-v030\cfgdrift-0.3.0-cp313-cp313-win_amd64.whl`

## 二、纯 Python 通用 wheel（主发布件）

构建命令：`CFGDRIFT_NO_C=1 python -m build --wheel --outdir dist-v030 --no-isolation`

### 内容验证（zipfile 实测）
- 二进制文件（.pyd/.so/.dll）：**NONE — PASS**
- daemon 模块：`cfgdrift/daemon/__init__.py`、`cfgdrift/daemon/daemon.py`、`cfgdrift/daemon/worker.py` ✓
- alert 模块：`cfgdrift/alert/__init__.py`、`channels.py`、`config.py`、`dispatcher.py`、`models.py`、`state.py` ✓
- 共 36 个文件

### METADATA 实测
```
Version: 0.3.0
Requires-Python: >=3.8
Requires-Dist: click>=8.1
Requires-Dist: PyYAML>=6.0
Requires-Dist: tomli>=2.0; python_version < "3.11"
Requires-Dist: fastapi>=0.110; extra == "web"
Requires-Dist: uvicorn>=0.27; extra == "web"
Requires-Dist: pytest>=8.0; extra == "dev"
```

### 干净安装实测（pip install --target 临时目录）
```
cfgdrift version: 0.3.0
HAVE_C: False
PARSER_BACKEND: pure
daemon+alert imports: OK
```
CLI：
```
$ cfgdrift --version
cfgdrift, version 0.3.0
$ cfgdrift daemon --help   # exit 0，含 start/status/stop 子命令
$ cfgdrift alert --help    # exit 0，含 add/list/remove/test 子命令
```

## 三、sdist 源码包

构建命令：`python -m build --sdist --outdir dist-v030 --no-isolation`（未设 CFGDRIFT_NO_C）

### 内容验证（tarfile 实测，66 个条目）
- `src/csrc/*.c` 共 4 个：`parser_core.c`、`parser_ini.c`、`parser_json.c`、`parser_toml.c` ✓
- `MANIFEST.in` ✓
- `setup.py`、`pyproject.toml` ✓
- daemon 3 个 .py + alert 6 个 .py 源码全部在包内 ✓
- tests/ 全套测试源码（含 test_qa_v030.py 等）✓

### 可复现性验证
从 sdist 解压后重新 `python -m build --wheel`，成功产出 `cfgdrift-0.3.0-cp313-cp313-win_amd64.whl`（EXIT=0），证明源码包完整、可复现构建。

## 四、C 加速平台 wheel（可选）

构建命令：`python -m build --wheel --outdir dist-v030 --no-isolation`（本机 MSVC 编译成功）

### 内容验证
- `cfgdrift/_cfgdrift.cp313-win_amd64.pyd` ✓
- 纯 wheel 中无任何二进制（再次确认无泄漏）

### 干净安装实测
```
cfgdrift version: 0.3.0
HAVE_C: True
PARSER_BACKEND: c
C parse result: {'a': 1}   # C 解析正常
```

## 五、全量回归测试

```
343 passed, 2 skipped in 68.16s (0:01:08)
```
与 QA 第 4 轮基线完全一致（343 passed / 2 skipped）。

## 六、纯 wheel 干净安装冒烟（端到端）

环境：`CFGDRIFT_HOME` 指向沙箱 home，PYTHONPATH 指向纯 wheel 安装目标。

| 步骤 | 命令 | 结果 |
|---|---|---|
| init | `cfgdrift init` | exit 0，db 创建成功 |
| 保存基线 | `cfgdrift scan --save-as-baseline prod <dir>` | exit 0，Snapshot 保存 |
| 修改配置 | port 8080→9090、workers 4→8、level info→debug | - |
| diff | `cfgdrift scan --baseline prod <dir>` | **exit 1**，3 处漂移（modified=3 total=3 max=WARN） |
| 告警规则 | `cfgdrift alert add --name drift-alert --type webhook --url http://127.0.0.1:9999/hook` | exit 0，写入 alerts.yaml |
| alert list | `cfgdrift alert list` | exit 0，显示规则 |
| daemon status | `cfgdrift daemon status`（未运行） | **exit 1**，`daemon not running` |
| daemon 短跑 | `cfgdrift daemon start --target <dir> --baseline prod --interval 1 --foreground` | 15s 超时终止（预期）；worker 启动、pid 写入、scan 循环正常、检测到漂移、alert dispatcher 触发 webhook（连接拒绝→重试3次→cooldown 抑制） |

## 七、重建后快速验证（README 修复后）

三件产物时间戳均为 2026-08-03 01:07（更新）。

| 检查项 | 纯 wheel | 平台 wheel | sdist |
|---|---|---|---|
| METADATA/PKG-INFO 含 `cfgdrift-0.3.0-*` | True | True | True |
| METADATA/PKG-INFO 含 `cfgdrift-0.2.0` | False | False | False |
| Version: 0.3.0 | True | - | - |
| 无 .pyd/.so/.dll | True | - | - |
| daemon/alert 7 个新模块 | True | - | - |
| 含 `_cfgdrift.cp313-win_amd64.pyd` | - | True | - |
| csrc/*.c 4 个 + MANIFEST.in + daemon/alert 源码 | - | - | True |

干净安装冒烟（纯 wheel，pip install --target）：
```
version: 0.3.0
HAVE_C: False | BACKEND: pure
daemon+alert imports: OK
```

## 八、备注与建议

1. **发布组合建议**：三件一起发布（sdist + py3-none-any wheel + cp313-win_amd64 wheel）。
   - PyPI 择优：`cp313-cp313-win_amd64` 会被 Python 3.13 + Windows x64 环境优先选中（精确匹配）；`py3-none-any` 作为通用兜底覆盖所有其他平台/Python 版本；sdist 供无法直接使用 wheel 的环境（如无网络编译、旧 setuptools）从源码安装。
   - 说明：纯 wheel 安装后自动走 pure 后端（HAVE_C=False），C wheel 自动走 C 后端（HAVE_C=True），两者功能等价、测试覆盖双模式。

2. **无害观察**：纯 wheel 内含 4 个 `csrc/*.c` 源码文件（setuptools 对 Extension sources 的附带打包行为）。不含任何二进制，不影响 `py3-none-any` 通用 tag，对使用者无副作用，可接受。

3. **遗留文件提醒**：源码树 `src/cfgdrift/_cfgdrift.cp313-win_amd64.pyd` 是历史 in-place 构建遗留物。已验证它**不会**进入纯 wheel 和 sdist；建议后续提交前 gitignore 或清理，避免仓库内出现平台相关二进制。

4. **环境变量兼容**：构建时使用 `CFGDRIFT_NO_C=1` 强制纯 wheel；运行时支持 `CFGDRIFT_BACKEND=auto|pure|c` 三态切换（auto 默认，C 缺失自动降级）。
