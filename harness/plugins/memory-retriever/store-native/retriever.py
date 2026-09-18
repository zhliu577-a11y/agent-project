"""Store-native memory recall."""

from core.memory import MemoryRetriever, MemoryStore, StoreMemoryRetriever


def create_retriever(
    plugin_dir,
    context=None,
    memory: MemoryStore | None = None,
) -> MemoryRetriever:
    if memory is None:
        raise ValueError("store-native retriever requires a memory store")
    return StoreMemoryRetriever(memory)
