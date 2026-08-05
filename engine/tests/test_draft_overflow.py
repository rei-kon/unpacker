"""A5/A6/A7: переполнение черновика, смена текста и пульс typing.

Три находки ревью про одно и то же — про минуты, в которые человек не понимает, жив ли бот:

  • после 3600 единиц черновик замирал навсегда, а дельты выбрасывались: длинный документ
    печатается три минуты, две из них человек смотрит на мёртвый экран;
  • `TextStart` (цепочка текст → tool → текст) молча подменял уже прочитанный абзац —
    выглядит как сбой, хотя это прогресс;
  • `send_chat_action` уходил ровно один раз, индикатор Telegram живёт ~5 секунд, а до
    первой дельты бывает минута чтения файлов и MCP-вызовов.
"""

from __future__ import annotations

import asyncio

from engine.adapters.telegram.draft import DraftStreamer, DraftTuning
from engine.core.streaming import _utf16_units


class ChattyBot:
    """Дублёр Bot, который помнит и текст, и косметические вызовы."""

    def __init__(self, *, typing_fails=False):
        self.sent: list[str] = []
        self.edits: list[str] = []
        self.actions: list[str] = []
        self.typing_fails = typing_fails
        self._next_id = 0

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text)
        self._next_id += 1
        message_id = self._next_id

        class M:
            pass

        m = M()
        m.message_id = message_id
        return m

    async def edit_message_text(self, text, chat_id=None, message_id=None, **kw):
        self.edits.append(text)

    async def delete_message(self, chat_id, message_id):
        pass

    async def send_chat_action(self, chat_id, action, **kw):
        if self.typing_fails:
            raise RuntimeError("flood")
        self.actions.append(action)

    @property
    def shown(self) -> str:
        """Что человек видит в черновике прямо сейчас."""
        return (self.edits or self.sent)[-1]


def _tuning(**kw):
    """Быстрый темп без порога дельты: здесь предмет теста — что показано, а не троттлинг."""
    return DraftTuning(interval=0.03, min_delta_units=0, **kw)


# ── A5: переполнение — скользящее окно вместо мёртвого экрана ───────────────


async def test_overflow_shows_the_fresh_tail_not_a_frozen_head():
    bot = ChattyBot()
    d = DraftStreamer(bot, chat_id=1, thread_id=None, tuning=_tuning(max_units=200))
    d.on_delta("а" * 400)
    await asyncio.sleep(0.05)
    d.on_delta("свежий хвост генерации")
    await asyncio.sleep(0.06)
    assert "свежий хвост генерации" in bot.shown, "человек должен видеть, что генерация идёт"


async def test_overflow_marks_the_cut_with_a_leading_ellipsis():
    bot = ChattyBot()
    d = DraftStreamer(bot, chat_id=1, thread_id=None, tuning=_tuning(max_units=200))
    d.on_delta("а" * 400)
    await asyncio.sleep(0.05)
    assert bot.shown.startswith("…"), f"начало обрезано — это надо показать: {bot.shown[:20]!r}"


async def test_overflow_says_how_much_is_written():
    """Строка-счётчик: текст не прыгает под глазами, но видно, что работа идёт."""
    bot = ChattyBot()
    d = DraftStreamer(bot, chat_id=1, thread_id=None, tuning=_tuning(max_units=200))
    d.on_delta("а" * 400)
    await asyncio.sleep(0.05)
    assert "продолжаю" in bot.shown and "400" in bot.shown


async def test_overflow_window_stays_within_the_limit():
    bot = ChattyBot()
    d = DraftStreamer(bot, chat_id=1, thread_id=None, tuning=_tuning(max_units=200))
    d.on_delta("я" * 2000)
    await asyncio.sleep(0.05)
    assert _utf16_units(bot.shown) <= 200, f"окно шире лимита: {_utf16_units(bot.shown)}"


async def test_overflow_does_not_throw_deltas_away():
    """Дельты после переполнения копятся: иначе отдать хвост при обрыве будет неоткуда."""
    bot = ChattyBot()
    d = DraftStreamer(bot, chat_id=1, thread_id=None, tuning=_tuning(max_units=200))
    d.on_delta("а" * 400)
    await asyncio.sleep(0.05)
    d.on_delta("б" * 600)
    await asyncio.sleep(0.06)
    assert "1000" in bot.shown, f"счётчик не увидел новые дельты: {bot.shown[-40:]!r}"


async def test_finalize_after_overflow_shows_the_whole_answer():
    """Окно — только про показ по ходу: финал приходит целиком."""
    bot = ChattyBot()
    d = DraftStreamer(bot, chat_id=1, thread_id=None, tuning=_tuning(max_units=200))
    d.on_delta("а" * 400)
    await asyncio.sleep(0.05)
    await d.finalize("полный ответ целиком")
    assert bot.edits[-1] == "полный ответ целиком"


