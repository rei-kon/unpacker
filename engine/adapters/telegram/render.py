"""Рендер AskResult → текст для чата (§5.4 UX ошибок). Чистые функции — тестируемо."""

from __future__ import annotations

from engine.core.agent import AskResult
from engine.core.events import ToolStarted

_PLACEHOLDER = "…"


def render_result(result: AskResult) -> str:
    """Превратить результат ask в текст сообщения пользователю по контракту §5.4."""
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
    return _render_other_error(result)


# Частые причины other_error на человеческом языке. Раньше здесь было глухое «детали
# в логах» — и это вводило в заблуждение дважды: причина не логировалась вообще, а
# пользователь не понимал, повторять запрос или чинить бота. Ключи ищем в нижнем
# регистре по detail; порядок важен — от частного к общему.
_KNOWN_CAUSES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("connection closed", "connection reset", "connection aborted", "incomplete"),
     "Связь с API оборвалась на середине ответа. Это разовый сбой сети — просто повтори запрос."),
    (("overloaded", "529"),
     "Сервис перегружен и отказал во время ответа. Подожди минуту и повтори."),
    (("rate limit", "429", "too many requests"),
     "Упёрлись в лимит запросов. Подожди немного и повтори."),
    (("timeout", "timed out"),
     "Ответ не уложился в отведённое время. Попробуй тот же запрос покороче или повтори."),
    (("quota", "credit", "insufficient"),
     "Кончился лимит на стороне сервиса. Тут повтор не поможет — нужно смотреть тариф."),
)


def _render_other_error(result: AskResult) -> str:
    detail = (result.outcome.detail or "").strip()
    low = detail.lower()
    for markers, human in _KNOWN_CAUSES:
        if any(m in low for m in markers):
            return f"⚠️ {human}"
    if detail:
        # Причина незнакомая — показываем как есть, усечённо. Лучше техническая строка,
        # чем «внутренняя ошибка»: по ней хотя бы можно искать и рассказать владельцу.
        short = detail if len(detail) <= 300 else detail[:300] + "…"
        return f"⚠️ Ошибка: {short}\n\nЕсли повторится — покажи эту строку владельцу."
    return "⚠️ Внутренняя ошибка без деталей — такого быть не должно, скажи владельцу."


def render_tool_status(event: ToolStarted) -> str:
    """Статус-строка для verbose 1 (псевдо-стриминг): «⚙️ запускаю <инструмент>…»."""
    return f"⚙️ запускаю {event.name}…"
