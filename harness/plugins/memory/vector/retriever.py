"""Native recall paired with the vector memory store."""

from core.memory import MemoryRetriever, MemoryStore, StoreMemoryRetriever


def create_retriever(
    plugin_dir,
    context=None,
    memory: MemoryStore | None = None,
) -> MemoryRetriever:
    if memory is None:
        raise ValueError("vector-native retriever requires the vector memory store")
    return StoreMemoryRetriever(memory)
