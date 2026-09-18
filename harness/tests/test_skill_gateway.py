# tests/test_skill_gateway.py —— 技能网关：目录、按需读取、use_skill 与 loop 集成
from pathlib import Path

import pytest

from core.events import Event, EventBus
from core.hooks import HookGateway
from core.model import ModelAdapter
from core.registry import ToolRegistry
from core.types import ModelResponse, ToolCall
from gateways.skill_gateway import (
    SkillAccessDenied,
    SkillGateway,
    SkillPermissionPolicy,
    UseSkill,
)
from loop import run_agent
from plugins.loader import PluginManifest, SkillPlugin, SkillResource, load_skill_plugins


def _skill(
    base: Path,
    name: str,
    description: str,
    content: str,
    preload: bool = False,
    resources: tuple[tuple[str, str], ...] = (),
    *,
    when_to_use: str = "",
    tags: tuple[str, ...] = (),
    compatibility: str = "",
    priority: int = 0,
    listing: str = "full",
) -> SkillPlugin:
    plugin_dir = base / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    content_path = plugin_dir / "SKILL.md"
    content_path.write_text(content, encoding="utf-8")
    raw_resources: list[dict[str, str]] = []
    parsed_resources: list[SkillResource] = []
    for resource_path, description in resources:
        path = plugin_dir / resource_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {resource_path}\n", encoding="utf-8")
        raw_resources.append({"path": resource_path, "description": description})
        parsed_resources.append(
            SkillResource(
                path=resource_path,
                description=description,
                resolved_path=path,
            )
        )

    entry: dict[str, object] = {"content": "SKILL.md", "preload": preload}
    if raw_resources:
        entry["resources"] = raw_resources
    manifest = PluginManifest(
        name=name,
        type="skill",
        version="",
        description=description,
        enabled=True,
        directory=plugin_dir,
        entry=entry,
        priority=priority,
    )
    return SkillPlugin(
        manifest=manifest,
        content_path=content_path,
        preload=preload,
        resources=tuple(parsed_resources),
        description=description,
        when_to_use=when_to_use,
        tags=tags,
        compatibility=compatibility,
        listing=listing,
    )


def test_catalog_lists_name_and_description(tmp_path) -> None:
    gateway = SkillGateway([_skill(tmp_path, "review", "评审规范", "# 正文")])
    assert gateway.available() == ["review"]
    assert gateway.catalog() == [("review", "评审规范")]


def test_trigger_evaluation_uses_metadata_and_records_hits(tmp_path) -> None:
    gateway = SkillGateway(
        [
            _skill(
                tmp_path,
                "review",
                "代码质量检查",
                "# review",
                when_to_use="检查 Python 代码的正确性和安全性",
                tags=("python", "security"),
            ),
            _skill(
                tmp_path,
                "release",
                "发布说明",
                "# release",
                when_to_use="整理版本发布记录",
                tags=("release",),
            ),
        ]
    )

    results = gateway.evaluate_triggers("检查 python 安全性")

    assert [result.name for result in results] == ["review"]
    assert gateway.usage("review").trigger_hits == 1


def test_listing_prioritizes_high_priority_descriptions(tmp_path) -> None:
    gateway = SkillGateway(
        [
            _skill(
                tmp_path,
                "alpha",
                "AAAA",
                "# alpha",
                priority=0,
            ),
            _skill(
                tmp_path,
                "beta",
                "BBBBBBBB",
                "# beta",
                priority=10,
            ),
        ],
        max_listing_bytes=22,
    )

    assert gateway.listing() == [("alpha", "AAAA"), ("beta", "")]


def test_name_only_removes_description_from_listing(tmp_path) -> None:
    gateway = SkillGateway(
        [
            _skill(
                tmp_path,
                "review",
                "评审规范",
                "# 正文",
                listing="name-only",
            )
        ]
    )

    assert gateway.catalog() == [("review", "评审规范")]
    assert gateway.listing() == [("review", "")]


def test_permissions_hide_denied_skills_and_allow_exact_override(tmp_path) -> None:
    gateway = SkillGateway(
        [
            _skill(tmp_path, "review", "评审规范", "# 正文"),
            _skill(tmp_path, "release", "发布说明", "# 正文"),
        ],
        policy=SkillPermissionPolicy(
            global_rules=(
                ("*", "deny"),
                ("review", "allow"),
            )
        ),
    )

    assert gateway.available() == ["review"]
    assert [entry.name for entry in gateway.entries()] == ["review"]
    with pytest.raises(SkillAccessDenied):
        gateway.get("release")


def test_permission_policy_rejects_invalid_access() -> None:
    with pytest.raises(ValueError, match="allow, ask, or deny"):
        SkillPermissionPolicy(global_rules=(("*", "maybe"),))


def test_agent_permission_overrides_global_rule(tmp_path) -> None:
    gateway = SkillGateway(
        [_skill(tmp_path, "review", "评审规范", "# 正文")],
        policy=SkillPermissionPolicy(
            global_rules=(("review", "deny"),),
            agent_rules=(
                (
                    "reviewer",
                    (("review", "allow"),),
                ),
            ),
            agent="default",
        ),
    )

    assert gateway.available() == []
    assert gateway.permission("review", agent="reviewer").access == "allow"


