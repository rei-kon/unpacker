"""A1/A2: финал = ПОСЛЕДНИЙ edit черновика, а не «удалить и прислать заново».

Черновик рождается на первой дельте и умирает как финальный ответ. Практический смысл не
косметический: пока финал приходил новым сообщением, а `finish()` удалял черновик в
`finally`, ответ мог исчезнуть целиком — SDK упал, токен протух, поток кончился без Final,
отправка финала провалилась дважды. Человек читал текст своими глазами, а в чате пусто.

Здесь же каскад отката (MarkdownV2 → плейн → delete+send) и A2 — не-ok исход дописывает
пометку к показанному тексту вместо удаления.
"""

from __future__ import annotations

import asyncio

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from engine.adapters.telegram import draft as draft_module
from engine.adapters.telegram.draft import DraftStreamer, DraftTuning


def _tuning() -> DraftTuning:
    """Быстрый темп без порога дельты: здесь предмет теста — финал, а не троттлинг."""
    return DraftTuning(interval=0.03, min_delta_units=0)


class JournalBot:
    """Дублёр Bot с ЖУРНАЛОМ порядка вызовов — порядок доставки здесь и есть предмет теста."""

    def __init__(self, *, fail_markdown=False, fail_edit=False, not_modified=False):
        self.calls: list[tuple[str, str]] = []  # (метод, текст)
        self.kwargs: list[dict] = []
        self.fail_markdown = fail_markdown
        self.fail_edit = fail_edit
        self.not_modified = not_modified
        self._next_id = 0

    @property
    def methods(self) -> list[str]:
        return [m for m, _ in self.calls]

    def _texts(self, method: str) -> list[str]:
        return [t for m, t in self.calls if m == method]

    @property
    def sent(self) -> list[str]:
        return self._texts("send_message")

    @property
    def edits(self) -> list[str]:
        return self._texts("edit_message_text")

    async def send_message(self, chat_id, text, **kw):
        self.calls.append(("send_message", text))
        self.kwargs.append(kw)
        self._next_id += 1
        message_id = self._next_id

        class M:
            pass

        m = M()
        m.message_id = message_id
        return m

    async def edit_message_text(self, text, chat_id=None, message_id=None, **kw):
        self.calls.append(("edit_message_text", text))
        self.kwargs.append(kw)
        if self.not_modified:
            raise TelegramBadRequest(method=None, message="message is not modified")
        if self.fail_edit:
            raise RuntimeError("message to edit not found")
        if self.fail_markdown and kw.get("parse_mode") == "MarkdownV2":
            raise RuntimeError("can't parse entities")

    async def delete_message(self, chat_id, message_id):
        self.calls.append(("delete_message", str(message_id)))


async def _draft(bot, **kw) -> DraftStreamer:
    d = DraftStreamer(bot, chat_id=1, thread_id=None, tuning=_tuning(), **kw)
    d.on_delta("начало ответа")
    await asyncio.sleep(0.05)  # черновик успел родиться
    return d


# ── A1: счастливый путь ──────────────────────────────────────────────────────


async def test_finalize_edits_the_draft_instead_of_deleting_it():
    bot = JournalBot()
    d = await _draft(bot)
    assert await d.finalize("готовый ответ") is True
    assert "delete_message" not in bot.methods, "на счастливом пути черновик не удаляется"
    assert len(bot.sent) == 1, "сообщение ровно одно — то самое, что печаталось"
    assert "готовый ответ" in bot.edits[-1]


async def test_finalized_draft_has_no_cursor():
    bot = JournalBot()
    d = await _draft(bot)
    await d.finalize("готовый ответ")
    assert "▌" not in bot.edits[-1], "курсор обязан исчезнуть в финале"


async def test_finalize_without_draft_says_so():
    """Дельт не было (модель ответила одним куском) — бот идёт обычным путём _deliver."""
    bot = JournalBot()
    d = DraftStreamer(bot, chat_id=1, thread_id=None, tuning=_tuning())
    assert await d.finalize("ответ без стрима") is False
    assert bot.calls == []


async def test_finalize_stops_the_loop_so_nothing_overwrites_the_final_text():
    bot = JournalBot()
    d = await _draft(bot)
    await d.finalize("итог")
    d.on_delta("опоздавшая дельта")
    await asyncio.sleep(0.08)
    assert bot.edits[-1].startswith("итог"), f"черновик перезаписал финал: {bot.edits[-1]!r}"


