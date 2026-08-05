"""Seam-тест стриминга: `_run_prompt` → `on_event` → черновик → финал.

Раньше на эту связку не было ни одного теста: `DraftStreamer` проверялся изолированно с
дублёром бота, а склейка «дельты приходят из ядра → черновик → доставка ответа» держалась
на глазах. Именно в ней жила самая дорогая находка ревью — черновик удалялся в `finally`,
а финал мог не прийти вовсе.

Предмет проверки — ПОРЯДОК вызовов Bot API, поэтому дублёр ведёт один общий журнал, а не
три раздельных списка: «не было delete», «клавиатура на последнем», «хвост после финала» —
всё это утверждения о последовательности.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramRetryAfter

from engine.adapters.telegram import bot as bot_module
from engine.adapters.telegram.bot import TelegramBot
from engine.adapters.telegram.router import SessionRouter
from engine.core.agent import AskResult
from engine.core.brain import ButtonSpec, dump_buttons_yaml
from engine.core.buttons import ButtonRegistry
from engine.core.errors import Outcome
from engine.core.events import TextDelta, TextStart
from engine.core.pool import ClientPool
from engine.core.security import AllowList
from engine.core.sendfile import SendFilePolicy
from engine.core.store import Store

OWNER = 111
BUTTONS = [ButtonSpec(label="Создать КП", prompt="Собери КП")]


class JournalBot:
    """Дублёр Bot с ОДНИМ журналом вызовов: предмет теста — их порядок."""

    id = 4242

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.markups: list[tuple[str, object]] = []
        self.kwargs: list[dict] = []
        self._next_id = 100

    async def send_message(self, chat_id, text, **kw):
        self.calls.append(("send_message", text))
        self.markups.append(("send_message", kw.get("reply_markup")))
        self.kwargs.append(kw)
        self._next_id += 1
        return SimpleNamespace(message_id=self._next_id)

    async def edit_message_text(self, text, chat_id=None, message_id=None, **kw):
        self.calls.append(("edit_message_text", text))
        self.markups.append(("edit_message_text", kw.get("reply_markup")))

    async def delete_message(self, chat_id, message_id):
        self.calls.append(("delete_message", ""))

    async def send_document(self, chat_id, document, **kw):
        self.calls.append(("send_document", str(document)))
        self.markups.append(("send_document", kw.get("reply_markup")))
        self._next_id += 1
        return SimpleNamespace(message_id=self._next_id)

    async def send_chat_action(self, *a, **kw):
        self.calls.append(("send_chat_action", ""))

    @property
    def methods(self) -> list[str]:
        """Журнал без косметики: typing к порядку доставки отношения не имеет."""
        return [m for m, _ in self.calls if m != "send_chat_action"]

    @property
    def edits(self) -> list[str]:
        return [t for m, t in self.calls if m == "edit_message_text"]

    @property
    def sent(self) -> list[str]:
        return [t for m, t in self.calls if m == "send_message"]

    @property
    def text(self) -> str:
        return "\n".join(t for m, t in self.calls if m in ("send_message", "edit_message_text"))

    @property
    def last_markup(self) -> object:
        """Клавиатура последнего доставленного элемента (по журналу, а не по надежде)."""
        return self.markups[-1][1] if self.markups else None


class StreamCore:
    """Ядро, которое реально стримит: TextStart → дельты → готовый ответ."""

    def __init__(self, reply: str = "готовый ответ", *, outcome: str = "ok", deltas=None):
        self.reply = reply
        self.outcome = outcome
        self.deltas = ["начало ", "ответа"] if deltas is None else deltas
        self.asks: list[tuple[str, str]] = []

    async def ask(self, session_id, prompt, on_event=None):
        self.asks.append((session_id, prompt))
        if on_event is not None and self.deltas:
            on_event(TextStart())
            for piece in self.deltas:
                on_event(TextDelta(text=piece))
        await asyncio.sleep(0.05)  # черновик успевает родиться и отредактироваться
        return AskResult(text=self.reply, outcome=Outcome(self.outcome, ""))

    async def interrupt(self, session_id):
        pass


def _message(text="привет", user_id=OWNER):
    reacted: list = []
    msg = SimpleNamespace(
        text=text,
        caption=None,
        document=None,
        photo=None,
        message_thread_id=7,
        chat=SimpleNamespace(id=100, type="private"),
        from_user=SimpleNamespace(id=user_id),
        reacted=reacted,
    )

    async def react(items):
        reacted.append(items)

    msg.react = react
    return msg


@pytest.fixture
def stand(tmp_path):
    brain = tmp_path / "brains" / "office"
    brain.mkdir(parents=True)
    (brain / "CLAUDE.md").write_text("# мозг\n", encoding="utf-8")
    (brain / "kp.pdf").write_text("КП", encoding="utf-8")
    inst = tmp_path / "agents" / "office"
    state = inst / "state"
    state.mkdir(parents=True)
    (inst / "buttons.yaml").write_text(dump_buttons_yaml(BUTTONS), encoding="utf-8")
    store = Store(str(state / "state.db"))
    store.projects.create(slug="office", name="Офис", brain_path=str(brain), default_model=None)
    yield SimpleNamespace(brain=brain, inst=inst, state=state, store=store)
    store.close()


def _build(stand, core, *, bot=None):
    bot = bot or JournalBot()
    tg = TelegramBot(
        bot=bot,
        allow=AllowList([OWNER]),
        router=SessionRouter(store=stand.store, core=core, default_project_slug="office"),
        store=stand.store,
        pool=ClientPool(factory=lambda options: None, ceiling=2, idle_timeout=60.0),
        buttons=ButtonRegistry(stand.inst / "buttons.yaml"),
        send_file=SendFilePolicy(state_root=stand.state),
    )
    return SimpleNamespace(tg=tg, bot=bot, core=core)


# ── счастливый путь: одно сообщение от первой буквы до финала ────────────────


async def test_happy_path_never_deletes_the_draft(stand):
    s = _build(stand, StreamCore())
    await s.tg._on_text(_message())
    assert "delete_message" not in s.bot.methods, f"журнал: {s.bot.methods}"


async def test_happy_path_sends_exactly_one_message(stand):
    """Ровно одно сообщение на ответ: то самое, что печаталось. Финал — его edit."""
    s = _build(stand, StreamCore())
    await s.tg._on_text(_message())
    assert s.bot.methods.count("send_message") == 1, f"журнал: {s.bot.methods}"


async def test_final_text_replaces_the_draft_in_the_last_edit(stand):
    s = _build(stand, StreamCore(reply="готовый ответ"))
    await s.tg._on_text(_message())
    assert "готовый ответ" in s.bot.edits[-1].replace("\\", "")


async def test_final_edit_has_no_cursor(stand):
    s = _build(stand, StreamCore())
    await s.tg._on_text(_message())
    assert "▌" not in s.bot.edits[-1]


async def test_keyboard_lands_on_the_final_edit(stand):
    """Единственный элемент доставки — сам финальный edit: клавиатура на нём."""
    s = _build(stand, StreamCore())
    await s.tg._on_text(_message())
    kind, markup = s.bot.markups[-1]
    assert kind == "edit_message_text" and markup is not None


async def test_without_deltas_answer_comes_as_a_usual_message(stand):
    """Модель ответила одним куском, черновика не было — обычный путь доставки."""
    s = _build(stand, StreamCore(reply="краткий ответ", deltas=[]))
    await s.tg._on_text(_message())
    assert s.bot.edits == []
    assert "краткий ответ" in s.bot.sent[-1].replace("\\", "")


# ── длинный финал: edit первого куска + хвост новыми сообщениями ─────────────


async def test_long_final_edits_first_chunk_and_sends_the_tail(stand):
    s = _build(stand, StreamCore(reply="Абзац текста.\n\n" * 700))
    await s.tg._on_text(_message())
    methods = s.bot.methods
    assert methods[0] == "send_message", "черновик рождается первым"
    assert "delete_message" not in methods
    assert methods.count("edit_message_text") >= 1
    assert methods.count("send_message") >= 2, f"хвост должен уйти новыми сообщениями: {methods}"
    assert methods[-1] == "send_message", "хвост идёт ПОСЛЕ финального edit"


async def test_long_final_puts_keyboard_only_on_the_last_piece(stand):
    s = _build(stand, StreamCore(reply="Абзац текста.\n\n" * 700))
    await s.tg._on_text(_message())
    assert s.bot.last_markup is not None, "клавиатура на последнем элементе доставки"
    earlier = [m for _, m in s.bot.markups[:-1]]
    assert all(m is None for m in earlier), "на промежуточных кусках клавиатуры быть не должно"


async def test_long_final_delivers_the_whole_text(stand):
    body = "Абзац текста.\n\n" * 700
    s = _build(stand, StreamCore(reply=body))
    await s.tg._on_text(_message())
    delivered = s.bot.text.replace("\\", "").replace("\n", "").replace(" ", "")
    assert delivered.count("Абзацтекста.") == 700, "часть ответа потерялась при нарезке"


# ── файлы: маркер вырезан ДО финального edit ────────────────────────────────


async def test_send_file_marker_never_reaches_the_edited_draft(stand):
    s = _build(stand, StreamCore(reply="Готово [SEND_FILE:kp.pdf] — забирай"))
    await s.tg._on_text(_message())
    assert "SEND_FILE" not in s.bot.text.replace("\\", "")
    assert "send_document" in s.bot.methods


async def test_file_goes_after_the_final_edit_and_carries_the_keyboard(stand):
    s = _build(stand, StreamCore(reply="Готово [SEND_FILE:kp.pdf]"))
    await s.tg._on_text(_message())
    assert s.bot.methods[-1] == "send_document"
    kind, markup = s.bot.markups[-1]
    assert kind == "send_document" and markup is not None


# ── FA2: черновик не остаётся сиротой ни на одном пути ──────────────────────


async def test_answer_of_only_file_markers_stops_the_draft(stand):
    """Ответ — одни маркеры файлов: доставлять черновиком нечего, но и бросать его нельзя.

    Раньше черновик финализировался ТОЛЬКО когда первым в очереди шёл текст. Ответ,
    от которого после вырезания маркеров остаются одни пробелы, оставлял в чате вечный
    курсор ▌, а фоновый цикл черновика продолжал крутиться после ответа.
    """
    s = _build(stand, StreamCore(reply="[SEND_FILE:kp.pdf] [SEND_FILE:kp.pdf]"))
    await s.tg._on_text(_message())
    methods = s.bot.methods
    assert "delete_message" in methods, f"черновик брошен с курсором: {methods}"
    assert methods.index("delete_message") < methods.index("send_document"), (
        f"курсор ▌ висит, пока уходят файлы: {methods}"
    )


async def test_cancelled_run_stops_the_draft(stand):
    """Снятие задачи (рестарт бота): фоновый цикл черновика некому остановить, кроме нас."""

    class HangingCore(StreamCore):
        async def ask(self, session_id, prompt, on_event=None):
            if on_event is not None:
                on_event(TextStart())
                on_event(TextDelta(text="начало ответа"))
            await asyncio.sleep(30)  # снимут отменой, не дождавшись
            return AskResult(text="", outcome=Outcome("ok", ""))

    s = _build(stand, HangingCore())
    task = asyncio.create_task(s.tg._on_text(_message()))
    await asyncio.sleep(0.1)  # черновик успел появиться
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    last = s.bot.edits[-1].replace("\\", "")
    assert "начало ответа" in last, "прочитанное не стираем"
    assert "▌" not in last, "курсор остался — цикл черновика не остановлен"


# ── A2: не-ok исход — показанный текст остаётся ─────────────────────────────


async def test_failed_outcome_keeps_the_shown_text_and_explains(stand):
    """Человек читал печатающийся ответ — он обязан остаться, а не смениться сухим «сбой»."""
    s = _build(stand, StreamCore(reply="", outcome="exec_error"))
    await s.tg._on_text(_message())
    assert "delete_message" not in s.bot.methods
    last = s.bot.edits[-1].replace("\\", "")
    assert "начало ответа" in last, f"показанный текст пропал: {last!r}"
    assert "Сбой" in last or "сбой" in last


async def test_failed_outcome_without_draft_still_explains(stand):
    s = _build(stand, StreamCore(reply="", outcome="exec_error", deltas=[]))
    await s.tg._on_text(_message())
    assert "сбой" in s.bot.text.lower()


async def test_engine_crash_keeps_the_shown_text(stand):
    """Исключение SDK: накопленный текст выброшен ядром, но показанный — остаётся в чате."""

    class BoomCore(StreamCore):
        async def ask(self, session_id, prompt, on_event=None):
            if on_event is not None:
                on_event(TextStart())
                on_event(TextDelta(text="начало ответа"))
            await asyncio.sleep(0.05)
            raise RuntimeError("ядро упало")

    s = _build(stand, BoomCore())
    with pytest.raises(RuntimeError):
        await s.tg._on_text(_message())
    assert "delete_message" not in s.bot.methods
    assert "начало ответа" in s.bot.edits[-1].replace("\\", "")


# ── FA1: 429 на обычной отправке ────────────────────────────────────────────


@pytest.fixture
def _no_flood_wait(monkeypatch):
    """Ждать retry_after вживую тесту незачем — предмет проверки ветка, а не секунды."""
    monkeypatch.setattr(bot_module, "FLOOD_PAD", 0.0)


class FloodBot(JournalBot):
    """Telegram отвечает 429 на первые `floods` отправок."""

    def __init__(self, floods: int = 1):
        super().__init__()
        self.floods = floods

    async def send_message(self, chat_id, text, **kw):
        message = await super().send_message(chat_id, text, **kw)
        if self.floods:
            self.floods -= 1
            raise TelegramRetryAfter(
                method=None,  # type: ignore[arg-type]
                message="Flood control exceeded",
                retry_after=0,
            )
        return message


async def test_send_waits_out_the_flood_and_keeps_the_markup(stand, _no_flood_wait):
    """429 — «подожди», а не «разметка плохая»: повтор ТОЙ ЖЕ ступени, ответ не голым плейном."""
    s = _build(stand, StreamCore(reply="краткий ответ", deltas=[]), bot=FloodBot())
    await s.tg._on_text(_message())
    assert len(s.bot.sent) == 2, f"один повтор после 429: {s.bot.sent}"
    assert s.bot.kwargs[-1].get("parse_mode") == "MarkdownV2"
    assert "краткий ответ" in s.bot.sent[-1].replace("\\", "")


async def test_double_flood_still_delivers_the_answer(stand, _no_flood_wait):
    """Бан не кончается — ответ всё равно уходит, пусть и без разметки."""
    s = _build(stand, StreamCore(reply="краткий ответ", deltas=[]), bot=FloodBot(floods=2))
    await s.tg._on_text(_message())
    assert "краткий ответ" in s.bot.sent[-1].replace("\\", "")


async def test_no_project_removes_the_draft_and_says_so(stand):
    """Доставлять через черновик нечего — висящий обрубок хуже пустоты."""
    stand.store.projects.disable("office")
    s = _build(stand, StreamCore())
    await s.tg._on_text(_message())
    assert "проект" in s.bot.text.lower()