@pytest.mark.asyncio
async def test_ask_permission_fails_closed_without_approver(tmp_path) -> None:
    gateway = SkillGateway(
        [_skill(tmp_path, "review", "评审规范", "# secret")],
        policy=SkillPermissionPolicy(global_rules=(("review", "ask"),)),
    )

    result = await UseSkill(gateway).execute(name="review")

    assert "需要批准" in result
    assert "secret" not in result
    assert gateway.usage("review").approval_requests == 1
    assert gateway.usage("review").approvals_denied == 1


@pytest.mark.asyncio
async def test_ask_permission_reads_after_approval(tmp_path) -> None:
    gateway = SkillGateway(
        [_skill(tmp_path, "review", "评审规范", "# approved")],
        policy=SkillPermissionPolicy(global_rules=(("review", "ask"),)),
    )
    tool = UseSkill(gateway, approver=lambda name: name == "review")

    assert await tool.execute(name="review") == "# approved"
    assert gateway.usage("review").approvals_granted == 1


def test_gateway_rejects_duplicate_skill_names(tmp_path) -> None:
    first = _skill(tmp_path / "first", "review", "评审规范", "# 正文")
    second = _skill(tmp_path / "second", "review", "另一份规范", "# 其他正文")

    with pytest.raises(ValueError, match="技能重名"):
        SkillGateway([first, second])


def test_get_reads_lazily_and_caches(tmp_path) -> None:
    plugin_dir = tmp_path / "skills" / "demo"
    plugin_dir.mkdir(parents=True)
    content = plugin_dir / "SKILL.md"
    content.write_text("第一版", encoding="utf-8")

    manifest = PluginManifest(
        name="demo",
        type="skill",
        version="",
        description="演示",
        enabled=True,
        directory=plugin_dir,
        entry={"content": "SKILL.md", "preload": False},
    )
    skill = SkillPlugin(manifest=manifest, content_path=content, preload=False)
    gateway = SkillGateway([skill])

    assert gateway.get("demo") == "第一版"
    content.write_text("第二版", encoding="utf-8")
    assert gateway.get("demo") == "第一版"  # 命中缓存，不重复读盘


def test_reload_refreshes_content_and_loaded_bytes(tmp_path) -> None:
    plugin_dir = tmp_path / "skills" / "demo"
    plugin_dir.mkdir(parents=True)
    content = plugin_dir / "SKILL.md"
    content.write_text("第一版", encoding="utf-8")

    manifest = PluginManifest(
        name="demo",
        type="skill",
        version="",
        description="演示",
        enabled=True,
        directory=plugin_dir,
        entry={"content": "SKILL.md", "preload": False},
    )
    skill = SkillPlugin(manifest=manifest, content_path=content, preload=False)
    gateway = SkillGateway([skill])

    assert gateway.get("demo") == "第一版"
    content.write_text("第二版更长", encoding="utf-8")

    assert gateway.reload("demo") == "第二版更长"
    assert gateway.loaded_bytes("demo") == len("第二版更长".encode())
    assert gateway.invalidate("demo") is True
    assert gateway.has_content("demo") is False


def test_reload_resource_refreshes_resource_cache(tmp_path) -> None:
    gateway = SkillGateway(
        [
            _skill(
                tmp_path,
                "review",
                "评审规范",
                "# 评审正文",
                resources=(("references/security.md", "安全检查"),),
            )
        ]
    )
    resource_path = tmp_path / "review" / "references" / "security.md"

    assert "security.md" in gateway.get_resource("review", "references/security.md")
    resource_path.write_text("# 更新后的安全检查\n", encoding="utf-8")

    assert "更新后的安全检查" in gateway.reload_resource(
        "review",
        "references/security.md",
    )


def test_get_unknown_skill_raises(tmp_path) -> None:
    gateway = SkillGateway([_skill(tmp_path, "review", "评审规范", "# 正文")])
    with pytest.raises(KeyError, match="未知技能"):
        gateway.get("missing")


def test_get_enforces_content_budget(tmp_path) -> None:
    gateway = SkillGateway(
        [_skill(tmp_path, "review", "评审规范", "12345")],
        max_content_bytes=4,
    )

    with pytest.raises(ValueError, match="超过大小预算"):
        gateway.get("review")
    assert gateway.error("review") is not None
    assert gateway.is_loaded("review") is False


def test_get_rechecks_content_path_boundary(tmp_path) -> None:
    skill = _skill(tmp_path, "review", "评审规范", "# 正文")
    outside = tmp_path / "secret.md"
    outside.write_text("secret", encoding="utf-8")
    escaped = SkillPlugin(
        manifest=skill.manifest,
        content_path=outside,
        preload=False,
    )
    gateway = SkillGateway([escaped])

    with pytest.raises(ValueError, match="越出插件目录"):
        gateway.get("review")
    assert gateway.error("review") is not None


