"""A3/A4: темп черновика — 429, повторы и адаптивный интервал.

`interval = 3.0` — это ровно 20 правок в минуту при потолке Telegram ≈20/мин на чат, то
есть впритык и без запаса, а ветки `TelegramRetryAfter` не было вовсе: на 429 черновик
ждал те же 3 секунды и стучался снова, хотя 429 блокирует бота ЦЕЛИКОМ — черновик одного
человека затыкал бота остальным.

Второе: при сбое сети `_dirty` вставал обратно, а `_shown` не обновлялся — цикл каждые
3 секунды слал идентичный текст, получая 400 до конца генерации.
"""

from __future__ import annotations

import asyncio

from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from engine.adapters.telegram.draft import DraftStreamer, DraftTuning


class FlakyBot:
    """Дублёр Bot, у которого edit отвечает заданным исключением."""

    def __init__(self, error: Exception | None = None):
        self.error = error
        self.sent: list[str] = []
        self.edits: list[str] = []
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
        if self.error is not None:
            raise self.error

    async def delete_message(self, chat_id, message_id):
        pass


def _flood(seconds: int) -> TelegramRetryAfter:
    # method — объект вызова aiogram; в дублёре его нет, а для нас важно только retry_after
    return TelegramRetryAfter(
        method=None,  # type: ignore[arg-type]
        message="Flood control exceeded",
        retry_after=seconds,
    )


def _fast(**kw) -> DraftTuning:
    """Темп «быстро» — тесту не ждать секундами то, что проверяется логикой."""
    return DraftTuning(interval=0.03, min_delta_units=0, **kw)


# ── A4: лестница интервалов (чистая функция — без ожиданий в тесте) ─────────


def test_interval_starts_fast_so_the_first_letters_appear_quickly():
    t = DraftTuning()
    assert t.interval_for(0.0) == t.interval
    assert t.interval_for(3.0) == t.interval


def test_interval_grows_for_a_comfortable_read():
    t = DraftTuning()
    assert t.interval_for(20.0) > t.interval_for(1.0)


def test_long_generation_gets_the_calmest_interval():
    """Ответ на три минуты не должен жечь квоту чата ради дёрганья текста."""
    t = DraftTuning()
    assert t.interval_for(120.0) > t.interval_for(20.0)


def test_default_ladder_stays_under_telegram_edit_ceiling():
    """Потолок ≈20 правок/мин на чат: даже быстрая ступень обязана оставлять запас."""
    t = DraftTuning()
    assert 60 / t.interval_for(0.0) <= 60, "быстрее одной правки в секунду — заявка на 429"
    assert 60 / t.interval_for(120.0) <= 20


# ── A4: порог дельты ────────────────────────────────────────────────────────


async def test_tiny_delta_does_not_cost_an_api_call():
    """Мелкая правка читается как дёрганье и стоит столько же, сколько крупная."""
    bot = FlakyBot()
    d = DraftStreamer(
        bot, chat_id=1, thread_id=None, tuning=DraftTuning(interval=0.03, min_delta_units=60)
    )
    d.on_delta("первый показ")
    await asyncio.sleep(0.05)
    d.on_delta("+++")  # три символа — ради этого сеть не дёргаем
    await asyncio.sleep(0.08)
    assert bot.edits == [], f"мелкая дельта не должна давать edit: {bot.edits}"


async def test_delta_above_the_threshold_is_shown():
    bot = FlakyBot()
    d = DraftStreamer(
        bot, chat_id=1, thread_id=None, tuning=DraftTuning(interval=0.03, min_delta_units=60)
    )
    d.on_delta("первый показ")
    await asyncio.sleep(0.05)
    d.on_delta("я" * 100)
    await asyncio.sleep(0.08)
    assert bot.edits, "накопилось больше порога — обязаны показать"


