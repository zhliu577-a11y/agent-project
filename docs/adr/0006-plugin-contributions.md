# ADR 0006：功能包 contribution 与受控 kind 注册

- 状态：已采纳
- 日期：2026-09-15

## 背景

最初的插件清单只描述一个插件、一个 `type`：

```json
{
  "name": "math",
  "type": "mcp",
  "entry": {
    "command": "python",
    "args": ["server.py"]
  }
}
```

这种模型发现逻辑简单，但无法把一个需要多种能力的特性作为一个整体交付。
例如，一个代码质量包可能同时需要 skill、生命周期 hook 和本地 tool，并要求
它们一起安装、启停和版本化。

## 决策

发现阶段把单插件清单和功能包清单统一展开为 `PluginContribution`。

单插件格式保持向后兼容：

```json
{
  "name": "text",
  "type": "tool",
  "entry": {
    "module": "tool.py",
    "factory": "create_tools"
  }
}
```

功能包清单可以一次贡献多个能力：

```json
{
  "apiVersion": "1",
  "name": "code-quality",
  "version": "1.0.0",
  "contributes": [
    {
      "id": "lint",
      "kind": "skill",
      "entry": {
        "content": "skills/lint/SKILL.md"
      }
    },
    {
      "id": "format-hook",
      "kind": "hook",
      "entry": {
        "module": "hooks/hook.py",
        "factory": "create_hook"
      }
    }
  ]
}
```

包内 contribution 统一命名为 `<包名>--<contribution-id>`，所有路径相对
包根目录解析。

Harness 持有 `KindHandler` 注册表：

```python
register_kind(
    KindHandler(
        kind="tool",
        load=load_tool_plugin,
        apply=_apply_tool,
    )
)
```

普通插件只能引用受信任 Harness 代码已经注册的 kind，不能通过清单自行增加
核心执行阶段。

## 后果

- 原有单插件清单无需修改即可继续工作。
- 功能包可以作为一个整体安装、禁用和版本化多项能力。
- `discover_contributions()` 暴露归一化后的能力单元。
- `discover_plugins()` 保留为兼容投影，现有调用方和测试无需立即迁移。
- 新增 kind 只需要注册一个受信任的 `KindHandler`，不再分别修改 discovery、
  assembly 和 reporting 分支。
- 如果新 kind 需要新的 agent loop 阶段，仍然必须修改核心 loop；kind 注册表
  只管理宿主侧的集成契约。
