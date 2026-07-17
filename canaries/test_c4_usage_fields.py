"""C4 — читаются ли поля учёта токенов, и что они включают.

Допущение плана (§5.5): считать надо по `model_usage` (включает субагентов, с разбивкой
по моделям) и `total_cost_usd`, а НЕ по `usage` — тот субагентов не включает.
Это документированное поведение, не баг, поэтому берём правильные поля сразу.

Делим на два:
  A. жёсткий ассерт — поля вообще читаются и осмысленны (на этом стоит `/usage` и лимиты);
  B. фиксация факта — как соотносятся `usage` и `model_usage`, когда работал субагент.
     Ассерта нет: заставить модель гарантированно поднять субагента одним промптом нельзя,
     а канарейка, которая падает из-за настроения модели, — шум, а не сигнал.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from conftest import requires_live_sdk, write_results_note

from engine.core.streaming import is_result


def _options(brain: Path, max_turns: int = 5) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        cwd=str(brain),
        permission_mode="bypassPermissions",
        setting_sources=["user", "project"],
        max_turns=max_turns,
    )


async def _final_result(client: ClaudeSDKClient) -> Any:
    """Вернуть ResultMessage целиком — streaming-модуль ядра отдаёт только текст."""
    async for message in client.receive_response():
        if is_result(message):
            return message
    raise AssertionError("поток кончился, а ResultMessage не пришёл")


@requires_live_sdk
async def test_c4a_usage_fields_are_readable(brain: Path) -> None:
    """A. Опора: total_cost_usd и model_usage читаются и не пустые."""
    async with ClaudeSDKClient(options=_options(brain)) as client:
        await client.query("Скажи ровно одно слово: привет")
        result = await _final_result(client)

    cost = getattr(result, "total_cost_usd", None)
    model_usage = getattr(result, "model_usage", None)
    usage = getattr(result, "usage", None)

    write_results_note(
        "C4a",
        f"total_cost_usd={cost!r}; model_usage={model_usage!r}; usage={usage!r}",
    )
    assert isinstance(cost, float) and cost > 0, (
        f"total_cost_usd не читается как положительное число: {cost!r} — §5.5 на нём стоит"
    )
    assert model_usage, f"model_usage пуст: {model_usage!r} — считать расход не по чему"


@requires_live_sdk
async def test_c4b_subagent_usage_shape(brain: Path) -> None:
    """B. Разведка: включает ли model_usage субагента, а usage — нет."""
    async with ClaudeSDKClient(options=_options(brain, max_turns=12)) as client:
        await client.query(
            "Запусти субагента через инструмент Task: пусть он вернёт слово ЭХО. "
            "Потом ответь одним словом: готово"
        )
        result = await _final_result(client)

    model_usage = getattr(result, "model_usage", None)
    usage = getattr(result, "usage", None)
    models = list(model_usage.keys()) if isinstance(model_usage, dict) else None

    write_results_note(
        "C4b",
        f"после запроса с субагентом: модели в model_usage={models!r}; "
        f"model_usage={model_usage!r}; usage={usage!r}",
    )
