# gateways/skill_gateway.py - Skill catalog, permissions, budgets, and access.
#
# Skills are content-only plugins. The model sees a bounded catalog and reads
# the selected Skill only through use_skill; this gateway is the single place
# where visibility, permission checks, budgets, and usage accounting happen.
from __future__ import annotations

import fnmatch
import inspect
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from config import (
    DEFAULT_SKILL_MAX_CONTENT_BYTES,
    DEFAULT_SKILL_MAX_LISTING_BYTES,
    DEFAULT_SKILL_MAX_RESOURCE_BYTES,
    DEFAULT_SKILL_MAX_RESOURCE_TOTAL_BYTES,
)
from core.events import Event, EventPublisher
from core.tool import Tool
from plugins.loader import SkillPlugin

ApprovalCallback = Callable[..., bool | Awaitable[bool]]
_TRIGGER_TERM_RE = re.compile(r"[a-z0-9_]+|[\u4e00-\u9fff]{2,}", re.IGNORECASE)


class SkillAccessDenied(PermissionError):
    """The policy explicitly denies access to a Skill."""


class SkillApprovalRequired(PermissionError):
    """The policy requires approval before a Skill can be read."""


@dataclass(frozen=True)
class SkillPermissionDecision:
    """One policy decision for a Skill and Agent pair."""

    access: str
    reason: str
    matched_pattern: str | None = None
    source: str = "default"

    @property
    def allowed(self) -> bool:
        return self.access == "allow"

    @property
    def requires_approval(self) -> bool:
        return self.access == "ask"


@dataclass(frozen=True)
class SkillCatalogEntry:
    """Prompt-safe metadata for one visible Skill."""

    name: str
    description: str
    when_to_use: str
    tags: tuple[str, ...]
    compatibility: str
    priority: int
    listing: str
    access: str


@dataclass(frozen=True)
class SkillUsageStats:
    """Observability counters held by the in-process Skill gateway."""

    requests: int = 0
    loads: int = 0
    resource_loads: int = 0
    denied: int = 0
    approval_requests: int = 0
    approvals_granted: int = 0
    approvals_denied: int = 0
    load_failures: int = 0
    resource_failures: int = 0
    trigger_hits: int = 0

    @property
    def failures(self) -> int:
        return self.load_failures + self.resource_failures


@dataclass(frozen=True)
class SkillTriggerResult:
    """Deterministic trigger evaluation result for one Skill."""

    name: str
    score: float
    matched_terms: tuple[str, ...]
    matched_fields: tuple[str, ...]


class SkillPermissionPolicy:
    """Apply global and per-Agent allow/ask/deny rules.

    Agent-specific rules take precedence over global rules whenever an agent
    rule matches. Within a rule set, an exact name beats a wildcard, and a
    wildcard with more literal characters beats a broader wildcard.
    """

    def __init__(
        self,
        *,
        global_rules: tuple[tuple[str, str], ...] = (),
        agent_rules: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (),
        agent: str = "default",
    ) -> None:
        if not isinstance(agent, str) or not agent.strip():
            raise ValueError("agent must be a non-empty string")
        self._global_rules = _validate_permission_rules(global_rules)
        parsed_agent_rules: dict[str, tuple[tuple[str, str], ...]] = {}
        for name, rules in agent_rules:
            if not isinstance(name, str) or not name.strip():
                raise ValueError("agent permission keys must be non-empty strings")
            if name in parsed_agent_rules:
                raise ValueError(f"duplicate agent permission rules: {name}")
            parsed_agent_rules[name] = _validate_permission_rules(rules)
        self._agent_rules = parsed_agent_rules
        self._agent = agent

    @property
    def agent(self) -> str:
        return self._agent

    def evaluate(
        self,
        name: str,
        *,
        agent: str | None = None,
    ) -> SkillPermissionDecision:
        """Return the strongest matching rule for one Skill."""
        selected_agent = agent or self._agent
        agent_match = self._best_match(
            self._agent_rules.get(selected_agent, ()),
            name,
        )
        if agent_match is not None:
            pattern, access = agent_match
            return SkillPermissionDecision(
                access=access,
                reason=f"agent {selected_agent!r} matched {pattern!r}",
                matched_pattern=pattern,
                source="agent",
            )

        global_match = self._best_match(self._global_rules, name)
        if global_match is not None:
            pattern, access = global_match
            return SkillPermissionDecision(
                access=access,
                reason=f"global rule matched {pattern!r}",
                matched_pattern=pattern,
                source="global",
            )

        return SkillPermissionDecision(
            access="allow",
            reason="no matching rule; default allow",
        )

    @classmethod
    def _best_match(
        cls,
        rules: tuple[tuple[str, str], ...],
        name: str,
    ) -> tuple[str, str] | None:
        matched: list[tuple[int, tuple[int, int, int], str, str]] = []
        for index, (pattern, access) in enumerate(rules):
            if not _matches_pattern(name, pattern):
                continue
            matched.append(
                (
                    index,
                    _pattern_specificity(pattern),
                    pattern,
                    access,
                )
            )
        if not matched:
            return None
        _index, _specificity, pattern, access = max(
            matched,
            key=lambda item: (item[1], -item[0]),
        )
        return pattern, access