async def test_first_show_ignores_the_threshold():
    """Первые буквы человек должен увидеть сразу, даже если их всего пять."""
    bot = FlakyBot()
    d = DraftStreamer(
        bot, chat_id=1, thread_id=None, tuning=DraftTuning(interval=0.03, min_delta_units=60)
    )
    d.on_delta("Готов")
    await asyncio.sleep(0.05)
    assert bot.sent, "первый показ порогом не режется"


# ── A3: 429 ─────────────────────────────────────────────────────────────────


async def test_retry_after_raises_the_interval_floor():
    """Telegram просит ждать N секунд — ждём N, а не свои 3."""
    bot = FlakyBot(_flood(7))
    d = DraftStreamer(bot, chat_id=1, thread_id=None, tuning=_fast())
    d.on_delta("первый показ")
    await asyncio.sleep(0.05)
    d.on_delta(" ещё текст")
    await asyncio.sleep(0.08)
    assert d.interval >= 7.0, f"интервал не поднялся после 429: {d.interval}"


async def test_flood_does_not_keep_hammering_telegram():
    """429 блокирует бота целиком: черновик одного человека не смеет затыкать остальных."""
    bot = FlakyBot(_flood(5))
    d = DraftStreamer(bot, chat_id=1, thread_id=None, tuning=_fast())
    d.on_delta("первый показ")
    await asyncio.sleep(0.05)
    for i in range(10):
        d.on_delta(f" кусок{i}")
    await asyncio.sleep(0.25)  # при старом поведении сюда влезло бы ~8 попыток
    assert len(bot.edits) <= 2, f"после 429 стучимся дальше: {len(bot.edits)} попыток"


# ── A3: цикл повторной отправки идентичного текста ──────────────────────────


async def test_identical_text_is_not_resent_after_a_client_error():
    """400 на конкретном тексте повторным edit того же текста не лечится — только жжёт квоту."""
    bot = FlakyBot(TelegramBadRequest(method=None, message="message text is too long"))
    d = DraftStreamer(bot, chat_id=1, thread_id=None, tuning=_fast())
    d.on_delta("первый показ")
    await asyncio.sleep(0.05)
    d.on_delta(" добавка")
    await asyncio.sleep(0.2)
    assert len(bot.edits) == 1, f"тот же текст ушёл повторно {len(bot.edits)} раз"


async def test_network_blip_still_retries_on_the_next_tick():
    """Сеть моргнула — это другое: кусок терять нельзя, следующий тик дошлёт."""
    bot = FlakyBot(RuntimeError("сеть моргнула"))
    d = DraftStreamer(bot, chat_id=1, thread_id=None, tuning=_fast())
    d.on_delta("первый показ")
    await asyncio.sleep(0.05)
    d.on_delta(" добавка")
    await asyncio.sleep(0.15)
    assert len(bot.edits) >= 2, "сетевой сбой обязан ретрайиться"


async def test_not_modified_during_streaming_is_treated_as_shown():
    """Телеграм уже показывает этот текст — считаем показанным и не долбимся снова."""
    bot = FlakyBot(TelegramBadRequest(method=None, message="message is not modified"))
    d = DraftStreamer(bot, chat_id=1, thread_id=None, tuning=_fast())
    d.on_delta("первый показ")
    await asyncio.sleep(0.05)
    d.on_delta(" добавка")
    await asyncio.sleep(0.2)
    assert len(bot.edits) == 1, f"повторы поверх «not modified»: {len(bot.edits)}"


async def test_shorter_text_after_reset_is_shown_immediately():
    """Порог считает ПРИРОСТ. После TextStart текст может стать короче — это не «мелкая
    правка», а другой текст: держать вместо него старый абзац нельзя."""
    bot = FlakyBot()
    d = DraftStreamer(
        bot, chat_id=1, thread_id=None, tuning=DraftTuning(interval=0.03, min_delta_units=60)
    )
    d.on_delta("длинная промежуточная болтовня агента про то, как он читает файлы")
    await asyncio.sleep(0.05)
    d.on_reset()
    d.on_delta("итог")
    await asyncio.sleep(0.08)
    assert bot.edits and "итог" in bot.edits[-1], f"новый текст не показан: {bot.edits}"
