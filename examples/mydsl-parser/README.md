# mydsl-parser — cfgdrift 插件化解析器示例包

`mydsl-parser` 是一个**完整可运行**的 cfgdrift v0.5.0 解析器插件示例，实现了一种
「nginx-like」DSL，用于演示插件化解析器的三种核心价值：

1. **任意自定义格式接入**：`--format mydsl` 直接解析，无需修改 cfgdrift 本体；
2. **多层嵌套 + 数组元素**：`server { location /api { ... } }` 与重复块自动转数组；
3. **行号定位**：`build_line_map` 按 cfgdrift 的 key-path 约定返回
   `{key_path: 行号}`，`diff` 输出 `file:line` 精确定位漂移源。

## DSL 子集

```
# 注释（整行或行尾 # 均可）
server {                 # 无名称块：按 type 存储；重复 type 自动转数组
    listen 8080;         # 语句：首个 token 是 key，其余是 value
    server_name example.com www.example.com;   # 多 token -> 标量数组
    root "/var/www/my site";                   # 带引号字符串 -> 去引号
    location /api {      # 有名称块：按 type 分组，存为 {type: {name: {...}}}
        proxy_pass http://backend:9000;
    }
}
```

解析规则（详见 `mydsl_parser/plugin.py` 模块 docstring）：

- 标量：数值 token 转 `int` / `float`；单/双引号字符串去引号；其余按原字符串；
  空值（`key;`）为 `null`。
- 数组：一条语句多个 value token -> 标量列表（如 `server_name`）。
- 块数组：同一 `type` 的**无名称块**重复出现 -> `[{...}, {...}]`；同一
  `type + name` 的**有名称块**重复出现同样转数组。
- 同名标量语句 last-wins（与 JSON / INI 重复键行为一致）。
- 限制：语句必须单行；`{` / `}` 必须单独成行；括号不配对抛
  `ValueError`（带行号，CLI 报错并 exit 2）。

## 安装与注册

### 方式 B：entry point（pip 安装后自动发现）

```bash
# 在项目根目录执行（示例包无第三方依赖）
C:/Users/20713/.workbuddy/binaries/python/versions/3.13.12/python.exe -m pip install -e examples/mydsl-parser

# 验证注册成功（load_entry_points 后 --format mydsl 可用；也可省略 --format 按 .dsl 自动识别）
cfgdrift scan examples/demo/nginx-like.dsl --format mydsl
```

entry point 声明在 `pyproject.toml`：

```toml
[project.entry-points."cfgdrift.parsers"]
mydsl = "mydsl_parser:plugin"
```

`mydsl_parser.plugin` 是一个 `ParserPlugin` 实例。entry point 值还支持另外三种
形态：`(parse_fn, {"extensions": [...], "line_map": fn})` 元组、裸
`parse(text)` 函数（名字取 entry point 名）、`{"parse": fn, ...}` 映射。

### 方式 A：装饰器（进程内 import 即注册）

```python
import mydsl_parser  # 仅 import 即通过 register_plugin 注册 "mydsl"

from cfgdrift.cli import main
raise SystemExit(main(["scan", "examples/demo/nginx-like.dsl", "--format", "mydsl"]))
```

## 端到端演示

```bash
# 0) 安装示例包（entry point 注册）
C:/Users/20713/.workbuddy/binaries/python/versions/3.13.12/python.exe -m pip install -e examples/mydsl-parser

# 1) 建基线（显式 --format，或省略后按扩展名 .dsl 自动识别）
cfgdrift scan examples/demo/nginx-like.dsl --format mydsl --save-as-baseline mydsl-demo

# 2) 修改配置：把 examples/demo/nginx-like.dsl 第 4 行 listen 8080 改成 8081

# 3) diff：输出含 file:line 行号定位
cfgdrift diff examples/demo/nginx-like.dsl --baseline mydsl-demo
# [WARN] server[0].listen (nginx-like.dsl:4): 修改 8080 -> 8081
# Summary: added=0 removed=0 modified=1 type_changed=0 ignored=0 total=1 max=WARN
```

## 目录结构

```
examples/mydsl-parser/
├── pyproject.toml              # 插件包元数据 + cfgdrift.parsers entry point
├── README.md                   # 本说明
├── mydsl_parser/
│   ├── __init__.py             # 导出 parse / build_line_map / plugin
│   └── plugin.py               # DSL 解析器 + 行号映射 + 双路径注册
└── tests/
    └── test_mydsl_parser.py    # 解析/行号/集成/注册形态测试
examples/demo/
└── nginx-like.dsl              # 演示用示例配置（行号固定，供 diff 演示）
```

## 运行插件自带测试

```bash
C:/Users/20713/.workbuddy/binaries/python/versions/3.13.12/python.exe -m pytest examples/mydsl-parser/tests/ -q
```
