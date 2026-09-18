# math —— MCP 插件示例（含错误契约）

`type: "mcp"` 的最小示例：`server.py` 用 MCP SDK 暴露一个 `calculate` 工具，
通过 stdio 子进程运行；启动时只读清单，模型调用 `use_plugin("math")` 后才挂载。

## 工具

- `math__calculate`：安全计算数学表达式（只允许数字与 `+ - * / ** %` 和括号）

## 错误契约

`plugin.json` 声明了本插件已知的领域错误：

```json
"errors": [
  { "code": "unsupported_expression", "category": "tool",
    "hint": "只支持数字与 + - * / ** % 和括号，请修改表达式后重试" }
]
```

服务器抛 MCP SDK 的 `ToolError`（它的消息会被保留；普通异常会被 SDK 换成
通用文案），错误文本里出现 **`[code]`**（或 `code:`）时，网关会按声明补全
category/hint。SDK 可能在外面包一层 `Error executing tool …`，因此匹配是
**任意位置搜索**。模型收到的失败反馈形如：

```text
工具 math__calculate 执行失败: [unsupported_expression] 表达式无法计算: …
错误类别: tool，code: unsupported_expression。提示: 只支持数字与 …
```

匹配不到声明的 `isError` 会退化为通用 `ToolError`；第三方 MCP 服务器若不按
该前缀约定返回，声明不生效（仍走兜底分类）。