async def test_finalize_puts_keyboard_on_the_edit_when_asked():
    bot = JournalBot()
    d = await _draft(bot)
    await d.finalize("итог", markup="КЛАВИАТУРА")
    assert bot.kwargs[-1].get("reply_markup") == "КЛАВИАТУРА"


async def test_finalize_without_markup_does_not_invent_one():
    """Клавиатура — на ПОСЛЕДНЕМ элементе доставки: если дальше идут файлы, edit её не несёт."""
    bot = JournalBot()
    d = await _draft(bot)
    await d.finalize("итог")
    assert bot.kwargs[-1].get("reply_markup") is None


# ── A1: каскад отката ────────────────────────────────────────────────────────


async def test_finalize_falls_back_to_plain_when_markdown_fails():
    bot = JournalBot(fail_markdown=True)
    d = await _draft(bot)
    assert await d.finalize("**жирный** итог") is True
    assert len(bot.edits) >= 2, "после отказа MarkdownV2 обязан быть плейн-edit"
    assert bot.kwargs[-1].get("parse_mode") is None
    assert "жирный" in bot.edits[-1]
    assert "delete_message" not in bot.methods


async def test_finalize_deletes_and_sends_when_edit_is_impossible():
    """Человек удалил черновик руками — edit больше не пройдёт. Честный откат, ответ не теряем."""
    bot = JournalBot(fail_edit=True)
    d = await _draft(bot)
    assert await d.finalize("итог") is True
    assert bot.methods[-2:] == ["send_message", "delete_message"]
    assert "итог" in bot.sent[-1]


async def test_message_is_not_modified_is_a_success_not_a_reason_to_resend():
    """Телеграм уже показывает ровно этот текст — это успех, а не повод удалять и слать заново."""
    bot = JournalBot(not_modified=True)
    d = await _draft(bot)
    assert await d.finalize("итог") is True
    assert "delete_message" not in bot.methods
    assert len(bot.sent) == 1, "второго сообщения быть не должно"


# ── A2: не-ok исход — черновик остаётся с пометкой ───────────────────────────


async def test_abort_keeps_the_text_and_adds_the_note():
    bot = JournalBot()
    d = await _draft(bot)
    assert await d.abort("⚠️ Сбой на стороне движка.") is True
    last = bot.edits[-1]
    assert "начало ответа" in last, "показанный текст обязан остаться — человек его читал"
    assert "Сбой" in last
    assert "delete_message" not in bot.methods


async def test_abort_removes_the_cursor():
    bot = JournalBot()
    d = await _draft(bot)
    await d.abort("⚠️ Сбой.")
    assert "▌" not in bot.edits[-1]


async def test_abort_without_draft_says_so():
    """Черновика нет — пометку доставит бот обычным сообщением."""
    bot = JournalBot()
    d = DraftStreamer(bot, chat_id=1, thread_id=None, tuning=_tuning())
    assert await d.abort("⚠️ Сбой.") is False
    assert bot.calls == []


async def test_abort_never_raises_even_when_telegram_is_dead():
    bot = JournalBot(fail_edit=True)
    d = await _draft(bot)
    assert await d.abort("⚠️ Сбой.") is False, "не смогли дописать — пусть бот скажет сам"


# ── FA1: 429 в финальном тракте ──────────────────────────────────────────────
#
# Тут жила самая дорогая поломка контракта: 429 в финале ловился как «сеть моргнула», и
# каскад MarkdownV2 → плейн → delete+send прожигал ЧЕТЫРЕ вызова внутри флуд-бана, а
# `_resend` успевал удалить черновик до успешной отправки — ответ исчезал целиком.


def _flood(seconds: int = 0) -> TelegramRetryAfter:
    """429 Telegram. retry_after=0 — предмет теста ветка, а не секунды ожидания."""
    return TelegramRetryAfter(
        method=None,  # type: ignore[arg-type]
        message="Flood control exceeded",
        retry_after=seconds,
    )


@pytest.fixture(autouse=True)
def _no_flood_wait(monkeypatch):
    """Добавку к retry_after в тестах обнуляем: ждать её вживую тесту незачем."""
    monkeypatch.setattr(draft_module, "FLOOD_PAD", 0.0)


