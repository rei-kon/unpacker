"""C4 — читаются ли поля учёта токенов, и что они включают.

Допущение (§5.5): считать по `model_usage` (включает субагентов) и `total_cost_usd`, а НЕ
по `usage` (субагентов не включает).

A. Опора: на чём реально стоит /usage — `model_usage` непустой и содержит токены. Про cost:
   ассертим наличие и тип, но НЕ `>0` под подпиской. Первая редакция требовала cost>0 —
   а под подпиской (единственный документированный auth канареек) плата за токен не
   начисляется, cost=0.0 законен, и ассерт краснел бы по причине, не связанной с §5.5.
B. Разведка: включает ли model_usage субагента, в отличие от usage. Ассерта нет — заставить
   модель гарантированно поднять субагента промптом нельзя. Но факт самопроверяем: если Task
   в потоке не встретился, честно пишем «проба не состоялась», а не выдаём отсутствие данных
   за ответ (образец — probe_c8_topics: «топиков нет» ≠ «никто не написал»).
"""

from __future__ import annotations

from pathlib import Path

from claude_agent_sdk import ClaudeSDKClient
from conftest import (
    auth_context,
    canary_options,
    collect_stream,
    requires_live_sdk,
    write_results_note,
)


@requires_live_sdk
async def test_c4a_usage_fields_are_readable(brain: Path) -> None:
    async with ClaudeSDKClient(options=canary_options(brain)) as client:
        await client.query("Скажи ровно одно слово: привет")
        stream = await collect_stream(client.receive_response())

    result = stream.result
    assert result is not None, "ResultMessage не пришёл — учёт токенов брать неоткуда"
    cost = getattr(result, "total_cost_usd", None)
    model_usage = getattr(result, "model_usage", None)
    usage = getattr(result, "usage", None)
    ctx = auth_context()

    write_results_note(
        "C4a",
        f"auth={ctx}; total_cost_usd={cost!r}; model_usage={model_usage!r}; usage={usage!r}",
    )
    assert model_usage, f"model_usage пуст: {model_usage!r} — считать расход не по чему (§5.5)"
    # cost присутствует и типизирован; >0 требуем только на API-контуре, под подпиской 0.0 — норма
    assert cost is None or isinstance(cost, (int, float)), (
        f"total_cost_usd мусорного типа: {cost!r}"
    )
    if ctx == "api":
        assert isinstance(cost, (int, float)) and cost > 0, (
            f"на API-контуре total_cost_usd должен быть >0: {cost!r}"
        )


@requires_live_sdk
async def test_c4b_subagent_usage_shape(brain: Path) -> None:
    """Разведка: включает ли model_usage субагента.

    Прогон 2026-07-18 показал: тул субагента в SDK 0.2.121 зовётся `Agent` (поля
    subagent_type/run_in_background), не `Task`. Разрешаем оба имени — устойчиво к версии.
    """
    options = canary_options(brain, allow=("Agent", "Task"), max_turns=12)
    async with ClaudeSDKClient(options=options) as client:
        await client.query(
            "Запусти субагента (инструмент Agent/Task): пусть вернёт ЭХО. Потом ответь: готово"
        )
        stream = await collect_stream(client.receive_response())

    if not (stream.used_tool("Agent") or stream.used_tool("Task")):
        write_results_note(
            "C4b",
            f"ПРОБА НЕ СОСТОЯЛАСЬ: субагент не запускался (Agent/Task в потоке нет), "
            f"о соотношении usage/model_usage факта нет. tools={stream.tool_uses}",
        )
        return

    result = stream.result
    model_usage = getattr(result, "model_usage", None)
    usage = getattr(result, "usage", None)
    models = list(model_usage.keys()) if isinstance(model_usage, dict) else None
    write_results_note(
        "C4b",
        f"субагент запущен (Task в потоке); модели в model_usage={models}; "
        f"model_usage={model_usage!r}; usage={usage!r}",
    )
