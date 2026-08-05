"""Черновик «печатает…» не показывает служебный маркер `[SEND_FILE:]`.

Из residual-раздела ревью: маркер вырезается только в ФИНАЛЕ (`_deliver`), а псевдо-стриминг
показывает его как есть. Пока агент печатает, человек видит в черновике
`[SEND_FILE:/home/unpacker/agents/office/state/отчёт.pdf]` — непонятная строка плюс
раскладка каталогов сервера, и только потом она исчезает.
"""

from __future__ import annotations

import asyncio

from engine.adapters.telegram.draft import DraftStreamer
from engine.tests.test_draft import FakeBot, _tuning


async def _drain(seconds: float) -> None:
    await asyncio.sleep(seconds)


async def test_draft_hides_the_send_file_marker():
    bot = FakeBot()
    d = DraftStreamer(bot, chat_id=1, thread_id=None, tuning=_tuning(0.05))
    d.on_delta("Готово, держи [SEND_FILE:/home/unpacker/agents/office/state/отчёт.pdf] — забирай")
    await _drain(0.08)
    await d.finish()
    shown = " ".join(bot.sent + bot.edits)
    assert "SEND_FILE" not in shown, f"служебный маркер в черновике: {shown!r}"
    assert "/home/unpacker" not in shown, "путь на сервере человеку не нужен"
    assert "Готово" in shown, "сам текст ответа в черновике остаётся"


async def test_draft_hides_a_marker_arriving_by_pieces():
    """Дельты рвут маркер посередине — с чем и живёт настоящий стриминг."""
    bot = FakeBot()
    d = DraftStreamer(bot, chat_id=1, thread_id=None, tuning=_tuning(0.05))
    for piece in ("Вот ", "[SEND", "_FILE:", "kp.pdf]", " держи"):
        d.on_delta(piece)
    await _drain(0.08)
    await d.finish()
    shown = " ".join(bot.sent + bot.edits)
    assert "SEND_FILE" not in shown
    assert "Вот" in shown and "держи" in shown


async def test_draft_hides_a_half_typed_marker():
    """Маркер ещё не закрыт `]` — самый частый кадр стриминга. Показывать нечего."""
    bot = FakeBot()
    d = DraftStreamer(bot, chat_id=1, thread_id=None, tuning=_tuning(0.05))
    d.on_delta("Секунду, собираю [SEND_FILE:state/отч")
    await _drain(0.08)
    await d.finish()
    shown = " ".join(bot.sent + bot.edits)
    assert "SEND_FILE" not in shown, f"недописанный маркер тоже мусор: {shown!r}"
    assert "Секунду" in shown


async def test_draft_without_markers_is_untouched():
    bot = FakeBot()
    d = DraftStreamer(bot, chat_id=1, thread_id=None, tuning=_tuning(0.05))
    d.on_delta("Обычный ответ без вложений")
    await _drain(0.08)
    await d.finish()
    assert "Обычный ответ без вложений" in (bot.sent + bot.edits)[0]
