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


@dataclass(frozen=True)
class Outcome:
    kind: str  # "ok" | "max_turns" | "exec_error" | "auth_error" | "other_error"
    detail: str


def classify_result(result: object) -> Outcome:
    """Классифицировать финальный ResultMessage по subtype/api_error_status."""
    subtype = getattr(result, "subtype", None)
    api_status = getattr(result, "api_error_status", None)
    is_error = bool(getattr(result, "is_error", False))

    if api_status == 401:
        return Outcome("auth_error", "API вернул 401 — токен протух или неверен")
    if subtype == "error_max_turns":
        return Outcome("max_turns", "задача упёрлась в лимит шагов")
    if subtype == "error_during_execution":
        return Outcome("exec_error", "ошибка при выполнении")
    if is_error:
        errors = getattr(result, "errors", None)
        return Outcome("other_error", str(errors or subtype or "неизвестная ошибка"))
    return Outcome("ok", "")


def classify_exception(exc: BaseException) -> Outcome:
    """Классифицировать исключение SDK (ProcessError/CLIError/таймаут) по тексту + stderr."""
    parts = [str(exc), str(getattr(exc, "stderr", "") or ""), type(exc).__name__]
    text = " ".join(parts).lower()
    if any(marker in text for marker in _AUTH_MARKERS):
        return Outcome("auth_error", "auth-фейл в исключении (протух OAuth?)")
    return Outcome("other_error", str(exc) or type(exc).__name__)


def is_auth_error(outcome: Outcome) -> bool:
    return outcome.kind == "auth_error"