class FloodBot(JournalBot):
    """Telegram под флудом: первые `allow` вызовов `method` проходят, следующие — 429."""

    def __init__(self, *, method: str, allow: int = 0, floods: int = 1, **kw):
        super().__init__(**kw)
        self.method = method
        self.allow = allow
        self.floods = floods

    def _gate(self, method: str) -> None:
        if method != self.method:
            return
        if self.allow:
            self.allow -= 1
            return
        if self.floods:
            self.floods -= 1
            raise _flood()

    async def send_message(self, chat_id, text, **kw):
        message = await super().send_message(chat_id, text, **kw)
        self._gate("send_message")
        return message

    async def edit_message_text(self, text, chat_id=None, message_id=None, **kw):
        await super().edit_message_text(text, chat_id, message_id, **kw)
        self._gate("edit_message_text")


async def test_finalize_waits_out_the_flood_instead_of_burning_the_cascade():
    """429 — «подожди», а не «эта ступень не работает»: повторяем ТУ ЖЕ ступень."""
    bot = FloodBot(method="edit_message_text")
    d = await _draft(bot)
    assert await d.finalize("итог") is True
    assert len(bot.edits) == 2, f"на 429 ждём и повторяем один раз: {bot.edits}"
    assert bot.kwargs[-1].get("parse_mode") == "MarkdownV2", "повтор ТОЙ ЖЕ ступени каскада"
    assert "delete_message" not in bot.methods, f"каскад прожёг флуд-бан: {bot.methods}"
    assert len(bot.sent) == 1, "второго сообщения быть не должно"


async def test_abort_waits_out_the_flood():
    bot = FloodBot(method="edit_message_text")
    d = await _draft(bot)
    assert await d.abort("⚠️ Сбой.") is True
    assert "начало ответа" in bot.edits[-1]


async def test_second_flood_in_a_row_gives_way_to_the_next_step():
    """Повтор ровно один: ждать флуд-бан бесконечно — та же потеря ответа, только молча."""
    bot = FloodBot(method="edit_message_text", floods=99)
    d = await _draft(bot)
    assert await d.finalize("итог") is True
    assert bot.methods[-2:] == ["send_message", "delete_message"], f"журнал: {bot.methods}"
    assert "итог" in bot.sent[-1]


async def test_resend_sends_the_answer_before_deleting_the_draft():
    """Порядок — весь смысл: delete до успешного send оставлял человека вообще без ответа."""
    bot = JournalBot(fail_edit=True)
    d = await _draft(bot)
    assert await d.finalize("итог") is True
    assert bot.methods[-2:] == ["send_message", "delete_message"], f"журнал: {bot.methods}"


async def test_resend_keeps_the_draft_when_the_new_message_did_not_go():
    """Не смогли отправить заново — показанный текст обязан остаться в чате."""

    class DeadSendBot(JournalBot):
        async def send_message(self, chat_id, text, **kw):
            if self.calls:  # первый send создал черновик, остальные — уже финал
                raise RuntimeError("Telegram недоступен")
            return await super().send_message(chat_id, text, **kw)

    bot = DeadSendBot(fail_edit=True)
    d = await _draft(bot)
    assert await d.finalize("итог") is False
    assert "delete_message" not in bot.methods, "черновик стёрли, а ответ не доставили"


async def test_resend_waits_out_the_flood_on_the_new_message():
    bot = FloodBot(method="send_message", allow=1, fail_edit=True)
    d = await _draft(bot)
    assert await d.finalize("итог") is True
    assert "итог" in bot.sent[-1]


async def test_finalize_never_raises_even_when_everything_fails():
    class DeadBot(JournalBot):
        async def edit_message_text(self, *a, **kw):
            raise RuntimeError("Telegram недоступен")

        async def delete_message(self, *a, **kw):
            raise RuntimeError("Telegram недоступен")

        async def send_message(self, chat_id, text, **kw):
            if self.calls:
                raise RuntimeError("Telegram недоступен")
            return await super().send_message(chat_id, text, **kw)

    bot = DeadBot()
    d = await _draft(bot)
    assert await d.finalize("итог") is False, "не доставили — бот попробует обычным путём"