async def test_overflow_window_keeps_moving_with_the_threshold_on():
    """FA5: порог прироста меряет ПОКАЗАННОЕ, а в режиме хвоста его длина константна.

    Из-за этого grown всегда выходил нулём, показ откладывался «до следующего раза» — и
    окно замирало до самого финала. Ровно та мёртвая картинка, ради которой окно и делали.
    """
    bot = ChattyBot()
    d = DraftStreamer(
        bot,
        chat_id=1,
        thread_id=None,
        tuning=DraftTuning(interval=0.03, min_delta_units=60, max_units=200),
    )
    d.on_delta("а" * 400)
    await asyncio.sleep(0.05)
    d.on_delta("свежий хвост генерации" + "б" * 100)
    await asyncio.sleep(0.06)
    assert "свежий хвост генерации" in bot.shown, f"окно замерло: {bot.shown[-60:]!r}"


async def test_overflow_still_skips_tiny_deltas():
    """Порог не отменён: мелкая правка и в режиме хвоста не стоит вызова API."""
    bot = ChattyBot()
    d = DraftStreamer(
        bot,
        chat_id=1,
        thread_id=None,
        tuning=DraftTuning(interval=0.03, min_delta_units=60, max_units=200),
    )
    d.on_delta("а" * 400)
    await asyncio.sleep(0.05)
    before = len(bot.edits)
    d.on_delta("+++")
    await asyncio.sleep(0.06)
    assert len(bot.edits) == before, "три символа не стоят правки сообщения"


# ── A6: TextStart — переходная строка вместо немой подмены ──────────────────


async def test_text_start_shows_a_transition_instead_of_a_silent_swap():
    bot = ChattyBot()
    d = DraftStreamer(bot, chat_id=1, thread_id=None, tuning=_tuning())
    d.on_delta("первый абзац, который человек уже читает")
    await asyncio.sleep(0.05)
    d.on_reset()  # агент полез в тул: текст сменится
    await asyncio.sleep(0.05)
    assert "первый абзац" in bot.shown, "прочитанное не должно исчезать молча"
    assert "работаю дальше" in bot.shown, "исчезновение обязано читаться как прогресс"


async def test_transition_gives_way_to_the_new_text():
    bot = ChattyBot()
    d = DraftStreamer(bot, chat_id=1, thread_id=None, tuning=_tuning())
    d.on_delta("первый абзац")
    await asyncio.sleep(0.05)
    d.on_reset()
    await asyncio.sleep(0.05)
    d.on_delta("новый текст ответа")
    await asyncio.sleep(0.05)
    assert bot.shown.startswith("новый текст ответа")
    assert "работаю дальше" not in bot.shown


async def test_transition_is_not_shown_when_new_text_came_at_once():
    """Дельты успели прийти до тика — переходную строку показывать незачем."""
    bot = ChattyBot()
    d = DraftStreamer(bot, chat_id=1, thread_id=None, tuning=_tuning())
    d.on_delta("первый абзац")
    await asyncio.sleep(0.05)
    d.on_reset()
    d.on_delta("новый текст")
    await asyncio.sleep(0.05)
    assert "работаю дальше" not in " ".join(bot.edits)


# ── A7: пульс typing до первой дельты ───────────────────────────────────────


async def test_typing_pulses_while_the_agent_is_silent():
    """До первой дельты человек не видит НИЧЕГО: ни черновика, ни статуса при verbose 0."""
    bot = ChattyBot()
    d = DraftStreamer(bot, chat_id=1, thread_id=None, tuning=_tuning(typing_every=0.02))
    d.begin()
    await asyncio.sleep(0.09)
    await d.finish()
    assert len(bot.actions) >= 3, f"индикатор живёт ~5 с — его надо повторять: {bot.actions}"


async def test_typing_stops_once_the_draft_is_visible():
    bot = ChattyBot()
    d = DraftStreamer(bot, chat_id=1, thread_id=None, tuning=_tuning(typing_every=0.02))
    d.begin()
    d.on_delta("пошёл текст")
    await asyncio.sleep(0.06)
    before = len(bot.actions)
    await asyncio.sleep(0.06)
    await d.finish()
    assert len(bot.actions) == before, "черновик виден — typing больше не нужен"


async def test_typing_pulse_has_a_cap():
    """Кап на повторы: молотить chat_action всю долгую генерацию незачем."""
    bot = ChattyBot()
    d = DraftStreamer(
        bot, chat_id=1, thread_id=None, tuning=_tuning(typing_every=0.01, typing_max=2)
    )
    d.begin()
    await asyncio.sleep(0.1)
    await d.finish()
    assert len(bot.actions) <= 2


async def test_typing_failure_does_not_break_anything():
    bot = ChattyBot(typing_fails=True)
    d = DraftStreamer(bot, chat_id=1, thread_id=None, tuning=_tuning(typing_every=0.01))
    d.begin()
    d.on_delta("ответ")
    await asyncio.sleep(0.05)
    assert await d.finalize("итог") is True, "косметика не смеет мешать ответу"
