# tests/test_memory_gateway.py —— 长期记忆：JSONL 存储、网关与工具（不联网）
import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from core.errors import PluginError
from core.memory import (
    MemoryNote,
    MemoryQuery,
    MemoryRecallPort,
    MemoryRecord,
    MemoryStore,
    MemoryWriteDecision,
)
from core.memory_extraction import (
    MemoryCandidate,
    MemoryExtractionRequest,
    MemoryExtractionResult,
)
from core.registry import ToolRegistry
from core.session import SessionMetadata, SessionSnapshot
from core.types import Message
from gateways.memory_extraction_gateway import MemoryExtractionGateway
from gateways.memory_gateway import (
    ForgetTool,
    MemoryGateway,
    RecallTool,
    RememberTool,
    UpdateNoteTool,
)
from plugins.loader import (
    load_embedding_plugins,
    load_memory_extractor_plugins,
    load_memory_index_plugins,
    load_memory_plugins,
    load_memory_policy_plugins,
    load_memory_retriever_plugins,
)
from plugins.services import RuntimeServices, candidate_service_names

REPO_PLUGINS = Path(__file__).resolve().parents[1] / "plugins"


def _memory_gateway(tmp_path: Path, monkeypatch) -> MemoryGateway:
    monkeypatch.setenv("MEMORY_DATA_DIR", str(tmp_path))
    plugin = next(p for p in load_memory_plugins(REPO_PLUGINS) if p.manifest.name == "jsonl")
    return MemoryGateway(plugin.create())


def test_memory_gateway_satisfies_read_only_recall_port(tmp_path, monkeypatch) -> None:
    assert isinstance(_memory_gateway(tmp_path, monkeypatch), MemoryRecallPort)


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


def test_repo_offers_replaceable_memory_indexes_and_policies() -> None:
    index_names = {plugin.manifest.name for plugin in load_memory_index_plugins(REPO_PLUGINS)}
    retriever_names = {
        plugin.manifest.name for plugin in load_memory_retriever_plugins(REPO_PLUGINS)
    }
    policy_names = {plugin.manifest.name for plugin in load_memory_policy_plugins(REPO_PLUGINS)}
    extractor_names = {
        plugin.manifest.name for plugin in load_memory_extractor_plugins(REPO_PLUGINS)
    }
    assert {"lexical", "recent"} <= index_names
    assert {"lexical", "recent", "store-native"} <= retriever_names
    assert {"default", "strict"} <= policy_names
    assert "explicit" in extractor_names


