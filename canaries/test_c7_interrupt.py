"""C7 — рвёт ли `interrupt()` живой стрим, не убивая клиента.

Допущение (§5.1): control-plane разведён с data-plane — `/stop` прерывает идущую генерацию,
клиент остаётся тёплым. На этом стоит весь `/stop` и вытеснение из пула.

Три дефекта первой редакции, закрыты здесь:
  • Ассертилась только живучесть клиента; ветка «ПОТОК НЕ ЗАКРЫЛСЯ» проходила ЗЕЛЁНОЙ.
    Теперь обе половины — жёсткие ассерты с разными сообщениями (они опровергают разные
    куски §5.1 и ведут к разным правкам плана).
  • `sleep(6)` не гарантировал, что генерация идёт: включаем partial-стриминг и ЖДЁМ
    реального старта (появления цифр), а не спим вслепую. Не начала — канарейка НЕ
    СОСТОЯЛАСЬ (внятный fail), а не ложный зелёный.
  • Мозг «отвечай коротко» заставлял модель ответить за 2с — прерывать нечего. Берём
    brain_talky без установки на краткость.
Плюс проверяем обрезанность (счёт не дошёл до 500) — доказательство, что прерывание реально
состоялось, а не просто «стрим завершился сам».
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from claude_agent_sdk import ClaudeSDKClient
from conftest import canary_options, collect_stream, requires_live_sdk, write_results_note

ALIVE = "ALIVE-4B2E"


@requires_live_sdk
async def test_c7_interrupt_during_stream(brain_talky: Path) -> None:
    # include_partial_messages — иначе счёт «до 500» может прийти одним куском в конце,
    # и мы не увидим, что генерация идёт, — прерывать будет нечего или поздно.
    options = canary_options(brain_talky, include_partial_messages=True, max_turns=10)

    async with ClaudeSDKClient(options=options) as client:
        await client.query(
            "Считай вслух от 1 до 500: по одному числу на строку, только числа, "
            "ничего кроме них. Не останавливайся и не сокращай."
        )

        collected = ""
        started = asyncio.Event()

        async def reader() -> None:
            nonlocal collected
            async for message in client.receive_response():
                content = getattr(message, "content", None)
                if content is not None:
                    for block in content:
                        text = getattr(block, "text", None)
                        if isinstance(text, str):
                            collected += text
                # partial-стрим приходит StreamEvent'ами — вытащим текст и оттуда
                ev = getattr(message, "event", None)
                if isinstance(ev, dict):
                    delta = ev.get("delta", {})
                    if isinstance(delta, dict) and isinstance(delta.get("text"), str):
                        collected += delta["text"]
                if (
                    not started.is_set()
                    and re.search(r"\d", collected)
                    and len(collected.strip()) >= 3
                ):
                    started.set()
                if type(message).__name__ == "ResultMessage":
                    return

        task = asyncio.create_task(reader())

        try:
            await asyncio.wait_for(started.wait(), timeout=45)
        except TimeoutError:
            task.cancel()
            pytest.fail("генерация не пошла за 45с — C7 НЕ СОСТОЯЛАСЬ (не проблема §5.1)")

        if task.done():
            pytest.fail(
                "стрим завершился до interrupt (модель ответила слишком быстро/коротко) — "
                "C7 НЕ СОСТОЯЛАСЬ; нужен более длинный счёт или разговорчивее мозг"
            )

        interrupt_err: Exception | None = None
        try:
            await client.interrupt()
        except Exception as exc:  # noqa: BLE001 — исключение это факт, не повод ронять прогон
            interrupt_err = exc

        try:
            await asyncio.wait_for(task, timeout=60)
            closed = True
        except TimeoutError:
            task.cancel()
            closed = False

        reached_end = "500" in collected

        # Клиент пережил прерывание?
        after = ""
        try:
            await client.query(f"Скажи ровно: {ALIVE}")
            tail = await collect_stream(client.receive_response(), timeout=120)
            after = tail.full_text
        except Exception as exc:  # noqa: BLE001
            after = f"[query упал: {exc!r}]"

    write_results_note(
        "C7",
        f"поток_закрылся_после_interrupt={closed}; interrupt_err={interrupt_err!r}; "
        f"счёт_дошёл_до_500={reached_end}; собрано_символов={len(collected)}; "
        f"ответ_после={after!r}",
    )
    assert not reached_end, (
        "счёт дошёл до 500 — прерывание не состоялось, interrupt() ничего не оборвал"
    )
    assert closed, (
        "поток НЕ закрылся за 60с после interrupt() — control-plane §5.1 не работает, "
        "interrupt не рвёт идущую генерацию"
    )
    assert ALIVE in after, (
        "клиент НЕ пережил interrupt() — следующий запрос не обслужен. §5.1 («клиент остаётся "
        f"тёплым») ложно. Ответ: {after!r}"
    )
