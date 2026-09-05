# plugins/tools/text/tool.py —— 示例本地工具插件：进程内直接执行的轻量能力
#
# 一个本地工具插件 = 实现 core.tool.Tool 的类 + 一个工厂函数。
# 工厂接收插件目录（Path），返回单个 Tool 或 Tool 列表；
# 加载器会把每个工具包装成 <插件名>__<工具名>（如 text__slugify），
# 并在启动时直接注册进 ToolRegistry——不需要子进程，也不存在“挂载”。
import re

from core.tool import Tool


class Slugify(Tool):
    name = "slugify"
    description = (
        "把任意文本转成 URL 友好的 slug（小写、非字母数字转连字符）。"
        "适合生成文件名、URL 片段等，高频轻量，进程内即时返回。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "要转换的原始文本"},
        },
        "required": ["text"],
    }

    async def execute(self, **kwargs):
        text = kwargs["text"]
        slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
        return slug or "empty"


class CountWords(Tool):
    name = "count_words"
    description = "统计一段文本的单词数量（按空白切分），适合高频快速统计。"
    parameters = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "要统计的文本"},
        },
        "required": ["text"],
    }

    async def execute(self, **kwargs):
        return str(len(kwargs["text"].split()))


def create_tools(plugin_dir):
    """插件工厂：返回本插件提供的全部 Tool（单个 Tool 或列表均可）。"""
    return [Slugify(), CountWords()]
