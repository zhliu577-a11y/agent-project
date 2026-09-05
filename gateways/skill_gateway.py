# gateways/skill_gateway.py —— 技能网关：技能目录的持有者与按需读取者
#
# 渐进披露设计（ADR 0003）：
# - 启动时只向模型暴露目录条目（名字 + 一句话描述）；
# - 模型需要某技能时调用 use_skill(name)，本网关才读取该插件正文（惰性 + 缓存）；
# - preload 的技能在装配时由 main 注入系统提示词，不经过本网关的缓存。
from typing import Any

from core.tool import Tool
from plugins.loader import SkillPlugin


class SkillGateway:
    """技能目录网关：持有全部技能插件，按需返回正文。"""

    def __init__(self, skills: list[SkillPlugin]) -> None:
        self._skills = {skill.manifest.name: skill for skill in skills}
        self._cache: dict[str, str] = {}

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

    def get(self, name: str) -> str:
        """读取某技能的正文（惰性 + 缓存）；未知技能抛 KeyError。"""
        skill = self._skills.get(name)
        if skill is None:
            raise KeyError(f"未知技能: {name}，可用: {self.available()}")
        if name not in self._cache:
            try:
                self._cache[name] = skill.content_path.read_text(encoding="utf-8")
            except OSError as exc:
                raise ValueError(f"技能 {name} 正文读取失败: {exc}") from exc
        return self._cache[name]


class UseSkill(Tool):
    """把“按需读取技能正文”做成工具：渐进披露的模型侧入口。"""

    name = "use_skill"
    description = (
        "按需读取一个技能插件的完整操作说明并返回正文；收到正文后请按其中的规则/清单执行当前任务。"
    )

    def __init__(self, gateway: SkillGateway) -> None:
        self._gateway = gateway

    @property
    def parameters(self) -> dict[str, Any]:
        available = "、".join(self._gateway.available()) or "（暂无可用技能）"
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": f"要读取的技能名，可选：{available}",
                }
            },
            "required": ["name"],
        }

    async def execute(self, **kwargs: Any) -> str:
        name = kwargs["name"]
        try:
            return self._gateway.get(name)
        except KeyError as exc:
            return str(exc)
        except ValueError as exc:
            return str(exc)
