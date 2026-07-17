"""Telegram-адаптер §5.5, §9 — тонкая склейка aiogram ↔ ядро.

Вся логика — в SessionRouter (тестируется юнитом); здесь только aiogram-обвязка: гейт доступа
(AllowList, fail-closed), косметика (typing, реакция 👀, разбивка 4096, MarkdownV2), рендер
результата (§5.4), алерт владельцу при auth-фейле, запуск idle-reaper пула.

Команды слоя 1 (§5.5): /start /projects /switch /status /stop /close /clear /model /help.
Проверяется на живом боте (нужен токен BotFather) — логика уже покрыта юнит-тестами роутера.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, ReactionTypeEmoji

from engine.adapters.telegram.render import render_result, render_tool_status
from engine.adapters.telegram.router import NoProjectError, SessionRouter
from engine.core.events import ToolStarted
from engine.core.formatting import to_telegram_markdown
from engine.core.pool import ClientPool
from engine.core.security import AllowList
from engine.core.store import Store
from engine.core.streaming import split_message

logger = logging.getLogger("unpacker.engine")

_HELP = (
    "Я — агент из папки-мозга. Просто пиши — отвечу.\n\n"
    "/projects — список проектов\n"
    "/switch <проект> — переключиться на проект (новая сессия)\n"
    "/status — что сейчас активно\n"
    "/stop — прервать ответ\n"
    "/clear — начать заново\n"
    "/close — закрыть сессию\n"
    "/model opus|sonnet|haiku — сменить модель\n"
    "/verbose 0|1 — показывать шаги работы"
)
_REAPER_INTERVAL = 60.0


def make_owner_alert(bot: Bot, owner_id: int) -> Callable[[str], None]:
    """Fire-and-forget алерт владельцу (§5.4): не блокирует ask, сеть висит — ask не ждёт."""

    def alert(detail: str) -> None:
        asyncio.create_task(_safe_send(bot, owner_id, f"⚠️ Движок деградировал: {detail}"))

    return alert


async def _safe_send(bot: Bot, chat_id: int, text: str, thread_id: int | None = None) -> None:
    try:
        await bot.send_message(chat_id, text, message_thread_id=thread_id)
    except Exception:  # noqa: BLE001 — падение отправки не должно всплывать
        logger.warning("не удалось отправить сообщение в чат %s", chat_id, exc_info=True)


class TelegramBot:
    def __init__(
        self,
        *,
        bot: Bot,
        allow: AllowList,
        router: SessionRouter,
        store: Store,
        pool: ClientPool,
    ):
        # Bot передаётся готовым (один на процесс): тот же объект шлёт алерты владельцу
        # (make_owner_alert) и ведёт polling — два Bot на один токен не нужны.
        self._bot = bot
        self._dp = Dispatcher()
        self._allow = allow
        self._router = router
        self._store = store
        self._pool = pool
        self._register()

    @property
    def bot(self) -> Bot:
        return self._bot

    def _register(self) -> None:
        self._dp.message(Command("start"))(self._on_start)
        self._dp.message(Command("help"))(self._on_help)
        self._dp.message(Command("projects"))(self._on_projects)
        self._dp.message(Command("switch"))(self._on_switch)
        self._dp.message(Command("status"))(self._on_status)
        self._dp.message(Command("stop"))(self._on_stop)
        self._dp.message(Command("close"))(self._on_close)
        self._dp.message(Command("clear"))(self._on_clear)
        self._dp.message(Command("model"))(self._on_model)
        self._dp.message(Command("verbose"))(self._on_verbose)
        self._dp.message(F.text)(self._on_text)

    def _guard(self, message: Message) -> bool:
        uid = message.from_user.id if message.from_user else None
        ok = self._allow.should_handle(uid, message.chat.type)
        if not ok:
            logger.warning("BLOCKED user=%s chat=%s", uid, message.chat.type)
        return ok

    @staticmethod
    def _coords(message: Message) -> tuple[int, int | None, int]:
        return message.chat.id, message.message_thread_id, message.from_user.id  # type: ignore[union-attr]

    async def _on_start(self, message: Message) -> None:
        if not self._guard(message):
            return
        await self._reply(message, "На связи. Пиши — отвечу. /help — команды.")

    async def _on_help(self, message: Message) -> None:
        if not self._guard(message):
            return
        await self._reply(message, _HELP)

    async def _on_projects(self, message: Message) -> None:
        if not self._guard(message):
            return
        await self._reply(message, self._router.list_projects_text())

    async def _on_switch(self, message: Message, command: CommandObject) -> None:
        if not self._guard(message):
            return
        slug = (command.args or "").strip()
        if not slug:
            await self._reply(message, "Укажи проект: /switch <slug>. Список — /projects.")
            return
        chat_id, thread_id, user_id = self._coords(message)
        msg = await self._router.switch_project(
            chat_id=chat_id, thread_id=thread_id, user_id=user_id, slug=slug
        )
        await self._reply(message, msg)

    async def _on_status(self, message: Message) -> None:
        if not self._guard(message):
            return
        chat_id, thread_id, _ = self._coords(message)
        await self._reply(message, self._router.status_text(chat_id, thread_id))

    async def _on_stop(self, message: Message) -> None:
        if not self._guard(message):
            return
        chat_id, thread_id, _ = self._coords(message)
        await self._router.stop(chat_id, thread_id)
        await self._reply(message, "⏹ Прервал.")

    async def _on_close(self, message: Message) -> None:
        if not self._guard(message):
            return
        chat_id, thread_id, _ = self._coords(message)
        await self._reply(message, self._router.close(chat_id, thread_id))

    async def _on_clear(self, message: Message) -> None:
        if not self._guard(message):
            return
        chat_id, thread_id, user_id = self._coords(message)
        try:
            msg = await self._router.clear(chat_id=chat_id, thread_id=thread_id, user_id=user_id)
        except NoProjectError:
            msg = "Пока нет ни одного развёрнутого проекта."
        await self._reply(message, msg)

    async def _on_model(self, message: Message, command: CommandObject) -> None:
        if not self._guard(message):
            return
        model = (command.args or "").strip()
        if not model:
            await self._reply(message, "Укажи модель: /model opus|sonnet|haiku.")
            return
        chat_id, thread_id, _ = self._coords(message)
        await self._reply(message, self._router.set_model(chat_id, thread_id, model))

    async def _on_verbose(self, message: Message, command: CommandObject) -> None:
        if not self._guard(message):
            return
        arg = (command.args or "").strip()
        if arg not in ("0", "1"):
            await self._reply(message, "Использование: /verbose 0 (тихо) или /verbose 1 (шаги).")
            return
        chat_id, thread_id, _ = self._coords(message)
        await self._reply(message, self._router.set_verbose(chat_id, thread_id, int(arg)))

    async def _on_text(self, message: Message) -> None:
        if not self._guard(message) or not message.text:
            return
        chat_id, thread_id, user_id = self._coords(message)
        await self._react(message)
        try:
            await self._bot.send_chat_action(chat_id, "typing", message_thread_id=thread_id)
        except Exception:  # noqa: BLE001 — typing косметический, не должен рвать обработку
            pass

        on_event = self._make_status_handler(message)
        try:
            result = await self._router.on_message(
                chat_id=chat_id,
                thread_id=thread_id,
                user_id=user_id,
                text=message.text,
                on_event=on_event,
            )
        except NoProjectError:
            await self._reply(message, "Пока нет ни одного развёрнутого проекта.")
            return
        for chunk in split_message(render_result(result)):
            await self._reply(message, chunk)

    def _make_status_handler(self, message: Message):
        """verbose ≥1: одно статус-сообщение «⚙️ запускаю X…» на первый tool-вызов.

        Ровно ОДНО сообщение на запрос (не на каждый ToolStarted) — иначе tool-heavy задача
        выстрелила бы 20 send_message → 429 → терялся бы и статус, и финальный ответ (§5.3
        флуд-лимит, находка ревью D). Статус уходит в тот же топик (message_thread_id).
        """
        chat_id, thread_id, _ = self._coords(message)
        sid = self._store.bindings.resolve("tg", self._router.surface_key(chat_id, thread_id))
        session = self._store.sessions.get(sid) if sid else None
        verbose = session.verbose if session else 0
        if verbose < 1:
            return None

        shown = {"done": False}

        def handler(event) -> None:
            if isinstance(event, ToolStarted) and not shown["done"]:
                shown["done"] = True
                asyncio.create_task(
                    _safe_send(self._bot, chat_id, render_tool_status(event), thread_id)
                )

        return handler

    async def _react(self, message: Message) -> None:
        try:
            await message.react([ReactionTypeEmoji(emoji="👀")])
        except Exception:  # noqa: BLE001 — реакция косметическая, не критична
            pass

    async def _reply(self, message: Message, text: str) -> None:
        md = to_telegram_markdown(text)
        if md is not None:
            try:
                await message.answer(md, parse_mode="MarkdownV2")
                return
            except Exception:  # noqa: BLE001 — при ошибке разметки шлём плейн, ответ не теряем
                logger.warning("MarkdownV2 не отправился, плейн", exc_info=True)
        try:
            await message.answer(text)
        except Exception:  # noqa: BLE001
            logger.exception("не удалось ответить в чат %s", message.chat.id)

    async def run(self) -> None:
        reaper = asyncio.create_task(self._supervised_reaper())
        try:
            await self._dp.start_polling(self._bot)
        finally:
            reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reaper  # дождаться отмены, не оставлять висящую задачу
            await self._pool.close_all()
            self._store.close()

    async def _supervised_reaper(self) -> None:
        """Обёртка над pool.run_idle_reaper: сбой evict_idle не должен молча убить idle-evict
        навсегда (иначе RAM растёт без сигнала — находка ревью D)."""
        while True:
            try:
                await self._pool.run_idle_reaper(_REAPER_INTERVAL)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — переживаем сбой цикла и продолжаем
                logger.warning("idle-reaper упал, перезапускаю", exc_info=True)
                await asyncio.sleep(_REAPER_INTERVAL)
