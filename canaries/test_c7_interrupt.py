"""C7 — рвёт ли `interrupt()` живой стрим, не убивая клиента.

Допущение плана (§5.1): data-plane и control-plane разведены — `/stop` идёт мимо session-lock
и прерывает ту генерацию, которая прямо сейчас идёт. Ревью реализуемости отдельно поймало:
если `/stop` встанет в очередь за генерацией, которую должен прервать, он бесполезен.

Здесь проверяются ровно две вещи, и вторая не менее важна первой:
  1. interrupt во время стрима действительно завершает стрим;
  2. клиент ПЕРЕЖИВАЕТ прерывание и обслуживает следующий запрос.

Если (2) ложно, `/stop` придётся делать через drop-client + новую сессию, то есть ценой
потери контекста — а это уже другой UX и другой раздел плана.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from conftest import requires_live_sdk, write_results_note

from engine.core.streaming import collect_response_with_session

ALIVE = "ALIVE-4B2E"
LET_IT_RUN_SECONDS = 6.0


@requires_live_sdk
async def test_c7_interrupt_during_stream(brain: Path) -> None:
    options = ClaudeAgentOptions(
        cwd=str(brain),
        permission_mode="bypassPermissions",
        setting_sources=["user", "project"],
        max_turns=10,
    )

    async with ClaudeSDKClient(options=options) as client:
        await client.query(
            "Считай вслух от 1 до 500: по одному числу на строку, ничего кроме чисел. "
            "Не останавливайся и не сокращай."
        )

        consumer = asyncio.create_task(collect_response_with_session(client.receive_response()))
        await asyncio.sleep(LET_IT_RUN_SECONDS)  # дать генерации реально пойти

        # Control-plane: бьём по живой ссылке на клиента, мимо всякой очереди сообщений
        await client.interrupt()

        try:
            text, _ = await asyncio.wait_for(consumer, timeout=60)
            interrupted = f"стрим завершился после interrupt, хвост: {text[-120:]!r}"
        except TimeoutError:
            consumer.cancel()
            interrupted = "ПОТОК НЕ ЗАКРЫЛСЯ за 60с после interrupt()"
        except Exception as exc:  # noqa: BLE001 — исключение = штатное завершение прерыванием
            interrupted = f"стрим завершился исключением: {exc!r}"

        # Клиент пережил прерывание?
        try:
            await client.query(f"Скажи ровно: {ALIVE}")
            after, _ = await asyncio.wait_for(
                collect_response_with_session(client.receive_response()), timeout=120
            )
        except Exception as exc:  # noqa: BLE001
            after = ""
            interrupted += f" | следующий query упал: {exc!r}"

    write_results_note("C7", f"{interrupted} | ответ после прерывания: {after!r}")
    assert ALIVE in after, (
        "клиент НЕ пережил interrupt() — следующий запрос не обслужен. Допущение §5.1 "
        f"(«/stop мимо lock, клиент остаётся тёплым») ложно. Ответ: {after!r}"
    )
