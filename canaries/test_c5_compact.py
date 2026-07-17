"""C5 — работает ли `/compact` слэш-строкой в headless.

Разведка по открытому вопросу №1 плана: слэш-строка или программный фолбёк. Ассерта нет —
у нас нет ожидания, только вопрос, требующий записанного ответа (от него зависит, попадёт
ли `/compact` в фазу 2, §5.5 слой 2).

Первая редакция читала ТЕКСТ ответа модели — а «Хорошо, сжимаю историю» неотличимо от
реального сжатия, и разведка ошибалась в обе стороны. Единственный измеримый признак
компакта — падение занятости контекста. Меряем get_context_usage() до и после; факт — числа,
а не болтовня модели.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeSDKClient
from conftest import canary_options, collect_stream, requires_live_sdk, write_results_note


def _tokens(usage: Any) -> Any:
    if isinstance(usage, dict):
        return usage.get("totalTokens")
    return getattr(usage, "totalTokens", None)


@requires_live_sdk
async def test_c5_compact_slash_string(brain: Path) -> None:
    async with ClaudeSDKClient(options=canary_options(brain)) as client:
        # Набиваем историю — компактить пустоту бессмысленно
        for word in ("раз", "два", "три"):
            await client.query(f"Скажи ровно одно слово: {word}")
            await collect_stream(client.receive_response())

        before = _tokens(await client.get_context_usage())

        try:
            await client.query("/compact")
            stream = await collect_stream(client.receive_response())
            outcome = f"ответ={stream.full_text!r}; session_id={stream.session_id}"
        except Exception as exc:  # noqa: BLE001 — исключение это тоже ответ на вопрос
            outcome = f"исключение: {exc!r}"

        after = _tokens(await client.get_context_usage())

        # Живой ли клиент после /compact — отдельный вопрос той же важности
        try:
            await client.query("Скажи ровно одно слово: четыре")
            tail = await collect_stream(client.receive_response())
            alive = f"клиент жив, ответ={tail.full_text!r}"
        except Exception as exc:  # noqa: BLE001
            alive = f"клиент умер после /compact: {exc!r}"

    shrank = isinstance(before, int) and isinstance(after, int) and after < before
    write_results_note(
        "C5",
        f"totalTokens до={before} после={after} (сжалось={shrank}) | {outcome} | после: {alive}",
    )
