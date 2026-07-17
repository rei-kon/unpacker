"""Событийный разбор потока SDK §5.3 — для verbose 1 (псевдо-стриминг).

verbose 0 обслуживает collect_response_with_session (только финал). Для verbose 1 адаптеру
нужны события ПО ХОДУ: «запускаю инструмент X» — статус-сообщение, которое редактируется.
Здесь ядро превращает поток SDK в события; РЕНДЕРИНГ (статус в чат) — забота адаптера (Срез D).

События (verbose-уровни §5.3):
  • ToolStarted — начат tool-вызов (verbose ≥1 показывает как статус);
  • Final — финал: текст последнего содержательного сообщения + session_id + Outcome (§5.4).

Duck-typing, как в streaming.py — классы SDK не импортируем.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from engine.core.errors import Outcome, classify_result
from engine.core.streaming import extract_text, is_result


@dataclass(frozen=True)
class ToolStarted:
    name: str


@dataclass(frozen=True)
class Final:
    text: str
    session_id: str | None
    outcome: Outcome


Event = ToolStarted | Final


async def stream_events(messages: AsyncIterator[Any]) -> AsyncIterator[Event]:
    """Разобрать поток SDK в события.

    При штатном финале (ResultMessage) выдаёт ровно один Final и останавливается. Если поток
    оборвался без результата (упал subprocess) — Final НЕ выдаётся; вызывающий (_generate)
    ловит это как «поток без Final» и трактует как ошибку (drop клиента), чат не залипает.
    """
    last_text = ""
    session_id: str | None = None

    async for message in messages:
        sid = getattr(message, "session_id", None)
        if isinstance(sid, str) and sid:
            session_id = sid

        if is_result(message):
            yield Final(last_text, session_id, classify_result(message))
            return

        content = getattr(message, "content", None)
        if content is not None:
            for block in content:
                if type(block).__name__ == "ToolUseBlock":
                    yield ToolStarted(getattr(block, "name", "?"))
        chunk = extract_text(message)
        if chunk and chunk.strip():
            last_text = chunk
