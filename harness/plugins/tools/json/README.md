# json —— 本地工具插件（含错误契约示例）

日常 JSON 处理，进程内直接执行：

- `json__format`：美化 JSON 文本
- `json__get`：按点路径取值（`a.b.0`，数组用数字下标）

零依赖、纯函数，适合高频轻量场景；权限/审计钩子与其它工具一致。

## 错误契约

`plugin.json` 声明已知错误：

```json
"errors": [
  { "code": "invalid_json", "category": "tool", "hint": "检查 JSON 语法（引号/逗号/括号）后重试" }
]
```

`tool.py` 在解析失败时抛 `DeclaredPluginError("invalid_json", …)`；本地工具包装层
（`NamespacedTool`）按声明补全类别与提示，模型收到的失败反馈形如：

```text
工具 json__format 执行失败: JSON 解析失败: …
错误类别: tool，code: invalid_json。提示: 检查 JSON 语法（引号/逗号/括号）后重试。
```
