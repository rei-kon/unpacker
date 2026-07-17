"""C6 — возвращает ли `get_context_usage()` осмысленные числа на живой сессии.

Метод в API есть (проверено по сигнатуре) — «существует ли он» не вопрос. Вопрос: даёт ли
он осмысленные числа под подпиской. На этом стоит команда `/context` (§5.5, слой 2).

Первая редакция ассертила `usage is not None` — но ContextUsageResponse это TypedDict, то
есть обычный dict: None там невозможен физически, ассерт неопровержим и зелен даже для
пустышки со всеми нулями. Теперь ассертим документированные ключи по существу: после
реального запроса токенов и размера окна не может быть нуля.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeSDKClient
from conftest import canary_options, collect_stream, requires_live_sdk, write_results_note


def _field(usage: Any, key: str) -> Any:
    if isinstance(usage, dict):
        return usage.get(key)
    return getattr(usage, key, None)


@requires_live_sdk
async def test_c6_get_context_usage(brain: Path) -> None:
    async with ClaudeSDKClient(options=canary_options(brain)) as client:
        await client.query("Скажи ровно одно слово: привет")
        await collect_stream(client.receive_response())
        usage = await client.get_context_usage()

    total = _field(usage, "totalTokens")
    mx = _field(usage, "maxTokens")
    pct = _field(usage, "percentage")
    model = _field(usage, "model")

    write_results_note("C6", f"totalTokens={total} maxTokens={mx} percentage={pct} model={model!r}")
    assert isinstance(total, int) and total > 0, (
        f"totalTokens не положителен после запроса: {total!r} — /context не на чем строить"
    )
    assert isinstance(mx, int) and mx > 0, f"maxTokens не положителен: {mx!r}"
    assert isinstance(pct, (int, float)) and 0 <= pct <= 100, f"percentage вне [0,100]: {pct!r}"
