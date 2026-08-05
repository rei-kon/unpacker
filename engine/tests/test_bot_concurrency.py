"""A8/A9/A10: сигнал готовности, порядок двух сообщений подряд и молчаливые падения.

Три находки, у которых один общий корень — человек не понимает, что происходит:

  • правка сообщения не поднимает чат и не даёт уведомления, а финал теперь именно правка:
    ушедший из чата на минуту не узнает, что бот закончил (реакция 👀 → ✅ — дешёвая
    компенсация в один вызов API);
  • `_busy` проверялся только для нажатий кнопок, а `_on_text` его не спрашивал вовсе: два
    сообщения подряд = два параллельных прогона и перепутанный порядок доставки;
  • исключение мимо `ask` не доходило до человека НИКАК — бот просто замолкал, и для
    ученика это неотличимо от «бот умер».
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from engine.adapters.telegram.bot import TelegramBot
from engine.adapters.telegram.router import SessionRouter
from engine.core.agent import AskResult
from engine.core.brain import ButtonSpec, dump_buttons_yaml
from engine.core.buttons import ButtonRegistry
from engine.core.errors import Outcome
from engine.core.health import HealthMarker
from engine.core.pool import ClientPool
from engine.core.security import AllowList
from engine.core.store import Store

OWNER = 111
BUTTONS = [ButtonSpec(label="Создать КП", prompt="Собери КП")]


class FakeBot:
    id = 4242

    def __init__(self):
        self.messages: list[dict] = []
        self._next_id = 100

    async def send_message(self, chat_id, text, **kw):
        self.messages.append({"chat_id": chat_id, "text": text, **kw})
        self._next_id += 1
        return SimpleNamespace(message_id=self._next_id)

    async def edit_message_text(self, text, chat_id=None, message_id=None, **kw):
        pass

    async def delete_message(self, chat_id, message_id):
        pass

    async def send_chat_action(self, *a, **kw):
        pass

    @property
    def text(self) -> str:
        return "\n".join(m["text"] for m in self.messages)


class FakeCore:
    def __init__(self, *, outcome: str = "ok"):
        self.asks: list[tuple[str, str]] = []
        self.outcome = outcome

    async def ask(self, session_id, prompt, on_event=None):
        self.asks.append((session_id, prompt))
        return AskResult(text="ответ", outcome=Outcome(self.outcome, ""))

    async def interrupt(self, session_id):
        pass

    @property
    def prompts(self) -> list[str]:
        return [p for _, p in self.asks]


class SlowCore(FakeCore):
    """Ядро, которое «думает», пока тест не отпустит."""

    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def ask(self, session_id, prompt, on_event=None):
        self.asks.append((session_id, prompt))
        self.started.set()
        await self.release.wait()
        return AskResult(text="ответ", outcome=Outcome("ok", ""))


class BoomCore(FakeCore):
    async def ask(self, session_id, prompt, on_event=None):
        self.asks.append((session_id, prompt))
        raise RuntimeError("ядро упало")


def _message(text="привет", user_id=OWNER, thread_id=7):
    reacted: list = []
    msg = SimpleNamespace(
        text=text,
        caption=None,
        document=None,
        photo=None,
        message_thread_id=thread_id,
        chat=SimpleNamespace(id=100, type="private"),
        from_user=SimpleNamespace(id=user_id),
        reacted=reacted,
    )

    async def react(items):
        reacted.append([r.emoji for r in items])

    msg.react = react
    return msg


@pytest.fixture
def stand(tmp_path):
    brain = tmp_path / "brains" / "office"
    brain.mkdir(parents=True)
    (brain / "CLAUDE.md").write_text("# мозг\n", encoding="utf-8")
    inst = tmp_path / "agents" / "office"
    state = inst / "state"
    state.mkdir(parents=True)
    (inst / "buttons.yaml").write_text(dump_buttons_yaml(BUTTONS), encoding="utf-8")
    store = Store(str(state / "state.db"))
    store.projects.create(slug="office", name="Офис", brain_path=str(brain), default_model=None)
    yield SimpleNamespace(inst=inst, state=state, store=store, tmp=tmp_path)
    store.close()


def _build(stand, core=None, *, health=None, alert=None):
    bot = FakeBot()
    core = core or FakeCore()
    tg = TelegramBot(
        bot=bot,
        allow=AllowList([OWNER]),
        router=SessionRouter(store=stand.store, core=core, default_project_slug="office"),
        store=stand.store,
        pool=ClientPool(factory=lambda options: None, ceiling=2, idle_timeout=60.0),
        buttons=ButtonRegistry(stand.inst / "buttons.yaml"),
        draft=None,  # черновик здесь не предмет теста
        health=health,
        on_alert=alert,
    )
    return SimpleNamespace(tg=tg, bot=bot, core=core)


# ── A8: 👀 → ✅ ──────────────────────────────────────────────────────────────


async def test_reaction_turns_into_a_checkmark_when_the_answer_is_ready(stand):
    """Правка сообщения не пингует чат — сигнал готовности даёт смена реакции."""
    s = _build(stand)
    msg = _message()
    await s.tg._on_text(msg)
    assert msg.reacted[0] == ["👀"], "в начале — «взял в работу»"
    assert msg.reacted[-1] == ["✅"], f"по готовности реакция обязана смениться: {msg.reacted}"


async def test_failed_answer_does_not_claim_success(stand):
    s = _build(stand, BoomCore())
    msg = _message()
    with pytest.raises(RuntimeError):
        await s.tg._on_text(msg)
    assert ["✅"] not in msg.reacted, "галочка на упавшем прогоне — вранье"


async def test_reaction_failure_still_does_not_block_the_answer(stand):
    s = _build(stand)
    msg = _message()

    async def boom(items):
        raise RuntimeError("реакции запрещены в этом чате")

    msg.react = boom
    await s.tg._on_text(msg)
    assert s.core.prompts == ["привет"]


# ── A9: замок на окно + мягкий сигнал ───────────────────────────────────────


async def test_second_message_waits_for_the_first_to_be_delivered(stand):
    """Два текста подряд = два параллельных прогона: доставка перепутается местами."""
    s = _build(stand, SlowCore())
    first = asyncio.create_task(s.tg._on_text(_message("первый")))
    await s.core.started.wait()
    second = asyncio.create_task(s.tg._on_text(_message("второй")))
    await asyncio.sleep(0.05)
    assert s.core.prompts == ["первый"], "второй промпт не смеет начаться раньше времени"
    s.core.release.set()
    await asyncio.wait_for(asyncio.gather(first, second), timeout=2.0)
    assert s.core.prompts == ["первый", "второй"], "порядок обязан сохраниться"


async def test_second_message_gets_a_soft_signal_not_a_refusal(stand):
    """Отказ текстом — грубо и лишнее сообщение в чате; хватает реакции."""
    s = _build(stand, SlowCore())
    first = asyncio.create_task(s.tg._on_text(_message("первый")))
    await s.core.started.wait()
    waiting = _message("второй")
    second = asyncio.create_task(s.tg._on_text(waiting))
    await asyncio.sleep(0.05)
    assert ["✍️"] in waiting.reacted, f"мягкий сигнал «принял, отвечу следом»: {waiting.reacted}"
    assert "подожд" not in s.bot.text.lower() and "занят" not in s.bot.text.lower()
    s.core.release.set()
    await asyncio.wait_for(asyncio.gather(first, second), timeout=2.0)


async def test_busy_window_does_not_lock_another_topic(stand):
    """Замок — на окно (chat:thread): топики Telegram независимы (§5.2)."""
    s = _build(stand, SlowCore())
    first = asyncio.create_task(s.tg._on_text(_message("первый")))
    await s.core.started.wait()
    other = asyncio.create_task(s.tg._on_text(_message("в другом топике", thread_id=42)))
    await asyncio.sleep(0.05)
    assert len(s.core.prompts) == 2, "второй топик обязан работать параллельно"
    s.core.release.set()
    await asyncio.wait_for(asyncio.gather(first, other), timeout=2.0)


async def test_lock_is_released_after_a_crash(stand):
    """Упавший прогон не должен запирать окно навсегда."""
    s = _build(stand, BoomCore())
    with pytest.raises(RuntimeError):
        await s.tg._on_text(_message("первый"))
    s2 = _build(stand)
    s2.tg._locks = s.tg._locks  # то же хозяйство замков
    await s2.tg._on_text(_message("второй"))
    assert s2.core.prompts == ["второй"]


# ── A10: падение мимо ask больше не молчит ──────────────────────────────────


async def test_error_handler_is_registered(stand):
    """Убери регистрацию — и бот снова молчит на любом исключении, а тесты зелёные."""
    s = _build(stand)
    assert [h.callback.__name__ for h in s.tg._dp.errors.handlers] == ["_on_error"]


def _error_event(exception: Exception, *, thread_id: int | None = 7):
    """Дублёр ErrorEvent: aiogram отдаёт сюда исходный Update и исключение."""
    return SimpleNamespace(
        exception=exception,
        update=SimpleNamespace(
            message=SimpleNamespace(
                chat=SimpleNamespace(id=100, type="private"),
                message_thread_id=thread_id,
            ),
            callback_query=None,
        ),
    )


async def test_error_handler_tells_the_person_what_happened(stand):
    s = _build(stand)
    await s.tg._on_error(_error_event(RuntimeError("ядро упало")))
    assert s.bot.messages, "молчание неотличимо от «бот умер»"
    assert "⚠️" in s.bot.text
    assert s.bot.messages[-1].get("message_thread_id") == 7, "ответ уходит в тот же топик"


async def test_error_handler_marks_the_engine_degraded(stand):
    health = HealthMarker(str(stand.tmp / "health.json"))
    s = _build(stand, health=health)
    await s.tg._on_error(_error_event(RuntimeError("ядро упало")))
    marker = health.read()
    assert marker is not None and marker["status"] == "degraded"


async def test_error_handler_alerts_the_owner(stand):
    alerts: list[str] = []
    s = _build(stand, alert=alerts.append)
    await s.tg._on_error(_error_event(RuntimeError("ядро упало")))
    assert alerts, "владелец обязан узнать о падении"


async def test_error_handler_survives_an_update_without_a_chat(stand):
    """Апдейт без чата (например, my_chat_member) — писать некуда, падать нельзя."""
    s = _build(stand)
    event = SimpleNamespace(
        exception=RuntimeError("бум"),
        update=SimpleNamespace(message=None, callback_query=None),
    )
    await s.tg._on_error(event)
    assert s.bot.messages == []


async def test_error_from_a_callback_answers_into_its_chat(stand):
    s = _build(stand)
    event = SimpleNamespace(
        exception=RuntimeError("бум"),
        update=SimpleNamespace(
            message=None,
            callback_query=SimpleNamespace(
                message=SimpleNamespace(
                    chat=SimpleNamespace(id=100, type="private"),
                    message_thread_id=7,
                )
            ),
        ),
    )
    await s.tg._on_error(event)
    assert s.bot.messages, "нажатие кнопки упало — человек тоже должен узнать"
