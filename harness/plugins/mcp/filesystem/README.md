# filesystem - 仓库内置 MCP 插件

该插件通过 `server.py` 提供文件系统工具，不依赖 `npx`、npm 缓存、网络或系统
`PATH` 中的 `python`。加载器会把 `"command": "python"` 替换为当前 Runtime
使用的虚拟环境解释器。

默认允许目录是 `harness/data/workspace`。也可以用
`HARNESS_FILESYSTEM_ROOT` 覆盖，或在 `entry.args` 中传入一个或多个根目录。

提供的工具：

- `read_text_file`
- `write_file`
- `list_directory`
- `create_directory`
- `move_file`
- `get_file_info`
- `list_allowed_directories`

所有路径都会先解析并校验，任何 `..`、绝对路径或符号链接逃逸都会被拒绝。
`write_file` / `move_file` 仍由 `plugins/hooks/permission` 执行 ask/deny
权限确认，模型侧工具名为 `filesystem__<tool>`。
