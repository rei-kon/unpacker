"""Событийный разбор потока SDK §5.3 — для verbose 1 (псевдо-стриминг).

Поток разбирается в события ОДИН раз и одинаково для любого verbose: ядро не знает, что из
них покажут. Разница только в рендере — verbose 0 молча ждёт Final, verbose 1 показывает шаги
(«запускаю инструмент X» статус-сообщением, которое редактируется). Решает это адаптер
(Срез D); ядро отдаёт события через on_event и не ветвится на уровни.

События (verbose-уровни §5.3 + псевдо-стриминг §9):
  • ToolStarted — начат tool-вызов (verbose ≥1 показывает как статус);
  • TextStart  — началось новое assistant-сообщение (черновик «печатает…» начинается заново);
  • TextDelta  — кусок текста по мере генерации (include_partial_messages);
  • Final — финал: текст последнего содержательного сообщения + session_id + Outcome (§5.4)
    + расход (cost/usage/num_turns) и сырой is_error;

Поток, оборвавшийся без ResultMessage, просто заканчивается без Final — улики такого обрыва
(session_id, был ли прогресс) снимает AgentCore с сырых сообщений, до разбора в события.

Два умолчания, которые выглядят как недоделка, но сделаны нарочно:
  • дельты субагентов (parent_tool_use_id) не эмитятся — в черновик владельца чужая
    болтовня не течёт;
  • из партиалов берётся ТОЛЬКО text_delta: thinking_delta (размышления модели) в черновик
    не идёт — это внутренняя кухня, человеку в чат её показывать незачем.

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
class TextStart:
    pass


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class Final:
    text: str
    session_id: str | None
    outcome: Outcome
    # is_error — сырой флаг ResultMessage, а не производная от outcome.kind: SDK после
    # ошибочного результата намеренно валит CLI-подпроцесс, и ядру нужно знать именно
    # «клиент мёртв», не пересказывая это через классификацию (ревью SDK-ядра, находка 1).
    is_error: bool = False
    # Расход приезжает в каждом финале бесплатно (C4). Поля опциональные: чужой/старый
    # результат без них не должен ронять разбор.
    total_cost_usd: float | None = None
    usage: dict[str, Any] | None = None
    num_turns: int | None = None


Event = ToolStarted | TextStart | TextDelta | Final


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

        raw = getattr(message, "event", None)
        if isinstance(raw, dict):  # StreamEvent (партиал) — у него нет .content и .total_cost_usd
            if getattr(message, "parent_tool_use_id", None):
                continue  # болтовня субагента — не для черновика
            etype = raw.get("type")
            if etype == "message_start":
                yield TextStart()
            elif etype == "content_block_delta":
                delta = raw.get("delta") or {}
                text = delta.get("text")
                if delta.get("type") == "text_delta" and isinstance(text, str):
                    yield TextDelta(text)
            continue

        if is_result(message):
            yield _final(last_text, session_id, message)
            return

        content = getattr(message, "content", None)
        if content is not None:
            for block in content:
                if type(block).__name__ == "ToolUseBlock":
                    yield ToolStarted(getattr(block, "name", "?"))
        chunk = extract_text(message)
        if chunk and chunk.strip():
            last_text = chunk


def _final(text: str, session_id: str | None, result: Any) -> Final:
    """Собрать Final из результата SDK. Расход читаем duck-typing'ом и мягко: поля могут
    отсутствовать (старый SDK, чужой дублёр), и это не повод терять ответ."""
    usage = getattr(result, "usage", None)
    num_turns = getattr(result, "num_turns", None)
    cost = getattr(result, "total_cost_usd", None)
    return Final(
        text=text,
        session_id=session_id,
        outcome=classify_result(result),
        is_error=bool(getattr(result, "is_error", False)),
        total_cost_usd=cost if isinstance(cost, int | float) else None,
        usage=usage if isinstance(usage, dict) else None,
        num_turns=num_turns if isinstance(num_turns, int) else None,
    )
