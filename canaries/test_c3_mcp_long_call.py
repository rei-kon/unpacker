"""C3 — переживает ли долгий MCP-вызов (>90с) поток SDK.

Допущение (риск #676/#730): CLI не сбрасывал таймер активности, пока MCP-сервер думал, и
рвал поток со «Stream closed». Фикс портирован — отсюда пин 0.2.121. C3 — единственное,
что доказывает, что пин выбран не наугад.

Первая редакция отдавала длительность на откуп модели (`seconds` шёл аргументом тула): та
могла подставить 5, тул честно спал 5с, маркер приходил, ассерт зеленел — а граница в 90с
не пересекалась ни разу. Теперь:
  • у тула НЕТ параметров — он всегда спит константу SLEEP_SECONDS, модель не влияет;
  • меряем настенное время всего запроса и ассертим, что оно >= 90 (поток реально жил);
  • ассертим, что тул реально вызван (иначе маркер мог прийти мимо MCP).
90 секунд — не случайность: столько жил старый таймер активности. Спим 95 с запасом.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeSDKClient, create_sdk_mcp_server, tool
from conftest import canary_options, collect_stream, requires_live_sdk, write_results_note

SLOW_PONG = "SLOW-PONG-9C1D"
SLEEP_SECONDS = 95
TOOL_NAME = "mcp__canary__slow_ping"


@tool("slow_ping", "Медленный пинг: спит фиксированное время, потом отвечает.", {})
async def slow_ping(_args: dict[str, Any]) -> dict[str, Any]:
    t0 = time.monotonic()
    await asyncio.sleep(SLEEP_SECONDS)  # константа — длительность не отдаём модели
    slept = time.monotonic() - t0
    return {"content": [{"type": "text", "text": f"{SLOW_PONG} slept={slept:.1f}"}]}


@requires_live_sdk
async def test_c3_long_mcp_call_survives(brain: Path) -> None:
    server = create_sdk_mcp_server(name="canary", version="1.0.0", tools=[slow_ping])
    options = canary_options(
        brain,
        mcp_servers={"canary": server},
        allowed_tools=[TOOL_NAME],
    )

    t0 = time.monotonic()
    async with ClaudeSDKClient(options=options) as client:
        await client.query(
            "Вызови инструмент slow_ping (без аргументов) и верни его ответ дословно."
        )
        stream = await collect_stream(client.receive_response(), timeout=SLEEP_SECONDS + 145)
    wall = time.monotonic() - t0

    write_results_note(
        "C3",
        f"tool_used={stream.used_tool(TOOL_NAME)}; wall_clock={wall:.1f}s; "
        f"ответ={stream.full_text!r}",
    )
    assert stream.used_tool(TOOL_NAME), (
        f"модель не вызвала slow_ping — MCP-путь не проверен. tools={stream.tool_uses}"
    )
    assert SLOW_PONG in stream.full_text, (
        f"долгий MCP-вызов не пережил поток на пине 0.2.121 — риск #676/#730 не закрыт. "
        f"Ответ: {stream.full_text!r}"
    )
    assert wall >= 90, (
        f"запрос занял всего {wall:.1f}с — граница 90с НЕ пересечена, факт «пин держит долгие "
        f"вызовы» не доказан (тул отработал быстрее ожидаемого?)"
    )
