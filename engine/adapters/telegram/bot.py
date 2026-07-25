"""Telegram-адаптер §5.5, §9 — тонкая склейка aiogram ↔ ядро.

Вся логика — в SessionRouter и модулях ядра (тестируются юнитом); здесь только aiogram-обвязка:
гейт доступа (AllowList, fail-closed), косметика (typing, реакция 👀, кнопки-вкладки, разбивка
4096, MarkdownV2), рендер результата (§5.4), алерт владельцу при auth-фейле, idle-reaper пула.

Команды слоя 1 (§5.5): /start /projects /switch /status /stop /close /clear /model /help.

Фаза 1b/1 добавляет три вещи, и у каждой политика лежит НЕ здесь, а в ядре — адаптер только
переносит байты:
  • кнопки-вкладки (§9): ряд под ответом; нажатие триггера = его prompt уходит в сессию тем
    же путём, что обычное сообщение. Раскладка и разбор callback — keyboard.py, набор кнопок —
    ButtonRegistry поверх инстансного buttons.yaml;
  • приём документов/фото (§5.5): attach.py + core/uploads.py (имя, лимит, untrusted-рамка);
  • отдача файлов маркером `[SEND_FILE:]` (§9): core/sendfile.py — песочница путей (§8.2).
    Корни песочницы считаются на КАЖДЫЙ ответ по текущему проекту окна: сессия могла
    переключить проект, и мозг чужого проекта отдавать нельзя.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardMarkup,
    Message,
    ReactionTypeEmoji,
)

from engine.adapters.telegram.attach import AttachmentIntake, attachment_from_message
from engine.adapters.telegram.draft import DraftStreamer
from engine.adapters.telegram.keyboard import (
    SystemAction,
    TriggerPress,
    build_keyboard,
    parse_callback,
)
from engine.adapters.telegram.render import render_result, render_tool_status
from engine.adapters.telegram.router import NoProjectError, SessionRouter
from engine.core.buttons import ButtonRegistry
from engine.core.events import TextDelta, TextStart, ToolStarted
from engine.core.formatting import to_telegram_markdown
from engine.core.pool import ClientPool
from engine.core.security import AllowList
from engine.core.sendfile import FileSandbox, SandboxError, blocked_message, extract_send_files
from engine.core.store import Store
from engine.core.streaming import split_message
from engine.core.uploads import frame_attachment_prompt

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
    "/verbose 0|1 — показывать шаги работы\n\n"
    # Кнопки видно под ответом, а вот про файлы догадаться нельзя — говорим прямо (§9).
    "Ещё можно прислать файл — документ или фото, разберу. Кнопки под ответом — быстрые задачи."
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
        buttons: ButtonRegistry | None = None,
        intake: AttachmentIntake | None = None,
        state_dir: str | Path | None = None,
        send_file_enabled: bool = True,
    ):
        # Bot передаётся готовым (один на процесс): тот же объект шлёт алерты владельцу
        # (make_owner_alert) и ведёт polling — два Bot на один токен не нужны.
        self._bot = bot
        self._dp = Dispatcher()
        self._allow = allow
        self._router = router
        self._store = store
        self._pool = pool
        # None у buttons/intake = фича выключена флагом .env (§6.1). Отсутствие объекта, а не
        # флаг внутри объекта: выключенную фичу нельзя случайно вызвать — её просто нет.
        self._buttons = buttons
        self._intake = intake
        self._state_dir = Path(state_dir) if state_dir is not None else None
        self._send_file_enabled = send_file_enabled
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
        # Вложения — до F.text: у документа с подписью текста нет (там caption), но порядок
        # держим явным, чтобы будущий хендлер не перехватил файлы молча.
        self._dp.message(F.document)(self._on_attachment)
        self._dp.message(F.photo)(self._on_attachment)
        self._dp.message(F.text)(self._on_text)
        self._dp.callback_query()(self._on_callback)

    def _guard(self, message: Message) -> bool:
        uid = message.from_user.id if message.from_user else None
        ok = self._allow.should_handle(uid, message.chat.type)
        if not ok:
            logger.warning("BLOCKED user=%s chat=%s", uid, message.chat.type)
        return ok

    def _guard_callback(self, callback: CallbackQuery) -> bool:
        """Тот же гейт для нажатий. Нет сообщения (слишком старое) → отказ (fail-closed)."""
        uid = callback.from_user.id if callback.from_user else None
        chat = getattr(callback.message, "chat", None)
        ok = self._allow.should_handle(uid, chat.type if chat else None)
        if not ok:
            logger.warning("BLOCKED callback user=%s", uid)
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
        await self._handle_prompt(
            chat_id=chat_id, thread_id=thread_id, user_id=user_id, prompt=message.text
        )

    async def _on_attachment(self, message: Message) -> None:
        """Документ/фото: скачать в uploads инстанса и отдать агенту путь в рамке (§5.5, §8.2).

        Гейт стоит ПЕРВЫМ и он же — гейт приёма файла: содержимое файла попадёт в контекст
        агента, который работает с bypassPermissions, поэтому кормить его файлами вправе
        только allow-list.
        """
        if not self._guard(message):
            return
        attachment = attachment_from_message(message)
        if attachment is None:
            return
        chat_id, thread_id, user_id = self._coords(message)
        caption = message.caption or ""

        if self._intake is None:
            await self._send(chat_id, thread_id, "Приём файлов выключен в настройках движка.")
            return
        await self._react(message)
        try:
            sid = self._router.ensure_session(chat_id=chat_id, thread_id=thread_id, user_id=user_id)
        except NoProjectError:
            await self._send(chat_id, thread_id, "Пока нет ни одного развёрнутого проекта.")
            return

        result = await self._intake.take(session_id=sid, attachment=attachment)
        if result.path is None:
            await self._send(chat_id, thread_id, result.error or "Файл не принят.")
            return
        prompt = frame_attachment_prompt(path=result.path, kind=attachment.kind, user_text=caption)
        await self._handle_prompt(
            chat_id=chat_id, thread_id=thread_id, user_id=user_id, prompt=prompt
        )

    async def _on_callback(self, callback: CallbackQuery) -> None:
        """Нажатие кнопки-вкладки (§9).

        Промпт НИКОГДА не берётся из callback — только из инстансного buttons.yaml по индексу
        (§8.2, см. keyboard.py). Всё непонятное — тихий отказ.
        """
        if not self._guard_callback(callback):
            await self._answer_callback(callback)
            return
        action = parse_callback(callback.data)
        if action is None:
            logger.warning("callback не распознан: %r", callback.data)
            await self._answer_callback(callback)
            return

        # Гейт уже отверг callback без сообщения, но проверяем повторно вместо `assert`:
        # координаты чата — вход в сессию, и «по идее не None» тут слишком дорогая ставка.
        source = callback.message
        if source is None:
            return
        chat_id = source.chat.id
        thread_id = getattr(source, "message_thread_id", None)
        user_id = callback.from_user.id

        if isinstance(action, SystemAction):
            await self._answer_callback(callback)
            await self._run_system_action(action.action, chat_id, thread_id)
            return

        assert isinstance(action, TriggerPress)
        button = self._buttons.resolve(action.index) if self._buttons is not None else None
        if button is None:
            # кнопка с прошлого сообщения, а buttons.yaml уже переписан
            await self._answer_callback(callback, "Кнопка устарела — обновлю ряд под ответом.")
            return
        await self._answer_callback(callback, button.label)
        await self._handle_prompt(
            chat_id=chat_id, thread_id=thread_id, user_id=user_id, prompt=button.prompt
        )

    async def _run_system_action(self, action: str, chat_id: int, thread_id: int | None) -> None:
        """Системные кнопки = уже существующие команды движка (§5.5), без обращения к LLM."""
        if action == "projects":
            await self._send(chat_id, thread_id, self._router.list_projects_text())
        elif action == "status":
            await self._send(chat_id, thread_id, self._router.status_text(chat_id, thread_id))
        elif action == "stop":
            await self._router.stop(chat_id, thread_id)
            await self._send(chat_id, thread_id, "⏹ Прервал.")

    @staticmethod
    async def _answer_callback(callback: CallbackQuery, text: str | None = None) -> None:
        """Погасить «часики» на кнопке. Без этого Telegram крутит их 30 секунд."""
        try:
            await callback.answer(text)
        except Exception:  # noqa: BLE001 — просроченный callback: не повод рвать обработку
            logger.debug("callback.answer не прошёл", exc_info=True)

    async def _handle_prompt(
        self, *, chat_id: int, thread_id: int | None, user_id: int, prompt: str
    ) -> None:
        """Единый путь промпта в сессию — для текста, для кнопки-триггера и для файла.

        Ровно один путь важен принципиально (§9, паттерн action_buttons): кнопка не должна
        получить «особую» обработку, иначе стриминг/ошибки/косметика начнут расходиться.
        """
        try:
            await self._bot.send_chat_action(chat_id, "typing", message_thread_id=thread_id)
        except Exception:  # noqa: BLE001 — typing косметический, не должен рвать обработку
            pass

        status = self._make_status_handler(chat_id, thread_id)
        draft = DraftStreamer(self._bot, chat_id=chat_id, thread_id=thread_id)

        def on_event(event) -> None:
            # Велс-трюк §9: текст «печатается» черновиком; статусы тулов — как раньше.
            # Осознанная косметика: при двух параллельных сообщениях одного топика черновик
            # второго может появиться раньше финала первого (session_lock отпускается до
            # отправки финала) — данные не теряются, строгий порядок не гарантируем.
            if isinstance(event, TextDelta):
                draft.on_delta(event.text)
            elif isinstance(event, TextStart):
                draft.on_reset()
            elif status is not None:
                status(event)

        try:
            result = await self._router.on_message(
                chat_id=chat_id,
                thread_id=thread_id,
                user_id=user_id,
                text=prompt,
                on_event=on_event,
            )
        except NoProjectError:
            await self._send(chat_id, thread_id, "Пока нет ни одного развёрнутого проекта.")
            return
        finally:
            # Черновик убираем ДО финала: финал приходит отдельным аккуратным сообщением.
            await draft.finish()
        await self._deliver(chat_id, thread_id, render_result(result))

    # ── доставка ответа: текст + вложения по маркеру (§9) ─────────────────────

    async def _deliver(self, chat_id: int, thread_id: int | None, text: str) -> None:
        """Отправить ответ: текст нарезкой, файлы по маркерам, ряд кнопок под последним."""
        paths: list[str] = []
        if self._send_file_enabled:
            text, paths = extract_send_files(text)
        files, notes = self._resolve_send_files(chat_id, thread_id, paths)

        items: list[tuple[str, object]] = [("text", c) for c in split_message(text) if c.strip()]
        items += [("doc", p) for p in files]
        items += [("text", n) for n in notes]
        if not items:  # ответ состоял из одного маркера, и тот не прошёл
            items = [("text", "…")]

        markup = self._keyboard()
        last = len(items) - 1
        for i, (kind, payload) in enumerate(items):
            reply_markup = markup if i == last else None
            if kind == "doc":
                await self._send_document(chat_id, thread_id, payload, reply_markup)  # type: ignore[arg-type]
            else:
                await self._send(chat_id, thread_id, str(payload), reply_markup)

    def _resolve_send_files(
        self, chat_id: int, thread_id: int | None, paths: list[str]
    ) -> tuple[list[Path], list[str]]:
        """Прогнать пути через песочницу (§8.2). Непрошедшие → строки-объяснения человеку."""
        if not paths:
            return [], []
        sandbox = FileSandbox(self._sandbox_roots(chat_id, thread_id))
        files: list[Path] = []
        notes: list[str] = []
        for raw in paths:
            try:
                files.append(sandbox.resolve(raw))
            except SandboxError as exc:
                logger.warning("SEND_FILE отклонён (%s): %s", exc.reason, raw)
                notes.append(blocked_message(raw, exc))
        return files, notes

    def _sandbox_roots(self, chat_id: int, thread_id: int | None) -> list[Path]:
        """Разрешённые корни: мозг ТЕКУЩЕГО проекта окна + state/ инстанса.

        Считаются на каждый ответ, а не один раз при старте: /switch меняет проект, и мозг
        другого проекта отдавать нельзя (§8.2 — изоляция путей).
        """
        roots: list[Path] = []
        brain = self._router.brain_path(chat_id, thread_id)
        if brain:
            roots.append(Path(brain))
        if self._state_dir is not None:
            roots.append(self._state_dir)
        return roots

    def _keyboard(self) -> InlineKeyboardMarkup | None:
        if self._buttons is None or not self._buttons.enabled:
            return None
        return build_keyboard(self._buttons.get())

    def _make_status_handler(self, chat_id: int, thread_id: int | None):
        """verbose ≥1: одно статус-сообщение «⚙️ запускаю X…» на первый tool-вызов.

        Ровно ОДНО сообщение на запрос (не на каждый ToolStarted) — иначе tool-heavy задача
        выстрелила бы 20 send_message → 429 → терялся бы и статус, и финальный ответ (§5.3
        флуд-лимит, находка ревью D). Статус уходит в тот же топик (message_thread_id).
        """
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
        chat_id, thread_id, _ = self._coords(message)
        await self._send(chat_id, thread_id, text)

    async def _send(
        self,
        chat_id: int,
        thread_id: int | None,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        """Отправка по координатам, а не ответом на сообщение.

        Нажатие кнопки — не сообщение пользователя, отвечать «на него» нечем; поэтому единый
        путь отправки один — по chat_id + thread_id. MarkdownV2 с фолбэком в плейн: ответ
        важнее разметки.
        """
        md = to_telegram_markdown(text)
        if md is not None:
            try:
                await self._bot.send_message(
                    chat_id,
                    md,
                    message_thread_id=thread_id,
                    parse_mode="MarkdownV2",
                    reply_markup=reply_markup,
                )
                return
            except Exception:  # noqa: BLE001 — при ошибке разметки шлём плейн, ответ не теряем
                logger.warning("MarkdownV2 не отправился, плейн", exc_info=True)
        try:
            await self._bot.send_message(
                chat_id, text, message_thread_id=thread_id, reply_markup=reply_markup
            )
        except Exception:  # noqa: BLE001
            logger.exception("не удалось ответить в чат %s", chat_id)

    async def _send_document(
        self,
        chat_id: int,
        thread_id: int | None,
        path: Path,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        """Отдать файл документом (§9). Сбой отправки — строкой в чат, а не потерей ответа."""
        try:
            await self._bot.send_document(
                chat_id,
                FSInputFile(path),
                message_thread_id=thread_id,
                reply_markup=reply_markup,
            )
        except Exception:  # noqa: BLE001 — файл не ушёл: скажем словами
            logger.warning("не отправился файл %s", path, exc_info=True)
            await self._send(chat_id, thread_id, f"📎 Файл «{path.name}» не отправился.")

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
