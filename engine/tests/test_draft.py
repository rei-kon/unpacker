"""Велс-трюк §9: черновик «печатает…» — edit сообщения раз в interval, финал отдельно.

DraftStreamer — sync-приём дельт (on_event зовётся синхронно из ядра, должен быть лёгким)
+ фоновая задача, которая шлёт/редактирует черновик не чаще interval. finish() убирает черновик.
"""

import asyncio

from engine.adapters.telegram.draft import DraftStreamer


class FakeBot:
    """Записывает вызовы; message_id растёт с 1."""

    def __init__(self):
        self.sent: list[str] = []
        self.edits: list[str] = []
        self.deleted: list[int] = []
        self._next_id = 0

    async def send_message(self, chat_id, text, message_thread_id=None):
        self.sent.append(text)
        self._next_id += 1

        class M:
            message_id = self._next_id

        return M()

    async def edit_message_text(self, text, chat_id, message_id):
        self.edits.append(text)

    async def delete_message(self, chat_id, message_id):
        self.deleted.append(message_id)


async def _drain(seconds: float) -> None:
    await asyncio.sleep(seconds)


async def test_draft_sends_then_edits_then_deletes():
    bot = FakeBot()
    d = DraftStreamer(bot, chat_id=1, thread_id=None, interval=0.05)
    d.on_delta("Прив")
    await _drain(0.08)  # первая отправка черновика
    d.on_delta("ет, ")
    d.on_delta("бро")
    await _drain(0.08)  # один edit на две дельты (батч по интервалу)
    await d.finish()
    assert bot.sent, "черновик должен быть отправлен"
    assert bot.sent[0].endswith("▌")
    assert bot.edits and "Привет, бро" in bot.edits[-1]
    assert bot.deleted, "финиш должен удалить черновик"


async def test_no_deltas_no_draft():
    bot = FakeBot()
    d = DraftStreamer(bot, chat_id=1, thread_id=None, interval=0.05)
    await _drain(0.08)
    await d.finish()
    assert not bot.sent and not bot.deleted


async def test_reset_clears_buffer():
    bot = FakeBot()
    d = DraftStreamer(bot, chat_id=1, thread_id=None, interval=0.05)
    d.on_delta("промежуточная болтовня")
    await _drain(0.08)
    d.on_reset()  # новое assistant-сообщение → черновик начинается заново
    d.on_delta("настоящий ответ")
    await _drain(0.08)
    await d.finish()
    assert "настоящий ответ" in bot.edits[-1]
    assert "болтовня" not in bot.edits[-1]


async def test_throttle_batches_edits():
    bot = FakeBot()
    d = DraftStreamer(bot, chat_id=1, thread_id=None, interval=0.2)
    for i in range(50):
        d.on_delta(f"кусок{i} ")
    await _drain(0.25)
    await d.finish()
    # 50 дельт → максимум 1 send + пара edit'ов, не 50 сообщений
    assert len(bot.sent) == 1
    assert len(bot.edits) <= 3


async def test_long_text_freezes_with_ellipsis():
    bot = FakeBot()
    d = DraftStreamer(bot, chat_id=1, thread_id=None, interval=0.03, max_units=100)
    d.on_delta("а" * 500)
    await _drain(0.06)
    d.on_delta("б" * 500)
    await _drain(0.06)
    await d.finish()
    shown = (bot.edits or bot.sent)[-1]
    assert len(shown) <= 110  # обрезан по max_units + маркер
    assert shown.endswith("…")


class SlowBot(FakeBot):
    """send_message с задержкой — окно для гонки finish() во время in-flight send."""

    async def send_message(self, chat_id, text, message_thread_id=None):
        await asyncio.sleep(0.05)
        return await super().send_message(chat_id, text, message_thread_id)


async def test_finish_during_inflight_send_still_deletes_draft():
    bot = SlowBot()
    d = DraftStreamer(bot, chat_id=1, thread_id=None, interval=0.01)
    d.on_delta("быстрый ответ")
    await _drain(0.02)  # send в полёте, message_id ещё не записан
    await d.finish()
    assert bot.sent, "send должен был долететь"
    assert bot.deleted, "черновик-сирота: finish обязан дождаться send и удалить"


async def test_finish_survives_dead_task_with_exception():
    bot = FakeBot()
    d = DraftStreamer(bot, chat_id=1, thread_id=None, interval=0.01)

    async def boom():
        raise ValueError("умер внутри цикла")

    d._task = asyncio.create_task(boom())
    await _drain(0.02)  # задача уже мертва с исключением
    await d.finish()  # не должен бросить — иначе finally в боте съест финальный ответ


class FlakyBot(FakeBot):
    """Первый send падает — следующая дельта должна восстановить черновик (dirty-ретрай)."""

    def __init__(self):
        super().__init__()
        self.failures = 1

    async def send_message(self, chat_id, text, message_thread_id=None):
        if self.failures:
            self.failures -= 1
            raise RuntimeError("сеть моргнула")
        return await super().send_message(chat_id, text, message_thread_id)


async def test_failed_send_retries_on_next_tick():
    bot = FlakyBot()
    d = DraftStreamer(bot, chat_id=1, thread_id=None, interval=0.03)
    d.on_delta("не потеряй меня")
    await _drain(0.12)  # первый flush упал, dirty восстановлен → второй тик дошлёт
    await d.finish()
    assert bot.sent and "не потеряй меня" in bot.sent[-1]
