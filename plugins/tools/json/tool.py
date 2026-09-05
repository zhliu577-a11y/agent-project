# plugins/tools/json/tool.py —— 本地工具插件：日常 JSON 处理
#
# 用法示例（加载后自动带插件命名空间）：
#   json__format('{"a":1}', indent=2)   -> 美化后的 JSON 字符串
#   json__get('{"a":{"b":[1,2]}}', "a.b.1") -> "2"
import json as _json

from core.tool import Tool


class FormatJson(Tool):
    name = "format"
    description = (
        "解析并美化 JSON 文本（默认缩进 2，保留中文与 Unicode）；输入不是合法 JSON 时返回错误信息。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "要美化的 JSON 字符串"},
            "indent": {
                "type": "integer",
                "description": "缩进空格数，默认 2",
            },
        },
        "required": ["text"],
    }

    async def execute(self, **kwargs):
        text = kwargs["text"]
        indent = int(kwargs.get("indent", 2))
        try:
            obj = _json.loads(text)
        except _json.JSONDecodeError as exc:
            return f"JSON 解析失败: {exc}"
        return _json.dumps(obj, ensure_ascii=False, indent=indent)


class GetJson(Tool):
    name = "get"
    description = (
        "解析 JSON 并按点路径取值（如 a.b.0、a.0.name；数组用数字下标），"
        "返回取到的值对应的 JSON 文本；路径不存在时返回错误信息。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "要解析的 JSON 字符串"},
            "path": {"type": "string", "description": "点路径，默认 '.' 表示取整个对象"},
        },
        "required": ["text"],
    }

    async def execute(self, **kwargs):
        text = kwargs["text"]
        path = kwargs.get("path", ".")
        try:
            obj = _json.loads(text)
        except _json.JSONDecodeError as exc:
            return f"JSON 解析失败: {exc}"

        node = obj
        if path != ".":
            for segment in path.split("."):
                if isinstance(node, list):
                    try:
                        node = node[int(segment)]
                    except (ValueError, IndexError):
                        return f"路径 {path} 不存在（数组段 {segment} 非法）"
                elif isinstance(node, dict) and segment in node:
                    node = node[segment]
                else:
                    return f"路径 {path} 不存在（缺少段 {segment}）"
        return _json.dumps(node, ensure_ascii=False)


def create_tools(plugin_dir):
    """插件工厂：返回本插件提供的全部 Tool（单个 Tool 或列表均可）。"""
    return [FormatJson(), GetJson()]
