# gateways/skill_gateway.py —— 技能网关：技能目录的持有者与按需读取者
#
# 渐进披露设计（ADR 0003）：
# - 启动时只向模型暴露目录条目（名字 + 一句话描述）；
# - 模型需要某技能时调用 use_skill(name)，本网关才读取该插件正文（惰性 + 缓存）；
# - 附属资源必须显式通过 use_skill(name, resource=...) 读取；
# - 正文、资源路径和大小预算在装配与运行时双重校验。
from __future__ import annotations

from pathlib import Path
from typing import Any

from config import (
    DEFAULT_SKILL_MAX_CONTENT_BYTES,
    DEFAULT_SKILL_MAX_RESOURCE_BYTES,
    DEFAULT_SKILL_MAX_RESOURCE_TOTAL_BYTES,
)
from core.events import Event, EventBus
from core.tool import Tool
from plugins.loader import SkillPlugin


class SkillGateway:
    """技能目录网关：持有全部技能插件，按需返回正文或资源。"""

    def __init__(
        self,
        skills: list[SkillPlugin],
        *,
        max_content_bytes: int = DEFAULT_SKILL_MAX_CONTENT_BYTES,
        max_resource_bytes: int = DEFAULT_SKILL_MAX_RESOURCE_BYTES,
        max_resource_total_bytes: int = DEFAULT_SKILL_MAX_RESOURCE_TOTAL_BYTES,
    ) -> None:
        self._skills = {skill.manifest.name: skill for skill in skills}
        self._cache: dict[str, str] = {}
        self._resource_cache: dict[tuple[str, str], str] = {}
        self._loaded_bytes: dict[str, int] = {}
        self._errors: dict[str, str] = {}
        self._max_content_bytes = max_content_bytes
        self._max_resource_bytes = max_resource_bytes
        self._max_resource_total_bytes = max_resource_total_bytes

    def available(self) -> list[str]:
        return sorted(self._skills)

    def catalog(self) -> list[tuple[str, str]]:
        """目录条目：(名字, 一句话描述)。只用于提示词与 use_skill 参数枚举。"""
        return [
            (skill.manifest.name, skill.manifest.description)
            for skill in sorted(self._skills.values(), key=lambda s: s.manifest.name)
        ]

    def plugin(self, name: str) -> SkillPlugin | None:
        return self._skills.get(name)

    def is_loaded(self, name: str) -> bool:
        return name in self._loaded_bytes

    def has_content(self, name: str) -> bool:
        return name in self._cache

    def has_resource(self, name: str, resource_path: str) -> bool:
        return (name, resource_path) in self._resource_cache

    def loaded_bytes(self, name: str) -> int:
        return self._loaded_bytes.get(name, 0)

    def error(self, name: str) -> str | None:
        return self._errors.get(name)

    def resource_catalog(self, name: str) -> list[tuple[str, str]]:
        skill = self._skill(name)
        return [(resource.path, resource.description) for resource in skill.resources]

    def get(self, name: str) -> str:
        """读取某技能正文（惰性 + 缓存）；未知技能抛 KeyError。"""
        skill = self._skill(name)
        if name in self._cache:
            return self._cache[name]

        try:
            content = self._read_text(
                self._inside(skill, skill.content_path, "正文"),
                self._max_content_bytes,
                f"技能 {name} 正文",
            )
        except ValueError as exc:
            self._errors[name] = str(exc)
            raise

        self._cache[name] = content
        self._loaded_bytes[name] = self._loaded_bytes.get(name, 0) + len(content.encode("utf-8"))
        self._errors.pop(name, None)
        return content

    def get_resource(self, name: str, resource_path: str) -> str:
        """读取一个已声明的技能资源。"""
        skill = self._skill(name)
        resource = next(
            (candidate for candidate in skill.resources if candidate.path == resource_path),
            None,
        )
        if resource is None:
            available = [candidate.path for candidate in skill.resources]
            raise ValueError(f"技能 {name} 未知资源: {resource_path}，可选: {available}")

        cache_key = (name, resource_path)
        if cache_key in self._resource_cache:
            return self._resource_cache[cache_key]

        try:
            resolved = self._inside(skill, resource.resolved_path, f"资源 {resource_path}")
            self._check_resource_total(skill)
            content = self._read_text(
                resolved,
                self._max_resource_bytes,
                f"技能 {name} 资源 {resource_path}",
            )
        except ValueError as exc:
            self._errors[name] = str(exc)
            raise

        self._resource_cache[cache_key] = content
        size = len(content.encode("utf-8"))
        self._loaded_bytes[name] = self._loaded_bytes.get(name, 0) + size
        self._errors.pop(name, None)
        return content

    def _skill(self, name: str) -> SkillPlugin:
        skill = self._skills.get(name)
        if skill is None:
            raise KeyError(f"未知技能: {name}，可用: {self.available()}")
        return skill

    @staticmethod
    def _inside(skill: SkillPlugin, path: Path, label: str) -> Path:
        resolved = path.resolve()
        try:
            resolved.relative_to(skill.manifest.directory.resolve())
        except ValueError as exc:
            raise ValueError(f"技能 {skill.manifest.name} {label}路径越出插件目录: {path}") from exc
        return resolved

    def _check_resource_total(self, skill: SkillPlugin) -> None:
        total = 0
        for resource in skill.resources:
            path = self._inside(skill, resource.resolved_path, f"资源 {resource.path}")
            try:
                total += path.stat().st_size
            except OSError as exc:
                raise ValueError(
                    f"技能 {skill.manifest.name} 资源 {resource.path} 无法读取大小: {exc}"
                ) from exc
        if total > self._max_resource_total_bytes:
            raise ValueError(
                f"技能 {skill.manifest.name} 资源总大小超过预算: "
                f"{total} > {self._max_resource_total_bytes} bytes"
            )

    @staticmethod
    def _read_text(path: Path, max_bytes: int, label: str) -> str:
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise ValueError(f"{label}读取失败: {exc}") from exc
        if len(data) > max_bytes:
            raise ValueError(f"{label}超过大小预算: {len(data)} > {max_bytes} bytes")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"{label}不是合法 UTF-8: {exc}") from exc


