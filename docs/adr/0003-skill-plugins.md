# ADR 0003：Skills 型插件与渐进披露（M3 设计）

- 状态：已采纳（M3 已完成；2026-09-15 完成边界、预算、宿主预载与资源协议强化）
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
  "contract": "skill.v1",
  "description": "代码评审规范与检查清单（一句话，进目录用）",
  "enabled": true,
  "entry": {
    "content": "SKILL.md",
    "resources": [
      {
        "path": "references/security.md",
        "description": "安全检查参考"
      }
    ]
  }
}
```

- `entry.content`：正文文件，相对插件目录；缺省 `SKILL.md`；
- `entry.resources`：可选的只读附属文档；每项包含相对 `path` 和可选
  `description`，模型通过 `use_skill(name, resource=path)` 按需读取；
- `entry.preload`：旧兼容字段，宿主不再直接信任；实际预载只由
  `config/skill.json` 的 `preload` 列表决定；
- `contract` 对 Skill 是必填字段，只能是当前宿主支持的 `skill.vN`；
- `description` 与资源 `description` 必须是单行可打印文本，并受宿主字节预算限制；
- `entry.importFrontmatter` 可选打开 `SKILL.md` frontmatter 导入；它只是兼容层，
  `plugin.json` 始终是内部事实源，同名字段以 `plugin.json` 为准；
- `when_to_use`、`tags`、`compatibility`、`license`、`metadata`、`listing` 和
  frontmatter 导入属于 `skill.v2`；`skill.v1` 保持原语义；
- skill 只允许模型通过专用工具读取，不放进 ToolRegistry（它不是可执行工具）。

### 2. 加载与目录语义

- loader：`SUPPORTED_KINDS` 增加 `skill`；装配时做**零副作用校验**——
  校验清单结构、正文与资源路径必须位于插件目录内、文件存在且满足大小预算；
  不读取正文内容、不执行任何代码；
- 产物：`SkillPlugin { manifest, content_path, resources }` 放进
  `PluginAssembly.skills`；
- 正文采用**惰性读取**：模型请求时才读文件并缓存，启动成本与插件数量解耦；
  宿主可用 `invalidate()` / `reload()` / `reload_resource()` 显式刷新缓存。

正文、单资源、单技能资源总量、启动预载总量均有明确预算。超限时启动或读取
直接报错，不做静默截断；运行时读取会再次检查文件大小，防止装配后文件变化。

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
- 附属资源默认只展示清单，不返回内容；模型需要时再次调用
  `use_skill(name, resource=path)`；
- 全局规则类技能可由宿主在 `config/skill.json` 的 `preload` 列表中选择，
  启动时注入系统提示词。插件清单不能自行开启预载；未知名称或总量超预算时启动失败。
- 系统提示词使用 `maxListingBytes` 限制整个 Skill 目录。名称始终保留；描述按
  `priority` 从小到大分配预算，低优先级 Skill 先被降为 name-only，避免目录挤占
  上下文。

### 4. 权限与手动入口

- `config/skill.json` 支持全局和按 Agent 的 `allow / ask / deny` 规则，规则名支持
  通配符；精确规则优先于通配符，Agent 规则优先于全局规则；
- `deny` Skill 从目录和 `use_skill` 参数枚举中隐藏；每次读取仍在
  `SkillGateway` 和 `UseSkill` 上做二次检查；
- `ask` 通过宿主注入的审批回调决定是否读取；没有审批器时 fail closed；
- CLI 提供 `skill list`、`skill show`、`skill search`，与模型调用共用同一 Gateway。

### 5. 装配与归属

- `SkillGateway` 持有技能目录：`available()`、`get(name)`、`get_resource()`、
  缓存、权限、listing 预算、触发评分与使用统计；
  `UseSkill` 是暴露给模型的内核工具（与 `UsePlugin` 同级）；
- main 装配顺序：assembly 完成后创建 SkillGateway → 注册 UseSkill →
  组装目录段落 → 进入 loop；loop 零改动；
- `use_skill` 与其它工具一视同仁，过 `tool_before / tool_after`
  权限与审计钩子（permission 规则可直接写 `use_skill`）；
- 成功读取会发布 `skill.loaded` / `skill.resource_loaded`，启动预载发布
  `skill.preloaded`，读取失败发布 `skill.load_failed` /
  `skill.resource_failed`；权限路径还会发布 `skill.denied` /
  `skill.approval_required` / `skill.approved` /
  `skill.approval_denied`。`RuntimeSnapshot.skills` 提供 access、priority、
  listing、usage、loaded、bytes、error 等状态。

### 6. 顺带修正提示词组装

系统提示词的插件目录段落只保留两类：

- 可挂载 MCP 插件（模型需要时 `use_plugin`）；
- 可用技能（模型需要时 `use_skill`）。

本地工具的说明不再写进目录段落——它们的 JSON Schema 已经自带
name/description，模型直接可见，重复描述只会浪费上下文。

## 备选方案与取舍

### A. SKILL.md 默认全量注入系统提示词

最简单，但 N 个技能 × 长正文会在每次请求里重复占用上下文，违背渐进披露
目标。拒绝作为默认；仅宿主显式选择的全局规则可注入。

### B. skill 里放可执行代码

Harness 的 skill 本质是文档；可执行能力应由 tool / mcp / hook 插件承担。
如果未来需要“带工具的技能包”，另开 kind（如 `bundle`），不在本 ADR 混入。

### C. YAML frontmatter 承载元数据

外部 Skill 生态常用 frontmatter，因此提供受控导入层：只有显式设置
`importFrontmatter: true` 才读取，且只支持基础标量、数组和一层映射。导入值
不能覆盖 `plugin.json` 中的同名事实字段，也不能新增运行阶段或插件 kind。

## 后果

正面：

- 拖一个 `plugins/skills/<name>/` 目录即可新增技能，无需重启前安装；
- 模型只在需要时读取正文，上下文占用 ≈ 目录条目数 × 一句话；
- 为将来统一“插件目录工具”（MCP/技能共用一份可查询目录）铺路。

代价/待办：

- 技能正文和资源受宿主预算约束，超限明确失败而不是截断；
- 预载技能过多仍会占用上下文，因此必须由宿主在中心配置中显式选择；
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
