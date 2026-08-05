"""Рендер AskResult → текст для чата (§5.4 UX ошибок). Чистые функции — тестируемо."""

from __future__ import annotations

from engine.core.agent import AskResult
from engine.core.events import ToolStarted

_PLACEHOLDER = "…"
# Исходы, где текст ответа НЕ пересказываем: написанное человек уже читает в черновике, и
# пометка дописывается К НЕМУ (`draft.abort`). Повтор дал бы ровно тот дубль «показанный
# хвост + полный текст», который лечит доставка не-ok исходов нарезкой.
NOTE_ONLY_KINDS = ("stopped",)


def render_result(result: AskResult) -> str:
    """Превратить результат ask в текст сообщения пользователю по контракту §5.4."""
    body = _body(result)
    note = result.note
    return f"{note}\n\n{body}".strip() if note else body


def _body(result: AskResult) -> str:
    kind = result.outcome.kind
    text = result.text.strip()
    if kind == "ok":
        return text or _PLACEHOLDER
    if kind == "max_turns":
        note = "⏸ Задача упёрлась в лимит шагов. Напиши «продолжи», чтобы двигаться дальше."
        return f"{text}\n\n{note}".strip() if text else note
    if kind == "auth_error":
        return "⚠️ Токен подписки протух — я уже сообщил владельцу. Скоро починят."
    if kind == "exec_error":
        return "⚠️ Сбой выполнения на стороне движка. Попробуй ещё раз."
    if kind == "rate_limited":
        # Лимит провайдера — не поломка движка. Врать «внутренняя ошибка» здесь дороже
        # всего: человек идёт чинить то, что не сломано.
        return "⏳ Упёрлись в лимит запросов к модели. Подожди пару минут и повтори."
    if kind == "overloaded":
        return "⏳ Модель сейчас перегружена на стороне провайдера. Повтори через минуту."
    if kind == "stopped":
        # Остановка по просьбе человека — не сбой. Сказать здесь «внутренняя ошибка» сразу
        # после честного «⏹ Прервал» значит соврать ему в его же ответ.
        return "⏹ Остановил — задача прервана по твоей просьбе."
    if kind == "resume_error":
        return "⚠️ Прошлый контекст диалога не восстановился. Напиши ещё раз — начну заново."
    return "⚠️ Внутренняя ошибка, детали в логах."


def render_tool_status(event: ToolStarted) -> str:
    """Статус-строка для verbose 1 (псевдо-стриминг): «⚙️ запускаю <инструмент>…»."""
    return f"⚙️ запускаю {event.name}…"
