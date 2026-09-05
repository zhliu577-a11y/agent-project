# text —— 本地工具插件示例

展示了 `type: "tool"` 的写法：插件目录里的 `tool.py` 实现
`core.tool.Tool`，工厂 `create_tools` 返回一个或多个 Tool。

加载后工具名会自动带上插件命名空间：

- `text__slugify`
- `text__count_words`

与 MCP 插件的区别：本地工具在启动时直接注册进 ToolRegistry，
模型从第一轮就能调用，不需要 `use_plugin` 挂载，也没有子进程。
