# tests/test_plugin_loader.py —— 插件目录解析测试（不需要联网）
from plugin_loader import load_directory


def test_load_directory_only_returns_enabled(tmp_path) -> None:
    cfg = tmp_path / "mcp_servers.json"
    cfg.write_text(
        '{"plugins": ['
        '{"name": "time", "description": "时间", "enabled": true,'
        ' "mcp": {"command": "python", "args": ["mcp_servers/time_server.py"]}},'
        '{"name": "math", "description": "计算", "enabled": false,'
        ' "mcp": {"command": "python", "args": ["mcp_servers/math_server.py"]}}'
        "]}",
        encoding="utf-8",
    )
    entries = load_directory(cfg)
    assert [e.name for e in entries] == ["time"]
