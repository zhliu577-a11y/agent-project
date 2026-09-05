# ADR 0003：Skills 型插件与渐进披露（M3 设计）

- 状态：已采纳（M3 已完成）
- 日期：2026-09-05
- 前置：ADR 0002（kind 注册表 + PluginAssembly）已落地

## 背景

仓库已具备 hook / mcp / tool / model 四类插件，装配入口统一。剩下两个问题：

1. “技能”还没有形态。很多能力不是工具调用，而是**一段模型该遵守/参考的操作说明**
   （评审规范、代码风格、任务流程）。它们应当能像插件一样被拖入目录；
2. 系统提示词目前把本地工具、MCP 插件描述**全部硬拼进去**。插件一多会挤占上下文；
   技能正文更不该默认全量注入——大多数技能一次任务根本用不上。

本 ADR 设计 `type: "skill"` 插件与“目录渐进披露”机制。

## 决策

### 1. Skill 是什么形态

一个 skill 是**纯内容插件**：不执行代码、不注册工具，只是一份 Markdown 指令
加上清单元数据：

```text
plugins/skills/<name>/
├── plugin.json
└── SKILL.md            # 正文（默认文件名，清单可覆盖）
```

```json
{
  "name": "code-review",
  "type": "skill",
  "version": "1.0.0",
  "description": "代码评审规范与检查清单（一句话，进目录用）",
  "enabled": true,
  "entry": {
    "content": "SKILL.md",
    "preload": false
  }
}
```

- `entry.content`：正文文件，相对插件目录；缺省 `SKILL.md`；
- `entry.preload`：是否把正文预载进系统提示词，默认 `false`（见第 3 节）；
- 不引入 YAML frontmatter——元数据统一放 `plugin.json`，保持单一事实来源；
- skill 只允许模型通过专用工具读取，不放进 ToolRegistry（它不是可执行工具）。

### 2. 加载与目录语义

- loader：`SUPPORTED_KINDS` 增加 `skill`；装配时只做**零副作用校验**——
  校验清单结构、`content` 文件存在且可读；不读取正文、不执行任何代码；
- 产物：`SkillPlugin { manifest, content_path }` 放进 `PluginAssembly.skills`；
- 正文采用**惰性读取**：模型请求时才读文件并缓存，启动成本与插件数量解耦。

### 3. 目录工具读什么、默认全量读吗

**默认不全量读。** 系统提示词里只出现“目录条目”：

```text
可用技能（需要时调用 use_skill 读取完整说明）：
- code-review: 代码评审规范与检查清单
```

- 目录条目 = 插件名 + 一句 description（不进正文）；
- 模型决定需要某技能时，调用 `use_skill(name)`；网关返回该插件
  `SKILL.md` 全文，作为工具结果回填给模型；
- `use_skill` 的参数枚举所有可用技能名；未知技能返回“未知 + 可用列表”；
- 正文读取后缓存，重复调用不重复读盘；
- 特殊情况：全局规则类技能可显式 `"preload": true` 在启动时注入系统提示词，
  但默认关闭，并在文档里提示尽量少用。

### 4. 装配与归属

- `SkillGateway` 持有技能目录：`available()`、`get(name)`、缓存与错误提示；
  `UseSkill` 是暴露给模型的内核工具（与 `UsePlugin` 同级）；
- main 装配顺序：assembly 完成后创建 SkillGateway → 注册 UseSkill →
  组装目录段落 → 进入 loop；loop 零改动；
- `use_skill` 与其它工具一视同仁，过 `tool_before / tool_after`
  权限与审计钩子（permission 规则可直接写 `use_skill`）。

### 5. 顺带修正提示词组装

系统提示词的插件目录段落只保留两类：

- 可挂载 MCP 插件（模型需要时 `use_plugin`）；
- 可用技能（模型需要时 `use_skill`）。

本地工具的说明不再写进目录段落——它们的 JSON Schema 已经自带
name/description，模型直接可见，重复描述只会浪费上下文。

## 备选方案与取舍

### A. SKILL.md 默认全量注入系统提示词

最简单，但 N 个技能 × 长正文会在每次请求里重复占用上下文，违背渐进披露
目标。拒绝作为默认；仅 `preload: true` 的全局规则可显式注入。

### B. skill 里放可执行代码

Harness 的 skill 本质是文档；可执行能力应由 tool / mcp / hook 插件承担。
如果未来需要“带工具的技能包”，另开 kind（如 `bundle`），不在本 ADR 混入。

### C. YAML frontmatter 承载元数据

会增加解析器与两处元数据来源；`plugin.json` 已统一承担，拒绝。

## 后果

正面：

- 拖一个 `plugins/skills/<name>/` 目录即可新增技能，无需重启前安装；
- 模型只在需要时读取正文，上下文占用 ≈ 目录条目数 × 一句话；
- 为将来统一“插件目录工具”（MCP/技能共用一份可查询目录）铺路。

代价/待办：

- 技能正文质量依赖插件作者自控篇幅（文档给出建议长度，暂无硬截断）；
- 预载技能过多会重新引入上下文膨胀，需在文档与示例中约束；
- `use_skill` 返回的正文是一次性工具结果；若任务跨多轮，模型需自行在
  后续请求中携带要点（与现有工具结果一致，不做特殊注入）。

## 落地清单

1. loader：`skill` kind + `SkillPlugin` + assembly 字段；
2. `gateways/skill_gateway.py`：SkillGateway + UseSkill；
3. main：创建网关、注册 UseSkill、重排提示词目录段落；
4. 示例插件 `plugins/skills/code-review/`；
5. 测试（loader / gateway / use_skill / loop 集成）+ README/ADR 更新；
6. 回归 ruff + pytest。

落地记录：清单全部完成（示例 `plugins/skills/code-review`，测试 66 个通过）。
