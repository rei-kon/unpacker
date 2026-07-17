"""C5 — работает ли `/compact` слэш-строкой в headless.

Это не проверка допущения, а **разведка по открытому вопросу №1** плана: «`/compact` в headless
— канарейка фазы 0 решит: слэш-строка или программный фолбэк». Поэтому здесь нет жёсткого
ассерта: у нас нет ожидания, которое можно опровергнуть. Есть вопрос, на который нужен
записанный ответ — от него зависит, попадёт ли `/compact` в фазу 2 (§5.5, слой 2).

Ассерт тут был бы враньём: он превратил бы «мы не знаем» в «мы требуем».
"""
from __future__ import annotations

from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from conftest import requires_live_sdk, write_results_note

from engine.core.streaming import collect_response_with_session


@requires_live_sdk
async def test_c5_compact_slash_string(brain: Path) -> None:
    options = ClaudeAgentOptions(
        cwd=str(brain),
        permission_mode="bypassPermissions",
        setting_sources=["user", "project"],
        max_turns=5,
    )

    async with ClaudeSDKClient(options=options) as client:
        # Сперва набиваем хоть какую-то историю — компактить пустоту бессмысленно
        await client.query("Скажи ровно одно слово: раз")
        await collect_response_with_session(client.receive_response())
        await client.query("Скажи ровно одно слово: два")
        await collect_response_with_session(client.receive_response())

        try:
            await client.query("/compact")
            text, session = await collect_response_with_session(client.receive_response())
            outcome = f"ответ={text!r}; session_id={session!r}"
        except Exception as exc:  # noqa: BLE001 — исключение это тоже ответ на вопрос
            outcome = f"исключение: {exc!r}"

        # Живой ли клиент после /compact — отдельный вопрос той же важности
        try:
            await client.query("Скажи ровно одно слово: три")
            after, _ = await collect_response_with_session(client.receive_response())
            alive = f"клиент жив, ответ={after!r}"
        except Exception as exc:  # noqa: BLE001
            alive = f"клиент умер после /compact: {exc!r}"

    write_results_note("C5", f"/compact слэш-строкой → {outcome} | после: {alive}")
