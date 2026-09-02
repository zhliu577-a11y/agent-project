# mcp_servers/time_server.py —— 示例 MCP 服务器：提供一个"当前时间"工具
from datetime import datetime
from zoneinfo import ZoneInfo

from mcp.server.mcpserver import MCPServer

server = MCPServer(name="time-server")


@server.tool()
def get_current_time(timezone: str = "Asia/Shanghai") -> str:
    """返回指定时区（IANA 名称，如 Asia/Shanghai、UTC）的当前时间。"""
    try:
        return datetime.now(ZoneInfo(timezone)).isoformat(timespec="seconds")
    except Exception:
        # 时区数据不可用时，退回到系统本地时间，保证工具永不报错
        return datetime.now().astimezone().isoformat(timespec="seconds")


if __name__ == "__main__":
    server.run()