# core/hooks.py —— 生命周期钩子（类型化事件）
class LifecycleHooks:
    """插件继承此类，覆写需要关心的方法。不覆写的自动忽略。"""

    def turn_start(self, ctx): ...
    def llm_response(self, ctx, resp): ...
    def tool_before(self, ctx, tool_call): ...
    def tool_after(self, ctx, tool_call, result, ok): ...
    def turn_end(self, ctx): ...


class HookManager:
    """管理多个钩子插件，按注册顺序逐个调用，并隔离异常。"""

    def __init__(self):
        self._hooks = []

    def add(self, hook: LifecycleHooks) -> None:
        self._hooks.append(hook)

    def turn_start(self, ctx):
        for h in self._hooks:
            try:
                h.turn_start(ctx)
            except Exception as e:
                print(f"[hooks] turn_start 出错: {e}")

    def llm_response(self, ctx, resp):
        for h in self._hooks:
            try:
                h.llm_response(ctx, resp)
            except Exception as e:
                print(f"[hooks] llm_response 出错: {e}")

    def tool_before(self, ctx, tool_call):
        for h in self._hooks:
            try:
                h.tool_before(ctx, tool_call)
            except Exception as e:
                print(f"[hooks] tool_before 出错: {e}")

    def tool_after(self, ctx, tool_call, result, ok):
        for h in self._hooks:
            try:
                h.tool_after(ctx, tool_call, result, ok)
            except Exception as e:
                print(f"[hooks] tool_after 出错: {e}")

    def turn_end(self, ctx):
        for h in self._hooks:
            try:
                h.turn_end(ctx)
            except Exception as e:
                print(f"[hooks] turn_end 出错: {e}")