class UseSkill(Tool):
    """把“按需读取技能正文或资源”做成工具：渐进披露的模型侧入口。"""

    name = "use_skill"
    description = (
        "按需读取一个技能插件的完整操作说明或附属资源；收到正文后请按其中的规则/清单执行当前任务。"
    )

    def __init__(self, gateway: SkillGateway, events: EventBus | None = None) -> None:
        self._gateway = gateway
        self._events = events

    @property
    def parameters(self) -> dict[str, Any]:
        available = "、".join(self._gateway.available()) or "（暂无可用技能）"
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": f"要读取的技能名，可选：{available}",
                },
                "resource": {
                    "type": "string",
                    "description": "可选的资源路径；先读取技能正文查看可用资源清单",
                },
            },
            "required": ["name"],
        }

    async def execute(self, **kwargs: Any) -> str:
        name = kwargs.get("name")
        if not isinstance(name, str) or not name.strip():
            return "必须提供非空技能名 'name'。"
        name = name.strip()
        resource = kwargs.get("resource")
        if resource is not None and (not isinstance(resource, str) or not resource.strip()):
            return "参数 'resource' 必须是非空字符串。"

        try:
            if resource is not None:
                resource = resource.strip()
                was_loaded = self._gateway.has_resource(name, resource)
                content = self._gateway.get_resource(name, resource)
                if not was_loaded and self._events is not None:
                    await self._events.publish(
                        Event(
                            "skill.resource_loaded",
                            {
                                "name": name,
                                "resource": resource,
                                "bytes": len(content.encode("utf-8")),
                            },
                        )
                    )
                return content

            was_loaded = self._gateway.has_content(name)
            content = self._gateway.get(name)
            if not was_loaded and self._events is not None:
                await self._events.publish(
                    Event(
                        "skill.loaded",
                        {
                            "name": name,
                            "bytes": len(content.encode("utf-8")),
                        },
                    )
                )
            resources = self._gateway.resource_catalog(name)
            if not resources:
                return content
            lines = ["", "可用资源（需要时再次调用 use_skill）："]
            lines.extend(f"- {path}: {description}" for path, description in resources)
            return content.rstrip() + "\n" + "\n".join(lines)
        except KeyError as exc:
            return str(exc)
        except ValueError as exc:
            return str(exc)
