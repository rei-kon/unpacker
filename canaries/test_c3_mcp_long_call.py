"""C3 — переживает ли долгий MCP-вызов (>90с) поток SDK.

Допущение плана (риск #676/#730, корень — TS #114): CLI не сбрасывал таймер активности,
пока MCP-сервер думал, и рвал поток со «Stream closed». Фикс портирован — отсюда пин 0.2.121.

Эта канарейка — единственное, что доказывает, что пин выбран не наугад. Если она красная,
пин не спасает, и §5.1 нужно перепроектировать (быстрые MCP-тулы + CLAUDE_CODE_STREAM_CLOSE_TIMEOUT).

90 секунд — не случайное число: столько жил старый таймер активности. Спим 95, чтобы
пройти границу с запасом.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, create_sdk_mcp_server, tool
from conftest import requires_live_sdk, write_results_note

from engine.core.streaming import collect_response_with_session

SLOW_PONG = "SLOW-PONG-9C1D"
SLEEP_SECONDS = 95


@tool("slow_ping", "Медленный пинг: спит заданное число секунд, потом отвечает.", {"seconds": int})
async def slow_ping(args: dict[str, Any]) -> dict[str, Any]:
    await asyncio.sleep(int(args.get("seconds", SLEEP_SECONDS)))
    return {"content": [{"type": "text", "text": SLOW_PONG}]}


@requires_live_sdk
async def test_c3_long_mcp_call_survives(brain: Path) -> None:
    server = create_sdk_mcp_server(name="canary", version="1.0.0", tools=[slow_ping])
    options = ClaudeAgentOptions(
        cwd=str(brain),
        permission_mode="bypassPermissions",
        setting_sources=["user", "project"],
        mcp_servers={"canary": server},
        allowed_tools=["mcp__canary__slow_ping"],
        max_turns=5,
    )

    async with ClaudeSDKClient(options=options) as client:
        await client.query(
            f"Вызови инструмент slow_ping со значением seconds={SLEEP_SECONDS} "
            "и верни его ответ дословно, без своих комментариев."
        )
        # Запас поверх 95с сна: сам вызов + раздумья модели
        text, _ = await asyncio.wait_for(
            collect_response_with_session(client.receive_response()),
            timeout=SLEEP_SECONDS + 145,
        )

    write_results_note("C3", f"MCP-вызов на {SLEEP_SECONDS}с, ответ: {text!r}")
    assert SLOW_PONG in text, (
        f"долгий MCP-вызов ({SLEEP_SECONDS}с) не пережил поток на пине 0.2.121 — "
        f"допущение риска #676/#730 ложно, пин не спасает. Ответ: {text!r}"
    )
