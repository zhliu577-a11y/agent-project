# tests/test_registry.py —— 工具注册表测试（同步）
from core.registry import ToolRegistry
from core.tool import Tool


class Dummy(Tool):
    name = "dummy"
    description = "测试工具"
    parameters = {"type": "object", "properties": {}}

    async def execute(self, **kwargs):
        return "ok"


def test_describe_returns_tool_metadata() -> None:
    registry = ToolRegistry()
    registry.register(Dummy())
    meta = registry.describe("dummy")
    assert meta is not None
    assert meta["name"] == "dummy"
    assert meta["parameters"] == {"type": "object", "properties": {}}
    assert registry.describe("missing") is None