class SkillTriggerEvaluator:
    """Deterministic Skill matching without calling a model."""

    _FIELD_WEIGHTS = {
        "name": 2.5,
        "tags": 3.0,
        "when_to_use": 2.0,
        "description": 1.0,
        "compatibility": 0.25,
    }

    def __init__(self, entries: list[SkillCatalogEntry]) -> None:
        self._entries = tuple(entries)

    def evaluate(
        self,
        query: str,
        *,
        limit: int = 5,
        min_score: float = 0.0,
    ) -> list[SkillTriggerResult]:
        if not isinstance(query, str):
            raise TypeError("query must be a string")
        if not query.strip():
            return []
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        if isinstance(min_score, bool) or not isinstance(min_score, (int, float)):
            raise ValueError("min_score must be a number")

        terms = _query_terms(query)
        if not terms:
            return []

        results: list[SkillTriggerResult] = []
        for entry in self._entries:
            score = 0.0
            matched_terms: set[str] = set()
            matched_fields: set[str] = set()
            fields = {
                "name": entry.name,
                "tags": " ".join(entry.tags),
                "when_to_use": entry.when_to_use,
                "description": entry.description,
                "compatibility": entry.compatibility,
            }
            for term in terms:
                for field_name, field_value in fields.items():
                    if not field_value:
                        continue
                    if term != query.casefold() and term not in field_value.casefold():
                        continue
                    if term == query.casefold() and term == entry.name.casefold():
                        score += self._FIELD_WEIGHTS[field_name] * 1.5
                    elif term in field_value.casefold():
                        score += self._FIELD_WEIGHTS[field_name]
                    else:
                        continue
                    matched_terms.add(term)
                    matched_fields.add(field_name)
            if score > min_score:
                results.append(
                    SkillTriggerResult(
                        name=entry.name,
                        score=float(score),
                        matched_terms=tuple(sorted(matched_terms)),
                        matched_fields=tuple(sorted(matched_fields)),
                    )
                )

        results.sort(key=lambda item: (-item.score, item.name))
        return results[:limit]


