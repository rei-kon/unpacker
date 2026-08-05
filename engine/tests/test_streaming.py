"""Сборка ответа из сообщений Agent SDK + нарезка под лимит Telegram.

Фейки повторяют структуру SDK (duck-typing), чтобы тесты не тянули сам claude-agent-sdk:
- TextBlock: объект с .text
- AssistantMessage: объект с .content = [блоки]
- ResultMessage: объект с .total_cost_usd (и именем класса ResultMessage)
"""

from engine.core.streaming import (
    DELIVERY_LIMIT,
    TELEGRAM_LIMIT,
    _utf16_units,
    collect_response_with_session,
    extract_text,
    is_result,
    split_for_delivery,
    split_message,
    tail_by_units,
)


class FakeTextBlock:
    def __init__(self, text):
        self.text = text


class FakeToolBlock:
    """блок без .text (напр. tool_use) — не должен ломать сборку"""

    def __init__(self, name):
        self.name = name


class AssistantMessage:
    def __init__(self, content):
        self.content = content


class ResultMessage:
    def __init__(self, cost=0.0, session_id=None):
        self.total_cost_usd = cost
        self.session_id = session_id


def test_extract_text_from_assistant_message():
    m = AssistantMessage([FakeTextBlock("привет"), FakeTextBlock(", мир")])
    assert extract_text(m) == "привет, мир"


def test_extract_text_ignores_non_text_blocks():
    m = AssistantMessage([FakeToolBlock("Bash"), FakeTextBlock("готово")])
    assert extract_text(m) == "готово"


def test_extract_text_from_result_is_empty():
    assert extract_text(ResultMessage(0.01)) == ""


def test_is_result_true_only_for_result():
    assert is_result(ResultMessage()) is True
    assert is_result(AssistantMessage([FakeTextBlock("x")])) is False


async def _aiter(items):
    for it in items:
        yield it


async def test_collect_response_keeps_only_last_message():
    # в чат — только финал, промежуточная болтовня агента отбрасывается
    msgs = [
        AssistantMessage([FakeTextBlock("шаг1: читаю скилл...")]),
        AssistantMessage([FakeTextBlock("шаг2: генерирую...")]),
        AssistantMessage([FakeTextBlock("Готово: карусель собрана.")]),
        ResultMessage(0.02),
        AssistantMessage([FakeTextBlock("после результата — игнор")]),
    ]
    text, _ = await collect_response_with_session(_aiter(msgs))
    assert text == "Готово: карусель собрана."


async def test_collect_response_skips_trailing_empty_message():
    # если последнее сообщение пустое — берём предыдущее непустое
    msgs = [
        AssistantMessage([FakeTextBlock("Финальный ответ.")]),
        AssistantMessage([FakeToolBlock("Bash")]),  # без текста
        ResultMessage(0.01),
    ]
    text, _ = await collect_response_with_session(_aiter(msgs))
    assert text == "Финальный ответ."


async def test_collect_response_empty_yields_empty():
    text, _ = await collect_response_with_session(_aiter([ResultMessage()]))
    assert text == ""


async def test_collect_with_session_captures_id():
    msgs = [
        AssistantMessage([FakeTextBlock("ответ")]),
        ResultMessage(0.01, session_id="sess-xyz"),
    ]
    text, sid = await collect_response_with_session(_aiter(msgs))
    assert text == "ответ"
    assert sid == "sess-xyz"


async def test_collect_with_session_none_when_absent():
    msgs = [AssistantMessage([FakeTextBlock("ответ")]), ResultMessage()]
    text, sid = await collect_response_with_session(_aiter(msgs))
    assert text == "ответ"
    assert sid is None


def test_split_message_short_stays_single():
    assert split_message("коротко", limit=4096) == ["коротко"]


def test_split_message_splits_long():
    text = "a" * 10000
    chunks = split_message(text, limit=4096)
    assert all(len(c) <= 4096 for c in chunks)
    assert "".join(chunks) == text


def test_split_message_empty_returns_placeholder():
    # пустой ответ не должен падать при отправке в Telegram
    assert split_message("", limit=4096) == ["…"]


def test_split_message_counts_utf16_not_codepoints():
    # эмодзи вне BMP = 2 единицы UTF-16. Каждый кусок должен влезать в лимит Telegram
    # именно по UTF-16, а не по числу символов.
    text = "😀" * 5000  # 5000 кодпойнтов, но 10000 единиц UTF-16
    chunks = split_message(text, limit=4096)
    assert all(_utf16_units(c) <= 4096 for c in chunks)
    assert "".join(chunks) == text
    # по кодпойнтам поместилось бы в 2 куска, по UTF-16 — минимум 3
    assert len(chunks) >= 3


def test_split_message_never_splits_surrogate_pair():
    # каждый кусок сам по себе — валидная строка (суррогат не разорван)
    chunks = split_message("😀" * 3000, limit=100)
    for c in chunks:
        c.encode("utf-16-le")  # не бросает — пара цела


# ── A11: нарезка финала с запасом и по границам абзацев ──────────────────────


def test_split_for_delivery_keeps_headroom_under_telegram_limit():
    """Экранирование MarkdownV2 добавляет символы уже ПОСЛЕ нарезки: кусок ровно на 4096
    после `\\`-эскейпа перевалит лимит, Telegram вернёт 400, и весь кусок потеряет разметку.
    Поэтому финал режем с запасом."""
    chunks = split_for_delivery("абв\n\n" * 4000)
    assert chunks, "нарезка не должна отдавать пустой список"
    assert all(_utf16_units(c) <= DELIVERY_LIMIT for c in chunks)
    assert DELIVERY_LIMIT < TELEGRAM_LIMIT, "запас на экранирование обязан быть"


def test_split_for_delivery_cuts_on_paragraph_boundary():
    """Разрез посреди ```-блока разваливает разметку обоих кусков — режем по абзацам."""
    para = "а" * 1000
    text = "\n\n".join([para] * 8)
    chunks = split_for_delivery(text)
    assert len(chunks) > 1
    for c in chunks[:-1]:
        assert c.endswith(para), f"кусок оборван не по границе абзаца: {c[-20:]!r}"
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_split_for_delivery_falls_back_to_hard_cut_on_one_long_paragraph():
    """Абзац сам длиннее лимита — режем жёстко, а не отдаём кусок, который Telegram отвергнет."""
    chunks = split_for_delivery("я" * 9000)
    assert len(chunks) >= 3
    assert all(_utf16_units(c) <= DELIVERY_LIMIT for c in chunks)
    assert "".join(chunks) == "я" * 9000


def test_split_for_delivery_short_text_is_untouched():
    assert split_for_delivery("короткий ответ") == ["короткий ответ"]


def test_split_for_delivery_empty_returns_placeholder():
    assert split_for_delivery("") == ["…"]


# ── A5: хвост по единицам UTF-16 (скользящее окно черновика) ─────────────────


def test_tail_by_units_returns_the_end_of_the_text():
    assert tail_by_units("абвгде", 3) == "где"


def test_tail_by_units_counts_utf16_not_codepoints():
    tail = tail_by_units("😀" * 100, 10)
    assert _utf16_units(tail) <= 10
    tail.encode("utf-16-le")  # суррогатная пара не разорвана


def test_tail_by_units_shorter_text_is_returned_whole():
    assert tail_by_units("два", 100) == "два"
