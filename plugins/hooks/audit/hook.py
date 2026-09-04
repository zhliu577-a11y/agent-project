# plugins/hooks/audit/hook.py —— 示例钩子插件：审计日志
#
# 一个 hook 插件 = 实现 LifecycleHooks 的类 + 工厂函数 create_hook(plugin_dir)。
# 工厂收到插件自己的目录，因此配置、日志等文件都可以随插件走。
#
# 本示例演示了五个生命周期事件中的四个：
#   - llm_response ：模型每次回复后观察（记录回复摘要与请求了哪些工具）
#   - tool_before  ：工具执行前观察（返回 False 即可拒绝该工具，与 permission 同为权限闸门）
#   - tool_after   ：工具执行后记录结果
#   - turn_end     ：每轮收尾
#
# 想“更换”它：直接用新版内容覆盖本目录里的 hook.py（或整目录替换同名目录），重启即生效。
import logging
from datetime import datetime
from pathlib import Path

from core.hooks import LifecycleHooks
from core.types import ModelResponse, ToolCall, TurnContext

logger = logging.getLogger(__name__)


class AuditHooks(LifecycleHooks):
    def __init__(self, plugin_dir: Path) -> None:
        # 日志写在插件自己的目录里：整个插件文件夹拷贝/删除时不会影响其他插件
        self._log_path = plugin_dir / "audit.log"

    def _log(self, line: str) -> None:
        try:
            with self._log_path.open("a", encoding="utf-8") as f:
                f.write(f"[{datetime.now().isoformat(timespec='seconds')}] {line}\n")
        except OSError:
            logger.exception("audit 钩子写日志失败: %s", self._log_path)

    async def llm_response(self, ctx: TurnContext, resp: ModelResponse) -> None:
        calls = ", ".join(tc.name for tc in resp.tool_calls) if resp.tool_calls else "（无）"
        content = (resp.content or "").strip()
        preview = content[:80] + "…" if len(content) > 80 else content
        self._log(f"模型回复: {preview!r} | 请求工具: {calls}")

    async def tool_before(self, ctx: TurnContext, tool_call: ToolCall) -> bool:
        # 观察点：在这里返回 False 会拒绝该工具调用（拒绝原因由 loop 回填给模型）
        self._log(f"工具调用前: {tool_call.name} 参数: {tool_call.arguments}")
        return True

    async def tool_after(
        self, ctx: TurnContext, tool_call: ToolCall, result: object, ok: bool
    ) -> None:
        text = str(result)
        preview = text[:80] + "…" if len(text) > 80 else text
        self._log(f"工具调用后: {tool_call.name} ok={ok} 结果: {preview!r}")

    async def turn_end(self, ctx: TurnContext) -> None:
        self._log(f"本轮结束 (turn={ctx.turn}, stop={ctx.stop_reason})")


def create_hook(plugin_dir: Path) -> AuditHooks:
    """loader 契约：入口工厂，接收插件目录，必须返回 LifecycleHooks 实例。"""
    return AuditHooks(plugin_dir)
