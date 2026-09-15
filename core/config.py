# core/config.py —— 配置管理：中心 config.json + 环境变量覆盖 + 内置默认
#
# 优先级：环境变量 > config.json > 内置默认。
# 只承载“选择型配置”（模型/存储/嵌入/上下文预算）；
# 密钥与数据目录等仍走环境变量（不进版本库/不进选择文件）。
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.json"


@dataclass(frozen=True)
class AppConfig:
    model: str = "deepseek"
    session_store: str = "jsonl"
    session_id: str = "default"
    memory_store: str = "sqlite"
    embedding_provider: str = "debug"
    context_max_tokens: int = 20000
    context_strategy: str = "tail-window"

    @classmethod
    def load(cls, path: str | Path | None = None) -> "AppConfig":
        """读取并合并三层配置；文件存在但字段非法时启动即报错。"""
        config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
        raw: dict[str, Any] = {}
        if config_path.exists():
            raw = cls._read_json(config_path)

        session = cls._nested(raw, "session")
        memory = cls._nested(raw, "memory")
        embedding = cls._nested(raw, "embedding")
        context = cls._nested(raw, "context")

        return cls(
            model=os.getenv("AGENT_MODEL", _expect_str(raw.get("model", "deepseek"), "model")),
            session_store=os.getenv(
                "SESSION_STORE", _expect_str(session.get("store", "jsonl"), "session.store")
            ),
            session_id=os.getenv(
                "SESSION_ID", _expect_str(session.get("id", "default"), "session.id")
            ),
            memory_store=os.getenv(
                "MEMORY_STORE", _expect_str(memory.get("store", "sqlite"), "memory.store")
            ),
            embedding_provider=os.getenv(
                "EMBEDDING_PROVIDER",
                _expect_str(embedding.get("provider", "debug"), "embedding.provider"),
            ),
            context_max_tokens=int(
                os.getenv(
                    "CONTEXT_MAX_TOKENS",
                    _expect_int(context.get("maxTokens", 20000), "context.maxTokens"),
                )
            ),
            context_strategy=os.getenv(
                "CONTEXT_STRATEGY",
                _expect_str(context.get("strategy", "tail-window"), "context.strategy"),
            ),
        )

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"{path}: 不是合法的 JSON 配置文件: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError(f"{path}: 配置顶层必须是 JSON 对象")
        return raw

    @staticmethod
    def _nested(raw: dict[str, Any], key: str) -> dict[str, Any]:
        value = raw.get(key, {})
        if not isinstance(value, dict):
            raise ValueError(f"config.json: '{key}' 必须是对象")
        return value


def _expect_str(value: Any, key: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"config.json: '{key}' 必须是非空字符串")
    return value


def _expect_int(value: Any, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"config.json: '{key}' 必须是整数")
    return value
