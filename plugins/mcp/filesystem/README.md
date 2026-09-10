# filesystem —— 外部 MCP 插件（第三方服务）

本插件不包含服务器代码，`plugin.json` 直接启动官方包：

```text
cmd.exe /c npx -y @modelcontextprotocol/server-filesystem <沙箱目录>
```

用途：读写本地文件。危险操作（write / edit / move / delete）由
`plugins/hooks/permission` 的规则改成 `ask/deny`，工具名带命名空间
（如 `filesystem__write_file`）。

## 错误说明

第三方服务器不会输出本项目约定的 `[code] 错误文本` 前缀，因此这里**不声明
`errors`**——它返回 `isError` 时，网关按通用 `ToolError` 处理并记录原始文本。
如果你自己包装一层 MCP 服务器，可以采用约定前缀来获得 code/hint 支持。
