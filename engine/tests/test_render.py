"""Рендер результата в текст для Telegram — чистые функции (§5.4 UX ошибок)."""

from engine.adapters.telegram.render import render_result
from engine.core.agent import AskResult
from engine.core.errors import Outcome


def _r(kind, text="", detail=""):
    return AskResult(text=text, outcome=Outcome(kind, detail))


def test_ok_returns_text():
    assert render_result(_r("ok", "привет")) == "привет"


def test_ok_empty_text_placeholder():
    assert render_result(_r("ok", "")) == "…"  # пустой ответ не шлём пустым


def test_max_turns_offers_continue():
    out = render_result(_r("max_turns", "частичный ответ"))
    assert "частичный ответ" in out
    assert "лимит" in out.lower() or "продолж" in out.lower()


def test_auth_error_mentions_owner_notified():
    out = render_result(_r("auth_error", "401"))
    assert "владел" in out.lower() or "токен" in out.lower()


def test_exec_error_message():
    out = render_result(_r("exec_error", ""))
    assert "сбой" in out.lower() or "ошибк" in out.lower()


def test_other_error_generic():
    out = render_result(_r("other_error", "boom"))
    assert "ошибк" in out.lower()


# ── B4/B2: перегруз и потерянный контекст — говорим правду, а не «внутренняя ошибка» ─


def test_rate_limited_names_the_limit():
    out = render_result(_r("rate_limited", "429"))
    assert "лимит" in out.lower()
    assert "внутренняя ошибка" not in out.lower()


def test_overloaded_blames_provider_not_engine():
    out = render_result(_r("overloaded", "529"))
    assert "перегру" in out.lower()
    assert "внутренняя ошибка" not in out.lower()


def test_resume_error_explains_lost_context():
    out = render_result(_r("resume_error", ""))
    assert "контекст" in out.lower()


def test_note_is_prepended_to_answer():
    """Пометка «прошлый контекст не восстановился» едет вместе с обычным ответом:
    иначе человек не узнает, что диалог начался с чистого листа."""
    result = AskResult(
        text="вот ответ", outcome=Outcome("ok", ""), note="Прошлый контекст не восстановился."
    )
    out = render_result(result)
    assert out.startswith("Прошлый контекст не восстановился.")
    assert "вот ответ" in out
