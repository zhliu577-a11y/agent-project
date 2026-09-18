# permission —— 钩子插件示例（权限闸门）

`type: "hook"` 的权限策略插件：在 `tool_before` 位置表态
`allow / ask / deny`，由 `HookGateway` 按 `deny > ask > allow` 折叠。

## 配置

策略文件 `permission.json` 随插件目录走：

```json
{
  "default": "allow",
  "rules": [
    { "tool": "filesystem__delete_file", "mode": "deny" },
    { "tool": "filesystem__write_file", "mode": "ask" }
  ]
}
```

- `tool` 支持通配符（如 `*__shell`、`filesystem__*`），工具名是命名空间后的名字；
- `mode` 只能是 `allow / ask / deny`，写错启动即报错；
- `ask` 的确认提示由钩子网关统一执行一次（Web 模式下默认按拒绝处理）；
- `priority: 10`：比普通钩子先表态（越小越先执行）。

## 行为

被拒绝的工具不会执行，拒绝原因回填给模型；本地工具与 MCP 工具一视同仁。
更换策略不需要改代码：编辑 permission.json 后重启即可。
