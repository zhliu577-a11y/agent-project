# tests/test_prompt.py —— Prompt 模板管理
from core.prompt import build_system_prompt


def test_prompt_contains_catalogs_and_empty_fallback() -> None:
    prompt = build_system_prompt(
        mcp=[("time", "查时间")],
        skills=[("code-review", "评审规范")],
        preloads=[],
    )
    assert "time: 查时间" in prompt
    assert "code-review: 评审规范" in prompt
    assert "以下为预载技能" not in prompt

    empty = build_system_prompt(mcp=[], skills=[], preloads=[])
    assert "（暂无）" in empty


def test_prompt_appends_preloaded_skills() -> None:
    prompt = build_system_prompt(
        mcp=[],
        skills=[],
        preloads=[("rules", "必须先写测试")],
    )
    assert "### 技能 rules" in prompt
    assert "必须先写测试" in prompt
