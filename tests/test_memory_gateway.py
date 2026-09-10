# tests/test_memory_gateway.py —— 长期记忆：JSONL 存储、网关与工具（不联网）
from pathlib import Path

import pytest

from core.registry import ToolRegistry
from gateways.memory_gateway import (
    ForgetTool,
    MemoryGateway,
    RecallTool,
    RememberTool,
    UpdateNoteTool,
)
from plugins.loader import load_memory_plugins

REPO_PLUGINS = Path(__file__).resolve().parents[1] / "plugins"


def _memory_gateway(tmp_path: Path, monkeypatch) -> MemoryGateway:
    monkeypatch.setenv("MEMORY_DATA_DIR", str(tmp_path))
    plugin = next(p for p in load_memory_plugins(REPO_PLUGINS) if p.manifest.name == "jsonl")
    return MemoryGateway(plugin.create())


def _sqlite_gateway(tmp_path: Path, monkeypatch) -> MemoryGateway:
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    plugin = next(p for p in load_memory_plugins(REPO_PLUGINS) if p.manifest.name == "sqlite")
    return MemoryGateway(plugin.create())


@pytest.mark.asyncio
async def test_memory_remember_recall_forget_roundtrip(tmp_path, monkeypatch) -> None:
    gateway = _memory_gateway(tmp_path, monkeypatch)

    note = await gateway.remember("用户叫小张", tags=["user", "preference"])
    assert note.id
    assert (await gateway.recall("小张"))[0].id == note.id
    assert (await gateway.recall("preference"))[0].id == note.id  # 标签可搜索

    updated = await gateway.update(note.id, content="用户叫小李", tags=["user"])
    assert updated is not None and updated.content == "用户叫小李"
    assert (await gateway.recall("小李"))[0].id == note.id
    assert await gateway.recall("preference") == []  # 旧标签已被替换

    assert await gateway.forget(note.id) is True
    assert await gateway.forget(note.id) is False
    assert await gateway.recall() == []


@pytest.mark.asyncio
async def test_memory_tools_work_through_registry(tmp_path, monkeypatch) -> None:
    gateway = _memory_gateway(tmp_path, monkeypatch)
    registry = ToolRegistry()
    registry.register(RememberTool(gateway))
    registry.register(RecallTool(gateway))
    registry.register(ForgetTool(gateway))
    registry.register(UpdateNoteTool(gateway))

    saved = await registry.execute("remember", {"content": "项目用 ruff", "tags": ["project"]})
    assert saved.startswith("已记住")

    found = await registry.execute("recall", {"query": "ruff"})
    assert "项目用 ruff" in found

    note_id = found.split("[")[1].split("]")[0]
    updated = await registry.execute(
        "update_note", {"note_id": note_id, "content": "项目用 ruff（2026 版）"}
    )
    assert "已更新" in updated
    assert "2026" in await registry.execute("recall", {"query": "2026"})
    removed = await registry.execute("forget", {"note_id": note_id})
    assert "已删除" in removed


@pytest.mark.asyncio
async def test_sqlite_backend_roundtrip_and_search(tmp_path, monkeypatch) -> None:
    gateway = _sqlite_gateway(tmp_path, monkeypatch)

    first = await gateway.remember("用户叫小张", tags=["user", "preference"])
    second = await gateway.remember("项目用 ruff", tags=["project"])
    updated = await gateway.update(second.id, tags=["project", "style"])
    assert updated is not None and updated.tags == ["project", "style"]
    assert (await gateway.recall("小张"))[0].id == first.id
    assert (await gateway.recall("project"))[0].id == second.id
    assert len(await gateway.recall()) == 2

    assert await gateway.forget(first.id) is True
    assert await gateway.forget(first.id) is False
    assert len(await gateway.recall()) == 1


def test_repo_offers_production_and_readable_memory_backends() -> None:
    names = {plugin.manifest.name for plugin in load_memory_plugins(REPO_PLUGINS)}
    assert {"sqlite", "jsonl"} <= names


def _vector_gateway(tmp_path: Path, monkeypatch) -> MemoryGateway:
    monkeypatch.setenv("MEMORY_VECTOR_DB_PATH", str(tmp_path / "vector.db"))
    monkeypatch.delenv("EMBEDDING_PROVIDER", raising=False)  # 默认 debug，离线可跑
    plugin = next(p for p in load_memory_plugins(REPO_PLUGINS) if p.manifest.name == "vector")
    return MemoryGateway(plugin.create())


@pytest.mark.asyncio
async def test_vector_backend_semantic_recall_orders_by_similarity(tmp_path, monkeypatch) -> None:
    gateway = _vector_gateway(tmp_path, monkeypatch)

    ruff = await gateway.remember("项目使用 ruff 做代码风格检查", tags=["project"])
    weather = await gateway.remember("今天天气不错适合散步", tags=["life"])

    hits = await gateway.recall("ruff 代码风格")
    assert hits and hits[0].id == ruff.id  # 语义上相关的一条排最前
    assert weather.id not in [note.id for note in hits]

    updated = await gateway.update(weather.id, content="项目使用 ruff（天气无关）")
    assert updated is not None
    assert await gateway.forget(ruff.id) is True
    hits = await gateway.recall("ruff")
    assert hits and hits[0].id == weather.id
    assert len(await gateway.recall()) == 1
