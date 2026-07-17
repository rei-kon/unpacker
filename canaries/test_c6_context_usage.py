"""C6 — отвечает ли `get_context_usage()` на живой сессии.

Метод в API есть — это я проверил по сигнатуре до написания канарейки, и «существует ли он»
здесь не вопрос. Вопрос в другом: возвращает ли он осмысленные числа на живой сессии,
подключённой к подписке. На этом стоит команда `/context` (§5.5, слой 2, фаза 2) — мониторинг
занятости контекстного окна.
"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from conftest import requires_live_sdk, write_results_note

from engine.core.streaming import collect_response_with_session


def _describe(obj: Any) -> Any:
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    return obj


@requires_live_sdk
async def test_c6_get_context_usage(brain: Path) -> None:
    options = ClaudeAgentOptions(
        cwd=str(brain),
        permission_mode="bypassPermissions",
        setting_sources=["user", "project"],
        max_turns=5,
    )

    async with ClaudeSDKClient(options=options) as client:
        await client.query("Скажи ровно одно слово: привет")
        await collect_response_with_session(client.receive_response())
        usage = await client.get_context_usage()

    write_results_note("C6", f"get_context_usage() → {type(usage).__name__}: {_describe(usage)!r}")
    assert usage is not None, "get_context_usage() вернул None на живой сессии — /context не построить"
