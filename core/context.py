# core/context.py —— 上下文模块：token 估算与历史裁剪（短期记忆的“预算”）
#
# 裁剪策略只做“丢最旧、保最近”，不改变内核 loop；
# 更聪明的摘要压缩可做成 hook 插件替换/叠加本策略。
import math

from core.types import Message


def estimate_tokens(text: str) -> int:
    """粗略 token 估算：ASCII 约 4 字符/token，非 ASCII（中文等）约 1 字符/token。

    宁可高估也不低估，给模型上下文留余量。
    """
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    non_ascii = len(text) - ascii_chars
    return math.ceil(ascii_chars / 4) + non_ascii


def message_tokens(message: Message) -> int:
    tokens = estimate_tokens(message.content) + estimate_tokens(message.role)
    for tool_call in message.tool_calls:
        tokens += estimate_tokens(tool_call.name) + estimate_tokens(str(tool_call.arguments))
    return tokens


def history_tokens(messages: list[Message]) -> int:
    return sum(message_tokens(message) for message in messages)


def trim_history(messages: list[Message], max_tokens: int) -> tuple[list[Message], int]:
    """把历史裁剪进 max_tokens：从最旧开始丢，保最近；返回 (裁剪后, 丢弃条数)。"""
    if max_tokens <= 0:
        return [], len(messages)
    kept = list(messages)
    dropped = 0
    while len(kept) > 1 and history_tokens(kept) > max_tokens:
        kept.pop(0)
        dropped += 1
    return kept, dropped
