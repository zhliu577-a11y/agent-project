# plugins/model_routers/static/router.py - config-driven model routing
from pathlib import Path

from core.model import ModelRouteDecision, ModelRouter, ModelRouteRequest


class StaticModelRouter(ModelRouter):
    """Resolve explicit overrides, role routes, and fallback in that order."""

    def route(self, request: ModelRouteRequest) -> ModelRouteDecision:
        if request.requested_model is not None:
            return ModelRouteDecision(
                candidates=_dedupe((request.requested_model, *request.fallback)),
                reason="explicit model override",
            )

        role_route = request.routes.get(request.role) if request.role is not None else None
        candidates = role_route or (request.default_model,)
        return ModelRouteDecision(
            candidates=_dedupe((*candidates, *request.fallback)),
            reason=f"role route: {request.role}" if role_route else "default model",
        )


def _dedupe(names) -> tuple[str, ...]:
    result: list[str] = []
    for name in names:
        if name not in result:
            result.append(name)
    return tuple(result)


def create_router(plugin_dir: Path) -> StaticModelRouter:
    return StaticModelRouter()
