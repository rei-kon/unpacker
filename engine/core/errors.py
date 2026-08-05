"""Классификация ошибок §5.4 — по структурному контракту SDK, не по слепому regex.

План §5.4 требует ветвиться по `ResultMessage.subtype`, а не парсить текст: это
документированное поведение SDK. Здесь — перевод сырого результата/исключения в Outcome,
по которому AgentCore решает поведение:
  • ok               → отдать ответ;
  • max_turns        → «упёрлись в лимит шагов» + предложить продолжить (не ошибка);
  • exec_error       → перезапуск клиента + одна повторная попытка;
  • auth_error       → health-маркер degraded + алерт владельцу (протух OAuth — риск #13);
  • other_error      → честное сообщение в чат, drop клиента.

Duck-typing: не импортируем классы SDK ради устойчивости к версиям (как streaming.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# Закрытая таксономия исходов. Была комментарием у поля — и опечатка в ветке («rate_limted»)
# молча превращалась в other_error: движок честно показывал «внутренняя ошибка» вместо
# «подожди пару минут», а найти это можно было только на живом боте. Literal ловит на mypy.
OutcomeKind = Literal[
    "ok",
    "max_turns",
    "exec_error",
    "auth_error",
    "rate_limited",
    "overloaded",
    "resume_error",
    "stopped",  # прервано человеком (/stop) — не ошибка, см. AgentCore
    "other_error",
]

# Подстроки, надёжно указывающие на протухший/неверный OAuth в тексте исключения (риск #13).
# НЕ голое "401"/"forbidden": они ложно ловят «line 401», «/proj401/», «forbidden characters»
# → ложный health degraded + алерт владельцу. Берём контекстные сочетания (ревью C).
_AUTH_MARKERS = (
    "unauthorized",
    "authentication_failed",
    "authentication failed",
    "invalid_api_key",
    "oauth token",
    "401 unauthorized",
    "http 401",
    "status 401",
    "403 forbidden",
)

# Перегруз провайдера и лимиты. Та же дисциплина, что с 401: голые «429»/«529» не берём —
# они ловят «line 429» и пути вроде /tmp/run529. Только контекстные сочетания.
_RATE_MARKERS = (
    "rate_limit",
    "rate limit",
    "too many requests",
    "http 429",
    "status 429",
    "429 too many",
)
_OVERLOAD_MARKERS = (
    "overloaded_error",
    "overloaded",
    "http 529",
    "status 529",
    "529 overloaded",
    "http 500",
    "internal server error",
    "service unavailable",
)
# Не поднялась прошлая сессия. Дословный текст CLI при мёртвом `--resume`:
# «No conversation found with session ID: <id>» (проверено по бандлу CLI 0.2.121).
_RESUME_MARKERS = (
    "no conversation found",
    "session not found",
    "could not resume",
    "failed to resume",
    "no such session",
)

# HTTP-статусы из ResultMessage.api_error_status — структурный путь, надёжнее текста.
_RATE_STATUSES = frozenset({429})
_OVERLOAD_STATUSES = frozenset({500, 503, 529})


@dataclass(frozen=True)
class Outcome:
    kind: OutcomeKind
    detail: str


def classify_result(result: object) -> Outcome:
    """Классифицировать финальный ResultMessage по subtype/api_error_status."""
    subtype = getattr(result, "subtype", None)
    api_status = getattr(result, "api_error_status", None)
    is_error = bool(getattr(result, "is_error", False))

    if api_status == 401:
        return Outcome("auth_error", "API вернул 401 — токен протух или неверен")
    if api_status in _RATE_STATUSES:
        return Outcome("rate_limited", "провайдер вернул 429 — упёрлись в лимит запросов")
    if api_status in _OVERLOAD_STATUSES:
        return Outcome("overloaded", f"провайдер перегружен (HTTP {api_status})")
    if subtype == "error_max_turns":
        return Outcome("max_turns", "задача упёрлась в лимит шагов")
    if is_error:
        detail = _error_text(result)
        if _matches(detail, _RESUME_MARKERS):
            return Outcome("resume_error", "прошлая сессия не поднялась")
        if _matches(detail, _RATE_MARKERS):
            return Outcome("rate_limited", "упёрлись в лимит запросов")
        if _matches(detail, _OVERLOAD_MARKERS):
            return Outcome("overloaded", "провайдер перегружен")
        if subtype == "error_during_execution":
            return Outcome("exec_error", "ошибка при выполнении")
        return Outcome("other_error", detail or str(subtype or "неизвестная ошибка"))
    if subtype == "error_during_execution":
        return Outcome("exec_error", "ошибка при выполнении")
    return Outcome("ok", "")


def classify_exception(exc: BaseException) -> Outcome:
    """Классифицировать исключение SDK (ProcessError/CLIError/таймаут) по тексту + stderr.

    Лимиты сюда попадают не для красоты: живой баг SDK #812 отдаёт настоящий 429 именно
    исключением, а не результатом, — без этой ветки перегруз выглядел бы поломкой движка.
    """
    parts = [str(exc), str(getattr(exc, "stderr", "") or ""), type(exc).__name__]
    text = " ".join(parts).lower()
    if _matches(text, _AUTH_MARKERS):
        return Outcome("auth_error", "auth-фейл в исключении (протух OAuth?)")
    if _matches(text, _RESUME_MARKERS):
        return Outcome("resume_error", "прошлая сессия не поднялась")
    if _matches(text, _RATE_MARKERS):
        return Outcome("rate_limited", "упёрлись в лимит запросов")
    if _matches(text, _OVERLOAD_MARKERS):
        return Outcome("overloaded", "провайдер перегружен")
    return Outcome("other_error", str(exc) or type(exc).__name__)


def _matches(text: str, markers: tuple[str, ...]) -> bool:
    low = text.lower()
    return any(marker in low for marker in markers)


def _error_text(result: object) -> str:
    """Текст ошибки из результата: SDK кладёт его то в `errors`, то в `result`."""
    errors = getattr(result, "errors", None)
    body = getattr(result, "result", None)
    parts = [str(p) for p in (errors, body) if p]
    return " ".join(parts)