class SkillGateway:
    """The single read path for content-only Skill plugins."""

    def __init__(
        self,
        skills: list[SkillPlugin],
        *,
        max_content_bytes: int = DEFAULT_SKILL_MAX_CONTENT_BYTES,
        max_resource_bytes: int = DEFAULT_SKILL_MAX_RESOURCE_BYTES,
        max_resource_total_bytes: int = DEFAULT_SKILL_MAX_RESOURCE_TOTAL_BYTES,
        max_listing_bytes: int = DEFAULT_SKILL_MAX_LISTING_BYTES,
        policy: SkillPermissionPolicy | None = None,
        name_only: tuple[str, ...] = (),
    ) -> None:
        if (
            isinstance(max_listing_bytes, bool)
            or not isinstance(max_listing_bytes, int)
            or max_listing_bytes <= 0
        ):
            raise ValueError("max_listing_bytes must be a positive integer")

        self._skills: dict[str, SkillPlugin] = {}
        for skill in skills:
            name = skill.manifest.name
            if name in self._skills:
                raise ValueError(
                    f"技能重名: {name} ({self._skills[name].manifest.directory} / "
                    f"{skill.manifest.directory})"
                )
            self._skills[name] = skill

        self._cache: dict[str, str] = {}
        self._resource_cache: dict[tuple[str, str], str] = {}
        self._loaded_bytes: dict[str, int] = {}
        self._errors: dict[str, str] = {}
        self._usage: dict[str, SkillUsageStats] = {}
        self._max_content_bytes = max_content_bytes
        self._max_resource_bytes = max_resource_bytes
        self._max_resource_total_bytes = max_resource_total_bytes
        self._max_listing_bytes = max_listing_bytes
        self._policy = policy or SkillPermissionPolicy()
        self._name_only = tuple(name_only)

    @property
    def policy(self) -> SkillPermissionPolicy:
        return self._policy

    def available(self) -> list[str]:
        """Return visible names; denied Skills never appear here."""
        return sorted(name for name in self._skills if self._policy.evaluate(name).access != "deny")

    def entries(self) -> list[SkillCatalogEntry]:
        """Return visible metadata sorted by Skill priority then name."""
        entries = [
            SkillCatalogEntry(
                name=skill.manifest.name,
                description=self._description(skill),
                when_to_use=skill.when_to_use,
                tags=skill.tags,
                compatibility=skill.compatibility,
                priority=skill.manifest.priority,
                listing=skill.listing,
                access=self._policy.evaluate(skill.manifest.name).access,
            )
            for skill in self._skills.values()
            if self._policy.evaluate(skill.manifest.name).access != "deny"
        ]
        return sorted(entries, key=lambda entry: (entry.priority, entry.name))

    def catalog(self) -> list[tuple[str, str]]:
        """Return full visible name/description pairs for callers that need it."""
        return [
            (entry.name, entry.description)
            for entry in sorted(self.entries(), key=lambda entry: entry.name)
        ]

    def listing(self) -> list[tuple[str, str]]:
        """Return a budgeted directory listing.

        Names are always retained. Descriptions are allocated from high
        priority to low priority; once the byte budget is exhausted, lower
        priority Skills remain name-only.
        """
        entries = self.entries()
        if not entries:
            return []

        full = [
            (
                entry.name,
                "" if self._is_name_only(entry.name) else entry.description,
            )
            for entry in entries
        ]
        if self._listing_size(full) <= self._max_listing_bytes:
            return full

        baseline = [(entry.name, "") for entry in entries]
        baseline_size = self._listing_size(baseline)
        if baseline_size > self._max_listing_bytes:
            raise ValueError(
                "Skill listing budget is too small for all Skill names: "
                f"{baseline_size} > {self._max_listing_bytes} bytes"
            )

        remaining = self._max_listing_bytes - baseline_size
        texts = {name: "" for name, _ in baseline}
        for entry in entries:
            if self._is_name_only(entry.name) or not entry.description:
                continue
            description_bytes = len(entry.description.encode("utf-8"))
            needed = 2 + description_bytes
            if needed <= remaining:
                texts[entry.name] = entry.description
                remaining -= needed
                continue
            if remaining > 2:
                texts[entry.name] = _truncate_utf8(
                    entry.description,
                    remaining - 2,
                )
                remaining = 0
            break

        return [(entry.name, texts[entry.name]) for entry in entries]

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

    def permission(
        self,
        name: str,
        *,
        agent: str | None = None,
    ) -> SkillPermissionDecision:
        self._skill(name)
        return self._policy.evaluate(name, agent=agent)

    def access(
        self,
        name: str,
        *,
        approved: bool = False,
        agent: str | None = None,
    ) -> SkillPermissionDecision:
        """Enforce the deny/ask/allow decision for one Skill."""
        decision = self.permission(name, agent=agent)
        if decision.access == "deny":
            raise SkillAccessDenied(f"技能 {name} 被权限策略禁止访问")
        if decision.access == "ask" and not approved:
            raise SkillApprovalRequired(f"技能 {name} 需要人工批准后才能访问")
        return decision

    def usage(self, name: str) -> SkillUsageStats:
        return self._usage.get(name, SkillUsageStats())

    def usage_all(self) -> dict[str, SkillUsageStats]:
        return dict(self._usage)

    def record_request(self, name: str) -> None:
        self._increment(name, "requests")

    def record_load(self, name: str) -> None:
        self._increment(name, "loads")

    def record_resource_load(self, name: str) -> None:
        self._increment(name, "resource_loads")

    def record_denied(self, name: str) -> None:
        self._increment(name, "denied")

    def record_approval_required(self, name: str) -> None:
        self._increment(name, "approval_requests")

    def record_approval_granted(self, name: str) -> None:
        self._increment(name, "approvals_granted")

    def record_approval_denied(self, name: str) -> None:
        self._increment(name, "approvals_denied")

    def record_load_failure(self, name: str) -> None:
        self._increment(name, "load_failures")

    def record_resource_failure(self, name: str) -> None:
        self._increment(name, "resource_failures")

    def record_trigger(self, name: str) -> None:
        self._increment(name, "trigger_hits")

    def evaluate_triggers(
        self,
        query: str,
        *,
        limit: int = 5,
        min_score: float = 0.0,
    ) -> list[SkillTriggerResult]:
        """Evaluate visible Skills and record the returned trigger hits."""
        results = SkillTriggerEvaluator(self.entries()).evaluate(
            query,
            limit=limit,
            min_score=min_score,
        )
        for result in results:
            self.record_trigger(result.name)
        return results

    def invalidate(self, name: str, *, resource: str | None = None) -> bool:
        """Drop cached Skill content/resources so the next read refreshes them."""
        skill = self._skill(name)
        if resource is None:
            removed = name in self._cache or any(key[0] == name for key in self._resource_cache)
            self._cache.pop(name, None)
            for key in [key for key in self._resource_cache if key[0] == name]:
                self._resource_cache.pop(key, None)
        else:
            if not any(candidate.path == resource for candidate in skill.resources):
                raise ValueError(f"技能 {name} 未知资源: {resource}")
            key = (name, resource)
            removed = key in self._resource_cache
            self._resource_cache.pop(key, None)

        if removed:
            self._recompute_loaded_bytes(name)
            self._errors.pop(name, None)
        return removed

    def reload(
        self,
        name: str,
        *,
        approved: bool = False,
        agent: str | None = None,
    ) -> str:
        """Invalidate one Skill and read its current content."""
        self.invalidate(name)
        return self.get(name, approved=approved, agent=agent)

    def reload_resource(
        self,
        name: str,
        resource: str,
        *,
        approved: bool = False,
        agent: str | None = None,
    ) -> str:
        """Invalidate one Skill resource and read its current content."""
        self.invalidate(name, resource=resource)
        return self.get_resource(
            name,
            resource,
            approved=approved,
            agent=agent,
        )

    def resource_catalog(self, name: str) -> list[tuple[str, str]]:
        skill = self._skill(name)
        return [(resource.path, resource.description) for resource in skill.resources]

    def get(
        self,
        name: str,
        *,
        approved: bool = False,
        agent: str | None = None,
    ) -> str:
        """Read Skill content after enforcing the permission decision."""
        self.access(name, approved=approved, agent=agent)
        skill = self._skill(name)
        if name in self._cache:
            self.record_load(name)
            return self._cache[name]

        try:
            content = self._read_text(
                self._inside(skill, skill.content_path, "正文"),
                self._max_content_bytes,
                f"技能 {name} 正文",
            )
        except ValueError as exc:
            self._errors[name] = str(exc)
            self.record_load_failure(name)
            raise

        self._cache[name] = content
        self._loaded_bytes[name] = self._loaded_bytes.get(name, 0) + len(content.encode("utf-8"))
        self._errors.pop(name, None)
        self.record_load(name)
        return content

    def get_resource(
        self,
        name: str,
        resource_path: str,
        *,
        approved: bool = False,
        agent: str | None = None,
    ) -> str:
        """Read one declared Skill resource after enforcing permissions."""
        self.access(name, approved=approved, agent=agent)
        skill = self._skill(name)
        resource = next(
            (candidate for candidate in skill.resources if candidate.path == resource_path),
            None,
        )
        if resource is None:
            available = [candidate.path for candidate in skill.resources]
            error = f"技能 {name} 未知资源: {resource_path}，可选: {available}"
            self._errors[name] = error
            self.record_resource_failure(name)
            raise ValueError(error)

        cache_key = (name, resource_path)
        if cache_key in self._resource_cache:
            self.record_resource_load(name)
            return self._resource_cache[cache_key]

        try:
            resolved = self._inside(
                skill,
                resource.resolved_path,
                f"资源 {resource_path}",
            )
            self._check_resource_total(skill)
            content = self._read_text(
                resolved,
                self._max_resource_bytes,
                f"技能 {name} 资源 {resource_path}",
            )
        except ValueError as exc:
            self._errors[name] = str(exc)
            self.record_resource_failure(name)
            raise

        self._resource_cache[cache_key] = content
        size = len(content.encode("utf-8"))
        self._loaded_bytes[name] = self._loaded_bytes.get(name, 0) + size
        self._errors.pop(name, None)
        self.record_resource_load(name)
        return content

    def _description(self, skill: SkillPlugin) -> str:
        return skill.description or skill.manifest.description

    def _is_name_only(self, name: str) -> bool:
        skill = self._skills[name]
        return skill.listing == "name-only" or any(
            _matches_pattern(name, pattern) for pattern in self._name_only
        )

    def _increment(self, name: str, field_name: str) -> None:
        current = self._usage.get(name, SkillUsageStats())
        self._usage[name] = replace(
            current,
            **{field_name: getattr(current, field_name) + 1},
        )

    def _listing_size(self, entries: list[tuple[str, str]]) -> int:
        lines = [_format_listing_line(name, description) for name, description in entries]
        if not lines:
            return 0
        return sum(len(line.encode("utf-8")) for line in lines) + len(lines) - 1

    def _recompute_loaded_bytes(self, name: str) -> None:
        total = len(self._cache.get(name, "").encode("utf-8"))
        total += sum(
            len(content.encode("utf-8"))
            for (cached_name, _), content in self._resource_cache.items()
            if cached_name == name
        )
        if name in self._cache or any(key[0] == name for key in self._resource_cache):
            self._loaded_bytes[name] = total
        else:
            self._loaded_bytes.pop(name, None)

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
    """The model-facing, permission-checked Skill read tool."""

    name = "use_skill"
    description = "按需读取一个技能插件的完整操作说明或附属资源；收到正文后请按其规则执行当前任务。"

    def __init__(
        self,
        gateway: SkillGateway,
        events: EventPublisher | None = None,
        approver: ApprovalCallback | None = None,
    ) -> None:
        self._gateway = gateway
        self._events = events
        self._approver = approver

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
        if resource is not None:
            resource = resource.strip()

        self._gateway.record_request(name)
        try:
            decision = self._gateway.permission(name)
        except KeyError as exc:
            await self._publish_failure(name, resource=resource, exc=exc)
            return str(exc)

        if decision.access == "deny":
            self._gateway.record_denied(name)
            await self._publish(
                "skill.denied",
                {
                    "name": name,
                    "resource": resource,
                    "matched_pattern": decision.matched_pattern,
                    "source": decision.source,
                },
            )
            return f"技能 {name} 被权限策略禁止访问。"

        approved = False
        if decision.access == "ask":
            self._gateway.record_approval_required(name)
            await self._publish(
                "skill.approval_required",
                {
                    "name": name,
                    "resource": resource,
                    "matched_pattern": decision.matched_pattern,
                    "source": decision.source,
                },
            )
            if not await self._request_approval(name, resource, decision):
                self._gateway.record_approval_denied(name)
                await self._publish(
                    "skill.approval_denied",
                    {
                        "name": name,
                        "resource": resource,
                    },
                )
                return f"技能 {name} 需要批准；当前请求未获批准。"
            approved = True
            self._gateway.record_approval_granted(name)
            await self._publish(
                "skill.approved",
                {
                    "name": name,
                    "resource": resource,
                },
            )

        try:
            if resource is not None:
                was_loaded = self._gateway.has_resource(name, resource)
                content = self._gateway.get_resource(
                    name,
                    resource,
                    approved=approved,
                )
                if not was_loaded:
                    await self._publish(
                        "skill.resource_loaded",
                        {
                            "name": name,
                            "resource": resource,
                            "bytes": len(content.encode("utf-8")),
                        },
                    )
                return content

            was_loaded = self._gateway.has_content(name)
            content = self._gateway.get(name, approved=approved)
            if not was_loaded:
                await self._publish(
                    "skill.loaded",
                    {
                        "name": name,
                        "bytes": len(content.encode("utf-8")),
                    },
                )
            resources = self._gateway.resource_catalog(name)
            if not resources:
                return content
            lines = ["", "可用资源（需要时再次调用 use_skill）："]
            lines.extend(f"- {path}: {description}" for path, description in resources)
            return content.rstrip() + "\n" + "\n".join(lines)
        except (KeyError, ValueError, PermissionError) as exc:
            await self._publish_failure(name, resource=resource, exc=exc)
            return str(exc)

    async def _request_approval(
        self,
        name: str,
        resource: str | None,
        decision: SkillPermissionDecision,
    ) -> bool:
        if self._approver is None:
            return False
        try:
            result = self._call_approver(name, resource, decision)
            if inspect.isawaitable(result):
                result = await result
        except Exception:
            return False
        return bool(result)

    def _call_approver(
        self,
        name: str,
        resource: str | None,
        decision: SkillPermissionDecision,
    ) -> bool | Awaitable[bool]:
        assert self._approver is not None
        try:
            signature = inspect.signature(self._approver)
        except (TypeError, ValueError):
            return self._approver(name)

        positional = [
            parameter
            for parameter in signature.parameters.values()
            if parameter.kind
            in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
        ]
        has_varargs = any(
            parameter.kind == inspect.Parameter.VAR_POSITIONAL
            for parameter in signature.parameters.values()
        )
        if has_varargs or len(positional) >= 3:
            return self._approver(name, resource, decision)
        if len(positional) == 2:
            return self._approver(name, resource)
        return self._approver(name)

    async def _publish_failure(
        self,
        name: str,
        *,
        resource: str | None,
        exc: Exception,
    ) -> None:
        payload: dict[str, Any] = {
            "name": name,
            "error": str(exc),
            "error_type": type(exc).__name__,
        }
        event_name = "skill.load_failed"
        if resource is not None:
            event_name = "skill.resource_failed"
            payload["resource"] = resource
        await self._publish(event_name, payload)

    async def _publish(self, name: str, payload: dict[str, Any]) -> None:
        if self._events is None:
            return
        await self._events.publish(Event(name, payload))


