# tests/test_skill_gateway.py —— 技能网关：目录、按需读取、use_skill 与 loop 集成
from pathlib import Path

import pytest

from core.hooks import HookGateway
from core.model import ModelAdapter
from core.registry import ToolRegistry
from core.types import ModelResponse, ToolCall
from gateways.skill_gateway import SkillGateway, UseSkill
from loop import run_agent
from plugins.loader import PluginManifest, SkillPlugin, load_skill_plugins


def _skill(
    base: Path,
    name: str,
    description: str,
    content: str,
    preload: bool = False,
) -> SkillPlugin:
    plugin_dir = base / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    content_path = plugin_dir / "SKILL.md"
    content_path.write_text(content, encoding="utf-8")
    manifest = PluginManifest(
        name=name,
        type="skill",
        version="",
        description=description,
        enabled=True,
        directory=plugin_dir,
        entry={"content": "SKILL.md", "preload": preload},
    )
    return SkillPlugin(manifest=manifest, content_path=content_path, preload=preload)


def test_catalog_lists_name_and_description(tmp_path) -> None:
    gateway = SkillGateway([_skill(tmp_path, "review", "评审规范", "# 正文")])
    assert gateway.available() == ["review"]
    assert gateway.catalog() == [("review", "评审规范")]


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


def test_get_unknown_skill_raises(tmp_path) -> None:
    gateway = SkillGateway([_skill(tmp_path, "review", "评审规范", "# 正文")])
    with pytest.raises(KeyError, match="未知技能"):
        gateway.get("missing")


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
    assert "code-review" in by_name
    content = by_name["code-review"].content_path.read_text(encoding="utf-8")
    assert "代码评审技能" in content
