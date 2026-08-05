"""C9 — форма partial-события SDK: на ней держится ВЕСЬ псевдо-стриминг.

Допущение (`engine/core/events.py:67-79`): при `include_partial_messages=True` в потоке
приходят объекты со словарём `.event`, где `type == "message_start"` означает новое
assistant-сообщение, а `type == "content_block_delta"` с `delta["type"] == "text_delta"`
несёт кусок текста. Разбор СТРОГИЙ: не тот `delta["type"]` — дельта молча игнорируется.

Почему это канарейка, а не юнит-тест: в юнит-тестах словари партиалов мы конструируем сами
(`engine/tests/test_events.py`), поэтому смена формы на стороне SDK не покраснеет НИГДЕ —
черновик просто перестанет появляться у ученика при 767 зелёных тестах. Проверить форму
можно только живым запросом.

Существующая C7 (`canaries/test_c7_interrupt.py`) читает `delta["text"]` вообще без проверки
`delta["type"]` — то есть боевой контракт она не покрывает, а маскирует.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeSDKClient
from conftest import (
    DEFAULT_TIMEOUT,
    canary_options,
    requires_live_sdk,
    write_results_note,
)


async def _collect_partials(messages: Any, timeout: float = DEFAULT_TIMEOUT) -> list[dict]:
    """Сырые словари `.event` — ровно то, что разбирает `events.py`, без нашей интерпретации."""
    events: list[dict] = []

    async def _drain() -> None:
        async for message in messages:
            raw = getattr(message, "event", None)
            if isinstance(raw, dict):
                events.append(raw)
                continue
            if type(message).__name__ == "ResultMessage" or hasattr(message, "total_cost_usd"):
                return

    await asyncio.wait_for(_drain(), timeout=timeout)
    return events


@requires_live_sdk
async def test_c9_partial_events_have_the_shape_the_engine_parses(brain: Path) -> None:
    options = canary_options(brain, include_partial_messages=True)
    async with ClaudeSDKClient(options=options) as client:
        await client.query("Напиши три коротких предложения про весну.")
        events = await _collect_partials(client.receive_response())

    types = [e.get("type") for e in events]
    deltas = [e.get("delta") or {} for e in events if e.get("type") == "content_block_delta"]
    delta_types = sorted({d.get("type") for d in deltas})

    write_results_note(
        "C9",
        f"партиалов={len(events)}; типы={sorted(set(types))}; "
        f"типы delta={delta_types}; пример delta={deltas[0] if deltas else None!r}",
    )

    assert events, (
        "партиалов нет вовсе — include_partial_messages не даёт потока, черновика не будет"
    )
    assert "message_start" in types, (
        "нет message_start — черновик не узнает о новом assistant-сообщении (on_reset мёртв)"
    )
    assert "content_block_delta" in types, "нет content_block_delta — печатать в черновик нечего"
    text_deltas = [d for d in deltas if d.get("type") == "text_delta"]
    assert text_deltas, (
        f"ни одной delta с type='text_delta' (встретились {delta_types}) — "
        "разбор в events.py строгий, весь текст будет отброшен молча"
    )
    assert all(isinstance(d.get("text"), str) for d in text_deltas), (
        "в text_delta нет строкового поля text — брать текст неоткуда"
    )