def _validate_permission_rules(
    rules: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str], ...]:
    validated: list[tuple[str, str]] = []
    for pattern, access in rules:
        if not isinstance(pattern, str) or not pattern.strip():
            raise ValueError("permission patterns must be non-empty strings")
        if not isinstance(access, str) or access not in {"allow", "ask", "deny"}:
            raise ValueError(f"permission {pattern!r} must be allow, ask, or deny")
        validated.append((pattern, access))
    return tuple(validated)


def _matches_pattern(name: str, pattern: str) -> bool:
    return fnmatch.fnmatchcase(name, pattern)


def _pattern_specificity(pattern: str) -> tuple[int, int, int]:
    wildcard = any(character in pattern for character in "*?[")
    if not wildcard:
        return 2, len(pattern), len(pattern)
    literal_count = sum(character not in "*?[]" for character in pattern)
    return 1, literal_count, len(pattern)


def _query_terms(query: str) -> set[str]:
    folded = query.casefold().strip()
    if not folded:
        return set()
    terms = {term.casefold() for term in _TRIGGER_TERM_RE.findall(folded) if term}
    if len(folded) >= 2:
        terms.add(folded)
    return terms


def _format_listing_line(name: str, description: str) -> str:
    if description:
        return f"- {name}: {description}"
    return f"- {name}"


def _truncate_utf8(value: str, max_bytes: int) -> str:
    if max_bytes <= 0:
        return ""
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")
