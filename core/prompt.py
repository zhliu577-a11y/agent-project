# core/prompt.py —— Prompt 模板管理：系统提示词由目录数据统一组装
#
# CLI（main.py）调用本函数；未来其他前端共用同一提示词组装逻辑。


def build_system_prompt(
    *,
    mcp: list[tuple[str, str]],
    skills: list[tuple[str, str]],
    preloads: list[tuple[str, str]],
) -> str:
    """按目录数据组装系统提示词。

    mcp/skills：目录条目列表（名字, 一句话描述）；
    preloads：需要全量注入的全局技能（名字, 正文）。
    """
    mcp_catalog = "\n".join(f"- {name}: {desc}" for name, desc in mcp) or "（暂无）"
    skill_catalog = "\n".join(f"- {name}: {desc}" for name, desc in skills)

    prompt = (
        "你是一个乐于助人的助手。工具名格式为 <插件名>__<工具名>；"
        "本地工具启动即就绪，说明见各自的函数 schema。\n"
        "需要某个 MCP 插件时，先调用 use_plugin 挂载它，挂载成功后再调用其工具。\n"
        f"可挂载的 MCP 插件：\n{mcp_catalog}\n"
        "需要某项技能时，先调用 use_skill 读取完整说明，再按其执行。\n"
        f"可用技能：\n{skill_catalog or '（暂无）'}"
    )
    if preloads:
        sections = "\n\n".join(f"### 技能 {name}\n{content}" for name, content in preloads)
        prompt += f"\n\n以下为预载技能（全局规则，必须遵守）：\n{sections}"
    return prompt
