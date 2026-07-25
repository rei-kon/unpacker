"""Проводка aiogram: РЕАЛЬНАЯ регистрация хендлеров, а не вызов приватных методов (H2).

Ревью нашло дыру в честности тестов: 40+ тестов адаптера дёргают `_on_text`/`_on_callback`
напрямую. Убери регистрацию callback_query или поставь `F.text` выше `F.document` — все они
останутся зелёными, а у ученика кнопки и файлы молча мертвы.

Поэтому здесь два уровня:

  • сверка самой таблицы регистрации Dispatcher (порядок и наличие фильтров);
  • прогон настоящего `Update` через `dp.feed_update` — то есть тем же путём, каким идёт
    живой polling. Дублёром остаётся только Bot (сеть), сами события — настоящие
    aiogram-модели.

Плюс C18/K11: голос/видео/аудио раньше не имели хендлера вообще — бот молчал, хотя `/help`
обещает приём файлов, а молчание неотличимо от «бот умер».
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from aiogram.types import Chat, Message, Update, User, Voice

from engine.adapters.telegram.bot import TelegramBot
from engine.adapters.telegram.keyboard import encode_trigger
from engine.adapters.telegram.router import SessionRouter
from engine.core.buttons import ButtonRegistry
from engine.core.pool import ClientPool
from engine.core.security import AllowList
from engine.core.sendfile import SendFilePolicy
from engine.core.store import Store
from engine.tests.test_bot_attachments import (
    BUTTONS,
    BUTTONS_YAML,
    OWNER,
    FakeBot,
    FakeCore,
)

CHAT_ID = 100
THREAD_ID = 7


@pytest.fixture
def wired(tmp_path):
    """Собранный TelegramBot на дублёре Bot — но с настоящим Dispatcher."""
    brain = tmp_path / "brains" / "office"
    brain.mkdir(parents=True)
    (brain / "CLAUDE.md").write_text("# мозг\n", encoding="utf-8")

    inst = tmp_path / "agents" / "office"
    state = inst / "state"
    (state / "uploads").mkdir(parents=True)
    (inst / "buttons.yaml").write_text(BUTTONS_YAML, encoding="utf-8")

    store = Store(str(state / "state.db"))
    store.projects.create(slug="office", name="Офис", brain_path=str(brain), default_model=None)

    bot = FakeBot()
    core = FakeCore()
    tg = TelegramBot(
        bot=bot,
        allow=AllowList([OWNER]),
        router=SessionRouter(store=store, core=core, default_project_slug="office"),
        store=store,
        pool=ClientPool(factory=lambda options: None, ceiling=2, idle_timeout=60.0),
        buttons=ButtonRegistry(inst / "buttons.yaml"),
        send_file=SendFilePolicy(state_root=state),
    )
    yield tg, bot, core
    store.close()


def _msg(**fields) -> Message:
    """Настоящий aiogram Message — не SimpleNamespace."""
    # dict[str, Any]: Message принимает 200+ опциональных полей, и вывод типов по литералу
    # заставил бы mypy сверять **kwargs с каждым из них
    base: dict[str, Any] = {
        "message_id": 1,
        "date": datetime.now(tz=UTC),
        "chat": Chat(id=CHAT_ID, type="private"),
        "from_user": User(id=OWNER, is_bot=False, first_name="Никита"),
        "message_thread_id": THREAD_ID,
    }
    base.update(fields)
    return Message(**base)


async def _feed(tg: TelegramBot, **fields) -> None:
    await tg._dp.feed_update(tg.bot, Update(update_id=1, message=_msg(**fields)))


# ── таблица регистрации: то, что легко сломать молча ─────────────────────────


def _handler_names(observer) -> list[str]:
    return [h.callback.__name__ for h in observer.handlers]


def test_attachment_handlers_registered_before_text(wired):
    """Порядок важен: F.text выше F.document — и файлы молча уходят в текстовый хендлер."""
    names = _handler_names(wired[0]._dp.message)
    assert "_on_attachment" in names and "_on_text" in names
    assert names.index("_on_attachment") < names.index("_on_text")


def test_both_document_and_photo_are_wired(wired):
    names = _handler_names(wired[0]._dp.message)
    assert names.count("_on_attachment") == 2, "нужны оба: F.document и F.photo"


def test_callback_query_handler_registered(wired):
    """Убери эту строку — кнопки перестают работать, а все прямые тесты зелёные."""
    assert _handler_names(wired[0]._dp.callback_query) == ["_on_callback"]


def test_unsupported_handler_is_the_last_message_handler(wired):
    """Финальный хендлер обязан быть ПОСЛЕДНИМ: иначе он перехватит текст и файлы."""
    names = _handler_names(wired[0]._dp.message)
    assert names[-1] == "_on_unsupported"


def test_every_documented_command_has_a_handler(wired):
    """/help обещает список команд — проверяем, что каждая зарегистрирована."""
    names = set(_handler_names(wired[0]._dp.message))
    for cmd in ("start", "help", "projects", "switch", "status", "stop", "close", "clear"):
        assert f"_on_{cmd}" in names, f"команда /{cmd} не зарегистрирована"
    assert "_on_model" in names and "_on_verbose" in names


# ── прогон настоящего Update через Dispatcher ────────────────────────────────


async def test_text_update_reaches_the_agent(wired):
    tg, _bot, core = wired
    await _feed(tg, text="привет")
    assert core.prompts == ["привет"]


async def test_command_update_does_not_reach_the_agent(wired):
    """/help — команда движка, а не промпт: она не должна жечь квоту подписки."""
    tg, bot, core = wired
    await _feed(tg, text="/help")
    assert core.asks == []
    assert "файл" in bot.text.lower()


async def test_callback_update_reaches_the_agent(wired):
    """Кнопка проходит весь путь: Update → Dispatcher → callback-хендлер → сессия."""
    tg, _bot, core = wired
    from aiogram.types import CallbackQuery

    cb = CallbackQuery(
        id="cb1",
        from_user=User(id=OWNER, is_bot=False, first_name="Никита"),
        chat_instance="ci1",
        message=_msg(text="прошлый ответ"),
        data=encode_trigger(0, BUTTONS[0]),
    )
    await tg._dp.feed_update(tg.bot, Update(update_id=2, callback_query=cb))
    assert core.prompts == [BUTTONS[0].prompt]


async def test_stranger_text_is_dropped_by_the_real_pipeline(wired):
    """Гейт §8.2 держится и на реальном пути, а не только в прямом вызове."""
    tg, bot, core = wired
    await tg._dp.feed_update(
        tg.bot,
        Update(
            update_id=3,
            message=_msg(
                text="пусти меня", from_user=User(id=999, is_bot=False, first_name="Чужой")
            ),
        ),
    )
    assert core.asks == [] and bot.messages == []


# ── C18/K11: голос/видео/аудио больше не проваливаются в тишину ──────────────


async def test_voice_message_gets_an_honest_answer(wired):
    """Раньше хендлера не было вовсе: бот молчал, а молчание = «бот умер» для ученика."""
    tg, bot, core = wired
    voice = Voice(file_id="V1", file_unique_id="vu1", duration=3)
    await _feed(tg, voice=voice)
    assert core.asks == [], "голос не уходит агенту (расшифровки в Фазе 1b нет)"
    assert bot.text.strip(), "молчать нельзя — молчание неотличимо от смерти бота"


async def test_unsupported_answer_names_what_is_accepted(wired):
    """Отказ обязан говорить, что делать вместо этого (§5.5 — честные сообщения)."""
    tg, bot, _core = wired
    await _feed(tg, voice=Voice(file_id="V1", file_unique_id="vu1", duration=3))
    low = bot.text.lower()
    assert "документ" in low or "фото" in low or "текст" in low


async def test_sticker_does_not_reach_the_agent(wired):
    """Стикер тоже ловится финальным хендлером, а не молчанием."""
    from aiogram.types import Sticker

    tg, bot, core = wired
    sticker = Sticker(
        file_id="S1",
        file_unique_id="su1",
        type="regular",
        width=1,
        height=1,
        is_animated=False,
        is_video=False,
    )
    await _feed(tg, sticker=sticker)
    assert core.asks == []
    assert bot.text.strip()


async def test_unsupported_from_stranger_stays_silent(wired):
    """Гейт первым и здесь: чужому не отвечаем даже «не принимаю» (fail-closed §8.2)."""
    tg, bot, _core = wired
    await _feed(
        tg,
        voice=Voice(file_id="V1", file_unique_id="vu1", duration=3),
        from_user=User(id=999, is_bot=False, first_name="Чужой"),
    )
    assert bot.messages == []