def _jsonl_store(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MEMORY_DATA_DIR", str(tmp_path))
    return next(
        plugin for plugin in load_memory_plugins(REPO_PLUGINS) if plugin.manifest.name == "jsonl"
    ).create()


def _default_policy():
    return next(
        plugin
        for plugin in load_memory_policy_plugins(REPO_PLUGINS)
        if plugin.manifest.name == "default"
    ).create()


@pytest.mark.asyncio
async def test_memory_policy_skips_normalized_duplicates(tmp_path, monkeypatch) -> None:
    store = _jsonl_store(tmp_path, monkeypatch)
    gateway = MemoryGateway(
        store,
        policy=_default_policy(),
    )

    first = await gateway.remember("Use ruff for Python linting")
    duplicate = await gateway.remember("  use   RUFF for Python linting  ")

    assert duplicate.id == first.id
    assert [record.id for record in await store.list_notes()] == [first.id]


@pytest.mark.asyncio
async def test_memory_policy_supersedes_explicit_target(tmp_path, monkeypatch) -> None:
    store = _jsonl_store(tmp_path, monkeypatch)
    gateway = MemoryGateway(store, policy=_default_policy())

    old = await gateway.remember("Use black for formatting")
    new = await gateway.remember("Use ruff for formatting", supersedes=old.id)
    records = {record.id: record for record in await store.list_notes()}

    assert records[old.id].status == "superseded"
    assert new.supersedes == old.id
    assert await gateway.recall("black") == []
    assert [record.id for record in await gateway.recall("ruff")] == [new.id]


@pytest.mark.asyncio
async def test_memory_maintenance_expires_records_without_deleting_them(
    tmp_path,
    monkeypatch,
) -> None:
    store = _jsonl_store(tmp_path, monkeypatch)
    gateway = MemoryGateway(store, policy=_default_policy())

    expired = await gateway.remember(
        "Temporary deployment marker",
        expires_at="2000-01-01T00:00:00+00:00",
    )
    active = await gateway.remember("Durable project rule")

    result = await gateway.maintain()
    records = {record.id: record for record in await store.list_notes()}

    assert result == {"checked": 2, "expired": 1}
    assert records[expired.id].status == "expired"
    assert records[active.id].status == "active"
    assert await gateway.recall("Temporary") == []


@pytest.mark.asyncio
async def test_memory_recall_persists_access_statistics(tmp_path, monkeypatch) -> None:
    store = _jsonl_store(tmp_path, monkeypatch)
    gateway = MemoryGateway(store)

    note = await gateway.remember("Use ruff")
    await gateway.recall("ruff")
    await gateway.recall("ruff")
    stored = (await store.list_notes())[0]

    assert stored.id == note.id
    assert stored.metadata["accessCount"] == 2
    assert stored.last_accessed_at


@pytest.mark.asyncio
async def test_memory_identity_is_propagated_and_enforced(tmp_path, monkeypatch) -> None:
    store = _jsonl_store(tmp_path, monkeypatch)
    gateway = MemoryGateway(
        store,
        default_scope="agent",
        default_owner_id="owner-a",
        default_agent_id="agent-a",
        default_tenant_id="tenant-a",
        allowed_scopes={"agent"},
        allowed_owner_ids={"owner-a"},
        allowed_agent_ids={"agent-a"},
        allowed_tenant_ids={"tenant-a"},
    )

    note = await gateway.remember("inside")
    assert note.scope == "agent"
    assert note.owner_id == "owner-a"
    assert note.agent_id == "agent-a"
    assert note.tenant_id == "tenant-a"

    with pytest.raises(PluginError, match="agent"):
        await gateway.remember("other agent", agent_id="agent-b")
    with pytest.raises(PluginError, match="tenant"):
        await gateway.recall(tenant_id="tenant-b")

    other = MemoryGateway(
        store,
        default_scope="agent",
        default_owner_id="owner-a",
        default_agent_id="agent-b",
        default_tenant_id="tenant-a",
    )
    assert await other.recall("") == []


class _StaticExtractor:
    async def extract(self, request: MemoryExtractionRequest) -> MemoryExtractionResult:
        assert request.agent_id == "agent-a"
        return MemoryExtractionResult(
            candidates=[
                MemoryCandidate(
                    content="Use ruff for linting",
                    tags=["auto", "preference"],
                    kind="preference",
                    confidence=0.9,
                    metadata={"evidenceMessageIds": [request.new_messages[0].id]},
                )
            ]
        )


@pytest.mark.asyncio
async def test_memory_extraction_gateway_writes_candidates_through_gateway(
    tmp_path,
    monkeypatch,
) -> None:
    store = _jsonl_store(tmp_path, monkeypatch)
    memory = MemoryGateway(
        store,
        policy=_default_policy(),
        default_agent_id="agent-a",
    )
    extraction = MemoryExtractionGateway(_StaticExtractor(), memory)
    message = Message(role="user", content="remember that I use ruff")
    snapshot = SessionSnapshot(
        session_id="test",
        messages=[message],
        metadata=SessionMetadata(revision=1, message_count=1),
    )

    written = await extraction.process_turn(snapshot, [message])
    records = await store.list_notes()

    assert written == [records[0].id]
    assert records[0].content == "Use ruff for linting"
    assert records[0].source == "extracted"
    assert records[0].agent_id == "agent-a"


@pytest.mark.asyncio
async def test_recent_index_orders_candidates_by_update_time() -> None:
    index = next(
        plugin
        for plugin in load_memory_index_plugins(REPO_PLUGINS)
        if plugin.manifest.name == "recent"
    ).create()
    older = MemoryRecord(
        id="older",
        content="ruff project rule",
        tags=["project"],
        created_at="2026-01-01T00:00:00+00:00",
    )
    newer = MemoryRecord(
        id="newer",
        content="ruff project rule updated",
        tags=["project"],
        created_at="2026-01-02T00:00:00+00:00",
        updated_at="2026-02-01T00:00:00+00:00",
    )
    await index.rebuild([older, newer])

    hits = await index.search(MemoryQuery(text="ruff", limit=10))

    assert [record.id for record in hits] == ["newer", "older"]


@pytest.mark.asyncio
async def test_strict_policy_rejects_weak_writes_and_ranks_by_confidence(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("MEMORY_DATA_DIR", str(tmp_path))
    store = next(
        p for p in load_memory_plugins(REPO_PLUGINS) if p.manifest.name == "jsonl"
    ).create()
    policy = next(
        p for p in load_memory_policy_plugins(REPO_PLUGINS) if p.manifest.name == "strict"
    )
    gateway = MemoryGateway(store, policy=policy.create())

    await gateway.remember("tiny", confidence=0.9, importance=0.9)
    await gateway.remember("well formed but uncertain", confidence=0.2)
    confident = await gateway.remember(
        "well formed confident fact",
        confidence=0.95,
        importance=0.3,
    )
    important = await gateway.remember(
        "well formed important fact",
        confidence=0.7,
        importance=0.9,
    )

    assert [record.id for record in await store.list_notes()] == [
        confident.id,
        important.id,
    ]
    assert [record.id for record in await gateway.recall("fact")] == [
        confident.id,
        important.id,
    ]


@pytest.mark.asyncio
async def test_memory_gateway_uses_replaceable_index_and_policy(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MEMORY_DATA_DIR", str(tmp_path))
    store = next(
        p for p in load_memory_plugins(REPO_PLUGINS) if p.manifest.name == "jsonl"
    ).create()
    index = next(p for p in load_memory_index_plugins(REPO_PLUGINS) if p.manifest.name == "lexical")
    policy = next(
        p for p in load_memory_policy_plugins(REPO_PLUGINS) if p.manifest.name == "default"
    )
    gateway = MemoryGateway(store, index=index.create(), policy=policy.create())

    record = await gateway.remember("prefer ruff", tags=["style"], importance=0.9)
    hits = await gateway.recall("ruff")

    assert [hit.id for hit in hits] == [record.id]
    assert await gateway.forget(record.id) is True
    assert await gateway.recall("ruff") == []


@pytest.mark.asyncio
async def test_memory_gateway_uses_replaceable_retriever(tmp_path, monkeypatch) -> None:
    store = _jsonl_store(tmp_path, monkeypatch)
    retriever = next(
        plugin
        for plugin in load_memory_retriever_plugins(REPO_PLUGINS)
        if plugin.manifest.name == "lexical"
    ).create()
    gateway = MemoryGateway(store, retriever=retriever)

    record = await gateway.remember("prefer ruff", tags=["style"])

    assert [hit.id for hit in await gateway.recall("ruff")] == [record.id]
    assert await gateway.forget(record.id) is True
    assert await gateway.recall("ruff") == []


@pytest.mark.asyncio
async def test_memory_gateway_enforces_scope_and_owner_acl(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MEMORY_DATA_DIR", str(tmp_path))
    store = next(
        p for p in load_memory_plugins(REPO_PLUGINS) if p.manifest.name == "jsonl"
    ).create()
    gateway = MemoryGateway(
        store,
        default_scope="agent",
        default_owner_id="agent-a",
        allowed_scopes={"agent"},
        allowed_owner_ids={"agent-a"},
    )

    await gateway.remember("inside", tags=[])
    with pytest.raises(PluginError, match="scope"):
        await gateway.remember("outside", tags=[], scope="project")
    with pytest.raises(PluginError, match="owner"):
        await gateway.remember("other", tags=[], owner_id="agent-b")

    assert [record.content for record in await store.list_notes()] == ["inside"]

    other_owner = MemoryGateway(store, default_scope="agent", default_owner_id="agent-b")
    await other_owner.remember("other owner", tags=[])
    assert [record.content for record in await gateway.recall("")] == ["inside"]


def _vector_gateway(tmp_path: Path, monkeypatch) -> tuple[MemoryGateway, MemoryStore]:
    monkeypatch.setenv("MEMORY_VECTOR_DB_PATH", str(tmp_path / "vector.db"))
    monkeypatch.delenv("EMBEDDING_PROVIDER", raising=False)  # 默认 debug，离线可跑
    services = RuntimeServices()
    services.select("embedding", "debug")
    debug = next(p for p in load_embedding_plugins(REPO_PLUGINS) if p.manifest.name == "debug")
    services.register("embedding", "debug", debug.create())
    memory_plugin = next(
        p
        for p in load_memory_plugins(REPO_PLUGINS, services=services)
        if p.manifest.contribution_id == "vector"
    )
    store = memory_plugin.create()
    services.select("memory", "vector")
    services.register(
        "memory",
        memory_plugin.manifest.name,
        store,
        aliases=candidate_service_names(memory_plugin.manifest),
    )
    retriever_plugin = next(
        p
        for p in load_memory_retriever_plugins(REPO_PLUGINS, services=services)
        if p.manifest.contribution_id == "vector-native"
    )
    return MemoryGateway(store, retriever=retriever_plugin.create()), store


@pytest.mark.asyncio
async def test_vector_backend_semantic_recall_orders_by_similarity(tmp_path, monkeypatch) -> None:
    gateway, _ = _vector_gateway(tmp_path, monkeypatch)

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


class _AtomicTrackingStore(MemoryStore):
    supports_atomic_commit = True

    def __init__(self) -> None:
        self.records: list[MemoryRecord] = []
        self.commit_calls: list[tuple[list[MemoryRecord], tuple[str, ...]]] = []
        self.touch_calls: list[tuple[list[str], str]] = []

    async def list_notes(self) -> list[MemoryNote]:
        return list(self.records)

    async def add_note(self, content: str, tags: list[str]) -> MemoryNote:
        raise AssertionError("atomic writes must not create temporary records")

    async def delete_note(self, note_id: str) -> bool:
        previous = len(self.records)
        self.records = [record for record in self.records if record.id != note_id]
        return len(self.records) != previous

    async def update_note(
        self,
        note_id: str,
        content: str | None = None,
        tags: list[str] | None = None,
    ) -> MemoryNote | None:
        target = next((record for record in self.records if record.id == note_id), None)
        if target is None:
            return None
        if content is not None:
            target.content = content
        if tags is not None:
            target.tags = list(tags)
        return target

    async def search_notes(self, query: str) -> list[MemoryNote]:
        return [record for record in self.records if query.casefold() in record.content.casefold()]

    async def save_record(self, record: MemoryNote) -> None:
        raise AssertionError("gateway should use commit_records for atomic stores")

    async def touch_records(
        self,
        records: list[MemoryNote],
        *,
        accessed_at: str,
    ) -> None:
        self.touch_calls.append(([record.id for record in records], accessed_at))
        for record in records:
            record.last_accessed_at = accessed_at
            record.metadata["accessCount"] = int(record.metadata.get("accessCount", 0)) + 1

    async def commit_records(
        self,
        records: list[MemoryNote],
        *,
        delete_ids: tuple[str, ...] | list[str] = (),
    ) -> None:
        self.commit_calls.append((list(records), tuple(delete_ids)))
        current = {record.id: record for record in self.records}
        for record_id in delete_ids:
            current.pop(record_id, None)
        for record in records:
            current[record.id] = record
        self.records = list(current.values())


@pytest.mark.asyncio
async def test_gateway_atomic_store_uses_one_commit_without_temporary_write() -> None:
    store = _AtomicTrackingStore()
    gateway = MemoryGateway(store, policy=_default_policy())

    note = await gateway.remember("atomic memory")
    duplicate = await gateway.remember("  atomic   memory ")

    assert duplicate.id == note.id
    assert len(store.commit_calls) == 1
    assert [record.id for record in store.commit_calls[0][0]] == [note.id]
    assert store.commit_calls[0][1] == ()


@pytest.mark.asyncio
async def test_gateway_atomic_supersede_commits_target_and_replacement_together() -> None:
    store = _AtomicTrackingStore()
    gateway = MemoryGateway(store, policy=_default_policy())

    old = await gateway.remember("use black")
    new = await gateway.remember("use ruff", supersedes=old.id)

    assert len(store.commit_calls) == 2
    committed, deleted = store.commit_calls[-1]
    assert [record.id for record in committed] == [old.id, new.id]
    assert committed[0].status == "superseded"
    assert committed[1].supersedes == old.id
    assert deleted == ()


@pytest.mark.asyncio
async def test_recall_uses_field_level_touch_instead_of_full_record_commit() -> None:
    store = _AtomicTrackingStore()
    gateway = MemoryGateway(store)
    note = await gateway.remember("field update")

    hits = await gateway.recall("field")

    assert [record.id for record in hits] == [note.id]
    assert len(store.commit_calls) == 1
    assert store.touch_calls == [([note.id], hits[0].last_accessed_at)]
    assert store.records[0].content == "field update"


@pytest.mark.asyncio
async def test_recall_updates_access_statistics_only_for_returned_records(
    tmp_path,
    monkeypatch,
) -> None:
    store = _jsonl_store(tmp_path, monkeypatch)
    gateway = MemoryGateway(store)
    first = await gateway.remember("first")
    second = await gateway.remember("second")

    hits = await gateway.recall(limit=1)
    records = {record.id: record for record in await store.list_notes()}

    assert [record.id for record in hits] == [first.id]
    assert records[first.id].metadata["accessCount"] == 1
    assert "accessCount" not in records[second.id].metadata


class _PassThroughPolicy:
    def prepare_write(self, record: MemoryRecord, *, context=None) -> MemoryRecord:
        return record

    def reconcile(
        self,
        candidate: MemoryRecord,
        existing: list[MemoryRecord],
    ) -> MemoryWriteDecision:
        return MemoryWriteDecision()

    def rank(
        self,
        query: MemoryQuery,
        candidates: list[MemoryRecord],
    ) -> list[MemoryRecord]:
        return candidates


@pytest.mark.asyncio
async def test_recall_hard_filters_lifecycle_without_relying_on_policy(
    tmp_path,
    monkeypatch,
) -> None:
    store = _jsonl_store(tmp_path, monkeypatch)
    gateway = MemoryGateway(store, policy=_PassThroughPolicy())

    active = await gateway.remember("active memory")
    superseded = await gateway.remember("superseded memory")
    expired = await gateway.remember(
        "expired memory",
        expires_at="2000-01-01T00:00:00+00:00",
    )
    other_scope = await gateway.remember("other scope memory", scope="project")
    superseded.status = "superseded"
    await store.save_record(superseded)

    hits = await gateway.recall()

    assert [record.id for record in hits] == [active.id]
    assert expired.id not in [record.id for record in hits]
    assert other_scope.id not in [record.id for record in hits]


@pytest.mark.asyncio
async def test_concurrent_duplicate_writes_are_serialized(tmp_path, monkeypatch) -> None:
    store = _jsonl_store(tmp_path, monkeypatch)
    gateway = MemoryGateway(store, policy=_default_policy())

    results = await asyncio.gather(*(gateway.remember("shared fact") for _ in range(8)))

    assert len({record.id for record in results}) == 1
    assert len(await store.list_notes()) == 1


@pytest.mark.asyncio
async def test_sqlite_update_persists_content_tags_and_record_json(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    store = next(
        plugin for plugin in load_memory_plugins(REPO_PLUGINS) if plugin.manifest.name == "sqlite"
    ).create()
    gateway = MemoryGateway(store)

    note = await gateway.remember("before", tags=["old"])
    updated = await gateway.update(note.id, content="after", tags=["new"])
    stored = (await store.list_notes())[0]

    assert updated is not None
    assert stored.content == "after"
    assert stored.tags == ["new"]
    assert stored.updated_at


@pytest.mark.asyncio
async def test_sqlite_migrates_legacy_access_metadata(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE memory_notes ("
        "id TEXT PRIMARY KEY, content TEXT NOT NULL, tags TEXT NOT NULL,"
        "created_at TEXT NOT NULL, record TEXT)"
    )
    legacy = {
        "id": "legacy",
        "content": "legacy memory",
        "tags": [],
        "created_at": "2026-01-01T00:00:00+00:00",
        "last_accessed_at": "2026-01-02T00:00:00+00:00",
        "metadata": {"accessCount": 4},
    }
    conn.execute(
        "INSERT INTO memory_notes (id, content, tags, created_at, record) VALUES (?, ?, ?, ?, ?)",
        (
            legacy["id"],
            legacy["content"],
            json.dumps(legacy["tags"]),
            legacy["created_at"],
            json.dumps(legacy),
        ),
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("MEMORY_DB_PATH", str(db_path))
    store = next(
        plugin for plugin in load_memory_plugins(REPO_PLUGINS) if plugin.manifest.name == "sqlite"
    ).create()

    stored = (await store.list_notes())[0]

    assert stored.metadata["accessCount"] == 4
    assert stored.last_accessed_at == "2026-01-02T00:00:00+00:00"


@pytest.mark.asyncio
async def test_jsonl_atomic_commit_and_update_are_visible_to_new_store(
    tmp_path,
    monkeypatch,
) -> None:
    store = _jsonl_store(tmp_path, monkeypatch)
    note = await store.add_note("before", ["old"])
    updated = await store.update_note(note.id, content="after", tags=["new"])
    fresh_store = _jsonl_store(tmp_path, monkeypatch)
    stored = (await fresh_store.list_notes())[0]

    assert updated is not None
    assert stored.content == "after"
    assert stored.tags == ["new"]


@pytest.mark.asyncio
async def test_vector_update_persists_record_and_embedding(tmp_path, monkeypatch) -> None:
    gateway, store = _vector_gateway(tmp_path, monkeypatch)

    note = await gateway.remember("alpha")
    updated = await gateway.update(note.id, content="beta", tags=["new"])
    stored = (await store.list_notes())[0]
    hits = await gateway.recall("beta")

    assert updated is not None
    assert stored.content == "beta"
    assert stored.tags == ["new"]
    assert [record.id for record in hits] == [note.id]
