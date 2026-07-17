"""SessionRouter — чистая логика роутинга топик→проект→сессия и команд слоя 1 (§5.2, §5.5).

Отделена от aiogram: здесь проверяется вся логика (surface_key, создание/переключение сессий,
команды) с фейковым core, без живого Telegram. aiogram-склейка (bot.py) тонкая — на живом боте.
"""

import pytest

from engine.adapters.telegram.router import NoProjectError, SessionRouter
from engine.core.agent import AskResult
from engine.core.errors import Outcome
from engine.core.store import Store


class FakeCore:
    """Дублёр AgentCore: помнит вызовы ask/interrupt, отвечает фиксированным AskResult."""

    def __init__(self):
        self.asks = []
        self.interrupts = []

    async def ask(self, session_id, prompt, on_event=None):
        self.asks.append((session_id, prompt))
        return AskResult(text="ответ", outcome=Outcome("ok", ""))

    async def interrupt(self, session_id):
        self.interrupts.append(session_id)


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "state.db"))
    s.projects.create(slug="office", name="Офис", brain_path="/b/office", default_model="sonnet")
    s.projects.create(slug="sales", name="Продажник", brain_path="/b/sales", default_model="opus")
    yield s
    s.close()


@pytest.fixture
def router(store):
    return SessionRouter(store=store, core=FakeCore(), default_project_slug="office")


# ── surface_key + роутинг сообщений ──────────────────────────────────────────


def test_surface_key_with_and_without_topic(router):
    assert router.surface_key(100, 7) == "100:7"
    assert router.surface_key(100, None) == "100:0"  # личка без топика


async def test_first_message_creates_session_and_binding(router, store):
    result = await router.on_message(chat_id=100, thread_id=7, user_id=111, text="привет")
    assert result.outcome.kind == "ok"
    sid = store.bindings.resolve("tg", "100:7")
    assert sid is not None
    assert store.sessions.get(sid).project_slug == "office"  # дефолтный проект
    assert store.sessions.get(sid).owner_user_id == 111


async def test_second_message_reuses_session(router, store):
    await router.on_message(chat_id=100, thread_id=7, user_id=111, text="раз")
    sid1 = store.bindings.resolve("tg", "100:7")
    await router.on_message(chat_id=100, thread_id=7, user_id=111, text="два")
    sid2 = store.bindings.resolve("tg", "100:7")
    assert sid1 == sid2  # то же окно → та же сессия
    assert router._core.asks == [(sid1, "раз"), (sid1, "два")]


async def test_different_topics_different_sessions(router, store):
    await router.on_message(chat_id=100, thread_id=1, user_id=111, text="a")
    await router.on_message(chat_id=100, thread_id=2, user_id=111, text="b")
    assert store.bindings.resolve("tg", "100:1") != store.bindings.resolve("tg", "100:2")


async def test_no_project_raises(tmp_path):
    empty = Store(str(tmp_path / "empty.db"))
    r = SessionRouter(store=empty, core=FakeCore(), default_project_slug=None)
    with pytest.raises(NoProjectError):
        await r.on_message(chat_id=1, thread_id=0, user_id=111, text="x")
    empty.close()


# ── команды слоя 1 ───────────────────────────────────────────────────────────


def test_list_projects_text(router):
    text = router.list_projects_text()
    assert "office" in text and "sales" in text


async def test_switch_project_rebinds_to_new_session(router, store):
    await router.on_message(chat_id=100, thread_id=7, user_id=111, text="привет")
    old_sid = store.bindings.resolve("tg", "100:7")
    msg = await router.switch_project(chat_id=100, thread_id=7, user_id=111, slug="sales")
    new_sid = store.bindings.resolve("tg", "100:7")
    assert new_sid != old_sid
    assert store.sessions.get(new_sid).project_slug == "sales"
    assert "sales" in msg or "Продажник" in msg


async def test_switch_unknown_project(router):
    msg = await router.switch_project(chat_id=100, thread_id=7, user_id=111, slug="ghost")
    assert "ghost" in msg.lower() or "нет" in msg.lower()


async def test_status_shows_project_and_model(router, store):
    await router.on_message(chat_id=100, thread_id=7, user_id=111, text="привет")
    text = router.status_text(chat_id=100, thread_id=7)
    assert "office" in text
    assert "sonnet" in text


def test_status_no_session(router):
    text = router.status_text(chat_id=100, thread_id=7)
    assert "нет" in text.lower() or "сесси" in text.lower()


