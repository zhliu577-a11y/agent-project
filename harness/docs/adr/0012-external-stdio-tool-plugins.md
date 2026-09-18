# ADR 0012：外部进程工具插件与受控 runtime

- 状态：已接受
- 日期：2026-09-15
- 前置：ADR 0002（kind 注册表）、ADR 0007（受控 kind 与 contribution）

## 背景

现有的 `tool` kind 只支持 Python `module + factory`，工具与 Harness 在同一
进程内运行。TypeScript / Node 生态中的部分能力需要独立进程和更自然的语言
工具链，但直接让 `plugin.json` 注册任意运行时实现会绕过受信任的 `KindHandler`
与生命周期边界。

需要区分两件事：

- **kind 仍是受控的**：插件不能通过清单新增执行阶段；
- **kind 的实现运行时可以受控扩展**：同一个 `tool` kind 可以由受信任 Loader
  按声明选择进程内 Python 或外部进程。

## 决策

### 1. `runtime` 是 entry 内的受控字段

Python 进程内插件保持原契约，`runtime` 缺省为 `python`：

```json
{
  "module": "tool.py",
  "factory": "create_tools"
}
```

外部工具插件使用：

```json
{
  "runtime": "node",
  "protocol": "jsonrpc-stdio",
  "command": ["node", "dist/index.js"],
  "tools": [
    {
      "name": "open_url",
      "description": "Open a URL.",
      "parameters": {
        "type": "object",
        "properties": { "url": { "type": "string" } },
        "required": ["url"]
      }
    }
  ]
}
```

当前只允许 `runtime: node | process`。`process` 是其它语言（Go、Deno、Bun、
Rust 等）的通用启动方式；它不会自动安装依赖。第一版外部 runtime 只适用于
`tool` kind，不允许插件用该字段替换 model、session、memory、context 等
进程内对象。

### 2. 第一版协议固定为 `jsonrpc-stdio`

- stdout：一行一个 JSON-RPC 2.0 响应；
- stderr：诊断日志，由宿主转发；
- 调用方法：`tools/call`；
- `params`：`{"name": "...", "arguments": {...}}`；
- result：字符串，或 `{"content": [{"type": "text", "text": "..."}]}`；
- error：JSON-RPC `error.code` 为字符串且命中 `errors` 声明时，映射为
  `DeclaredPluginError`，否则映射为 `ToolError`。

### 3. 工具目录静态声明，进程按需启动

`entry.tools` 在 discovery 阶段校验并注册 schema，因此启动时不执行外部代码。
进程在第一次工具调用时创建；同一插件的全部工具共享一个
`StdioJsonRpcHost`。Runtime 关闭时 terminate 进程并等待回收。

第一版不实现动态 `tools/list`，避免把异步进程握手引入同步 discovery/装配
边界。需要动态工具目录、远程 HTTP 或跨客户端复用时，应使用 `mcp` kind。

## 结果

- 核心 loop、ToolRegistry、权限钩子和错误分类保持不变；
- Python 高频工具仍无子进程开销；
- TypeScript/Node 工具可以通过稳定的进程边界接入；
- `runtime` 与 `protocol` 的值由 Harness 代码控制，插件无法自行注册；
- 外部进程属于可信执行边界，清单校验不是沙箱，依赖仍需插件自行提供。

未来增加其他外部协议或语言时，应新增受控协议实现和测试，而不是让 manifest
执行任意 Python 导入路径。
