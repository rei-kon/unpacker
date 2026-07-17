"""C1 — виден ли скилл из папки-мозга на Linux.

Допущение плана (§4, риск #268): при явном `setting_sources=["user","project"]`
Claude Code подхватывает скиллы из `.claude/skills/` папки-мозга, на которую указывает cwd.

Почему это важно: скиллы — половина ценности папки-мозга. Если они молча не грузятся на
Linux-VPS (а именно там баг #268 и жил), то агент у ученика будет «пустым» без единой ошибки
в логах. Тихий отказ страшнее громкого.
"""
from __future__ import annotations

from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from conftest import requires_live_sdk, write_results_note

from engine.core.streaming import collect_response_with_session

PONG = "CANARY-PONG-7F3A"


@requires_live_sdk
async def test_c1_skill_from_brain_visible_on_linux(brain_with_skill: Path) -> None:
    options = ClaudeAgentOptions(
        cwd=str(brain_with_skill),
        permission_mode="bypassPermissions",
        # Явно и всегда — иначе скиллы и настройки из .claude/ мозга не подхватятся (§4, п.3)
        setting_sources=["user", "project"],
        max_turns=5,
    )
    async with ClaudeSDKClient(options=options) as client:
        await client.query("Сделай ping, используя скилл canary-ping.")
        text, _ = await collect_response_with_session(client.receive_response())

    write_results_note("C1", f"setting_sources=['user','project'], ответ агента: {text!r}")
    assert PONG in text, (
        "скилл из .claude/skills/ мозга НЕ подхватился при setting_sources=['user','project'] — "
        f"допущение §4 ложно. Ответ агента: {text!r}"
    )
