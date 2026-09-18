# ADR 0005：插件错误契约（声明式领域错误 + 内核兜底分类）

- 状态：已采纳（正在实现）
- 日期：2026-09-10
- 前置：ADR 0001（插件与网关）、`core/errors.py` 错误分类器

## 背景

插件（尤其 MCP 这类进程外插件）对调用方是**黑盒**：调用方无法预知它会报什么错，
只能拿到一段错误文本。模型因此不知道该“改参数重试”“稍后再试”还是“换工具”。

内核已有兜底分类（`classify_error`），但兜底只能猜“类型”（超时/存储/未知），
无法表达插件自己的领域语义。本 ADR 定义**插件声明自己的已知错误**的契约。

## 决策

### 1. 清单声明 errors（可选字段）

```jsonc
"errors": [
  { "code": "invalid_json",  "category": "tool",      "hint": "检查 JSON 语法后重试" },
  { "code": "rate_limited",  "category": "retryable", "hint": "稍后重试" }
]
```

- `code`：唯一标识，`A-Za-z0-9_.-`；同一插件内不可重复；
- `category`：必须来自内核固定集合（config / model / tool / plugin / retryable），
  插件不得自定义分类体系；
- `hint`：面向模型/调用方的可行动提示（可空）；
- `retryable`：可选，必须与 `category == "retryable"` 一致（避免自相矛盾）；
- 未声明 `errors` 的插件照常工作，全部走内核兜底分类。

### 2. 运行时如何对上

- 进程内插件：抛 `DeclaredPluginError(code, message)`，由承载它的包装层
  （本地工具的 `NamespacedTool`，未来的模型/存储包装层）按清单声明补全
  category/hint；未声明的 code 保持默认 `plugin` 类别并在日志里体现；
- MCP 插件：网关检查 `CallToolResult.isError`，按文本前缀
  `[code] ...` 或 `code: ...` 匹配清单声明；匹配失败则抛通用 `ToolError`；
- 声明与兜底叠加：**声明命中用声明，类名/状态码兜底，未知原样上报**。

### 3. 消费方

- loop：工具失败回填文案带 `错误类别 + code + hint`（模型据此选下一步）；
- `retry_async`：`category == "retryable"` 参与重试判定；
- audit/tracing：可按 code 统计“哪个插件最常报哪种错”；
- FastAPI：仍按 category 映射 HTTP 状态码。

## 边界

- 声明是**契约与提示**，不是沙箱保证：插件仍可能抛未声明的错误；
- 声明不替代兜底分类：运行时意外（网络、磁盘、第三方内部）永远存在；
- code 面向机器与模型，hint 面向行动；没有有用 hint 的错误不建议声明。

## 落地清单

1. `core/errors.py`：`DeclaredPluginError`、`resolve_declared_error`、
   category 集合与类型映射；
2. `plugins/loader.py`：校验 `errors` 字段，产出 `DeclaredError` 挂到 manifest；
   本地工具包装层按声明补全；
3. `gateways/mcp_gateway.py`：`isError` 前缀匹配声明；
4. `loop.py`：失败反馈带 code/hint，retryable 判定兼容声明类别；
5. 示例：`plugins/tools/json` 声明并抛出 `invalid_json`；
6. 测试 + README。