async def test_stop_calls_interrupt(router, store):
    await router.on_message(chat_id=100, thread_id=7, user_id=111, text="привет")
    sid = store.bindings.resolve("tg", "100:7")
    await router.stop(chat_id=100, thread_id=7)
    assert sid in router._core.interrupts


async def test_close_closes_session(router, store):
    await router.on_message(chat_id=100, thread_id=7, user_id=111, text="привет")
    router.close(chat_id=100, thread_id=7)
    assert store.bindings.resolve("tg", "100:7") is None  # окно отвязано


async def test_clear_starts_new_session(router, store):
    await router.on_message(chat_id=100, thread_id=7, user_id=111, text="привет")
    old = store.bindings.resolve("tg", "100:7")
    await router.clear(chat_id=100, thread_id=7, user_id=111)
    new = store.bindings.resolve("tg", "100:7")
    assert new is not None and new != old
    assert store.sessions.get(new).project_slug == "office"  # тот же проект, новая сессия


async def test_set_model(router, store):
    await router.on_message(chat_id=100, thread_id=7, user_id=111, text="привет")
    sid = store.bindings.resolve("tg", "100:7")
    router.set_model(chat_id=100, thread_id=7, model="haiku")
    assert store.sessions.get(sid).model == "haiku"


async def test_set_model_rejects_unknown(router, store):
    await router.on_message(chat_id=100, thread_id=7, user_id=111, text="привет")
    sid = store.bindings.resolve("tg", "100:7")
    msg = router.set_model(chat_id=100, thread_id=7, model="gpt-9000")
    assert "gpt-9000" in msg  # подсказка, а не «переключено»
    assert store.sessions.get(sid).model == "sonnet"  # не записана мусорная модель


async def test_set_verbose(router, store):
    await router.on_message(chat_id=100, thread_id=7, user_id=111, text="привет")
    sid = store.bindings.resolve("tg", "100:7")
    router.set_verbose(chat_id=100, thread_id=7, level=1)
    assert store.sessions.get(sid).verbose == 1


# ── деградированные условия (находки ревью D) ────────────────────────────────


async def test_clear_falls_back_when_project_disabled(router, store):
    """Проект сессии отключили → /clear не падает, а заводит сессию дефолтного активного."""
    await router.on_message(chat_id=100, thread_id=7, user_id=111, text="привет")  # office
    await router.switch_project(chat_id=100, thread_id=7, user_id=111, slug="sales")
    store.projects.disable("sales")  # проект текущей сессии отключён
    await router.clear(chat_id=100, thread_id=7, user_id=111)  # НЕ должно бросить
    new_sid = store.bindings.resolve("tg", "100:7")
    assert store.sessions.get(new_sid).project_slug == "office"  # фолбэк на дефолтный


async def test_clear_raises_when_no_active_project(tmp_path):
    empty = Store(str(tmp_path / "empty.db"))
    r = SessionRouter(store=empty, core=FakeCore(), default_project_slug=None)
    with pytest.raises(NoProjectError):  # хендлер это ловит и отвечает текстом
        await r.clear(chat_id=1, thread_id=0, user_id=111)
    empty.close()


async def test_switch_orphans_old_session_to_stopped(router, store):
    """Переключение проекта останавливает осиротевшую старую сессию (не тёплой в пуле)."""
    await router.on_message(chat_id=100, thread_id=7, user_id=111, text="привет")
    old_sid = store.bindings.resolve("tg", "100:7")
    await router.switch_project(chat_id=100, thread_id=7, user_id=111, slug="sales")
    assert store.sessions.get(old_sid).status == "stopped"


async def test_binding_survives_store_reopen(tmp_path):
    """Критерий 1a «переживает рестарт»: тот же surface_key после reopen резолвит ту же сессию."""
    path = str(tmp_path / "state.db")
    s1 = Store(path)
    s1.projects.create(slug="office", name="Офис", brain_path="/b", default_model="sonnet")
    r1 = SessionRouter(store=s1, core=FakeCore(), default_project_slug="office")
    await r1.on_message(chat_id=100, thread_id=7, user_id=111, text="привет")
    sid_before = s1.bindings.resolve("tg", "100:7")
    s1.close()

    s2 = Store(path)  # рестарт процесса
    assert s2.bindings.resolve("tg", "100:7") == sid_before  # то же окно → та же сессия
    s2.close()