@pytest.mark.asyncio
async def test_use_skill_enforces_resource_total_budget(tmp_path) -> None:
    gateway = SkillGateway(
        [
            _skill(
                tmp_path,
                "review",
                "评审规范",
                "# 评审正文",
                resources=(("references/security.md", "安全检查"),),
            )
        ],
        max_resource_total_bytes=1,
    )

    result = await UseSkill(gateway).execute(
        name="review",
        resource="references/security.md",
    )

    assert "资源总大小超过预算" in result
    assert gateway.error("review") is not None


@pytest.mark.asyncio
async def test_use_skill_lists_and_reads_resources(tmp_path) -> None:
    gateway = SkillGateway(
        [
            _skill(
                tmp_path,
                "review",
                "评审规范",
                "# 评审正文",
                resources=(("references/security.md", "安全检查"),),
            )
        ]
    )
    tool = UseSkill(gateway)

    content = await tool.execute(name="review")
    assert "references/security.md" in content
    resource = await tool.execute(name="review", resource="references/security.md")
    assert "# references/security.md" in resource
    assert gateway.loaded_bytes("review") > 0


@pytest.mark.asyncio
async def test_use_skill_publishes_load_events(tmp_path) -> None:
    gateway = SkillGateway(
        [
            _skill(
                tmp_path,
                "review",
                "评审规范",
                "# 评审正文",
                resources=(("references/security.md", "安全检查"),),
            )
        ]
    )
    bus = EventBus()
    events: list[Event] = []
    bus.subscribe("*", events.append)
    tool = UseSkill(gateway, events=bus)

    await tool.execute(name="review")
    await tool.execute(name="review")
    await tool.execute(name="review", resource="references/security.md")
    await tool.execute(name="review", resource="references/security.md")
    await bus.flush()

    assert [event.name for event in events] == [
        "skill.loaded",
        "skill.resource_loaded",
    ]
    assert events[0].payload["bytes"] == len("# 评审正文".encode())
    await bus.stop()


@pytest.mark.asyncio
async def test_use_skill_publishes_failure_events(tmp_path) -> None:
    gateway = SkillGateway(
        [_skill(tmp_path, "review", "评审规范", "12345")],
        max_content_bytes=4,
    )
    bus = EventBus()
    events: list[Event] = []
    bus.subscribe("*", events.append)
    tool = UseSkill(gateway, events=bus)

    await tool.execute(name="review")
    await bus.flush()

    assert [event.name for event in events] == ["skill.load_failed"]
    assert events[0].payload["name"] == "review"
    assert "超过大小预算" in events[0].payload["error"]
    await bus.stop()


@pytest.mark.asyncio
async def test_use_skill_publishes_resource_failure_event(tmp_path) -> None:
    gateway = SkillGateway([_skill(tmp_path, "review", "评审规范", "# 正文")])
    bus = EventBus()
    events: list[Event] = []
    bus.subscribe("*", events.append)
    tool = UseSkill(gateway, events=bus)

    await tool.execute(name="review", resource="references/missing.md")
    await bus.flush()

    assert [event.name for event in events] == ["skill.resource_failed"]
    assert events[0].payload["resource"] == "references/missing.md"
    await bus.stop()


@pytest.mark.asyncio
async def test_use_skill_returns_content_and_unknown_message(tmp_path) -> None:
    gateway = SkillGateway([_skill(tmp_path, "review", "评审规范", "# 评审正文\n- 检查点")])
    tool = UseSkill(gateway)
    registry = ToolRegistry()
    registry.register(tool)

    content = await registry.execute("use_skill", {"name": "review"})
    assert "评审正文" in content

    unknown = await registry.execute("use_skill", {"name": "missing"})
    assert "未知技能" in unknown
    assert "review" in unknown


class FakeModel(ModelAdapter):
    def __init__(self, script):
        self._script = list(script)

    async def complete(self, messages, tool_schemas, on_token=None) -> ModelResponse:
        return self._script.pop(0)


@pytest.mark.asyncio
async def test_loop_reads_skill_then_finishes(tmp_path) -> None:
    skill = _skill(tmp_path, "review", "评审规范", "# 评审\n先检查安全")

    model = FakeModel(
        [
            ModelResponse(
                content="",
                tool_calls=[ToolCall(id="1", name="use_skill", arguments={"name": "review"})],
            ),
            ModelResponse(content="按评审清单检查完毕", tool_calls=[]),
        ]
    )
    tools = ToolRegistry()
    tools.register(UseSkill(SkillGateway([skill])))

    ctx = await run_agent(model, tools, HookGateway(), "你是助手", "评审这段代码")

    assert any("先检查安全" in message.content for message in ctx.messages)
    assert ctx.stop_reason == "done"


def test_repo_skill_plugin_is_discoverable_and_readable() -> None:
    root = Path(__file__).resolve().parents[1] / "plugins"
    skills = load_skill_plugins(root)
    by_name = {skill.manifest.name: skill for skill in skills}
    assert {"code-review", "commit-message"} <= set(by_name)
    content = by_name["code-review"].content_path.read_text(encoding="utf-8")
    assert "代码评审技能" in content
    commit_content = by_name["commit-message"].content_path.read_text(encoding="utf-8")
    assert "提交信息规范技能" in commit_content
