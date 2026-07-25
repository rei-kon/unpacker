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
from dataclasses import dataclass
from pathlib import Path
from typing import assert_never

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InaccessibleMessage,
    InlineKeyboardMarkup,
    Message,
    ReactionTypeEmoji,
)

from engine.adapters.telegram.attach import AttachmentIntake, attachment_from_message
from engine.adapters.telegram.draft import DraftStreamer
from engine.adapters.telegram.keyboard import (
    SystemAction,
    SystemActionName,
    build_keyboard,
    button_digest,
    parse_callback,
)
from engine.adapters.telegram.render import render_result, render_tool_status
from engine.adapters.telegram.router import NoProjectError, SessionRouter
from engine.core.buttons import ButtonRegistry
from engine.core.events import Event, TextDelta, TextStart, ToolStarted
from engine.core.formatting import to_telegram_markdown
from engine.core.pool import ClientPool
from engine.core.security import AllowList
from engine.core.sendfile import (
    FileSandbox,
    SandboxError,
    SendFilePolicy,
    blocked_message,
    extract_send_files,
)
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
# Ответ на тип сообщения, которого движок не умеет (C18/K11). Говорим ЧТО не приняли и
# что делать вместо этого: «не понял» без альтернативы — тупик для новичка.
_UNSUPPORTED = (
    "Такой тип сообщений я пока не принимаю (голос, видео, аудио, стикеры).\n"
    "Пришли текст, документ или фото — их разберу."
)
_REAPER_INTERVAL = 60.0


@dataclass(frozen=True)
class OutText:
    """Кусок текстового ответа в очереди доставки."""

    text: str


@dataclass(frozen=True)
class OutDocument:
    """Файл в очереди доставки (маркер `[SEND_FILE:]` прошёл проверку путей)."""

    path: Path


# Очередь доставки — union, а не (kind, payload) со строковым тегом: тип полезной нагрузки
# теперь проверяет mypy, а не комментарий (K2).
Outgoing = OutText | OutDocument


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
        send_file: SendFilePolicy | None = None,
    ):
        # Bot передаётся готовым (один на процесс): тот же объект шлёт алерты владельцу
        # (make_owner_alert) и ведёт polling — два Bot на один токен не нужны.
        self._bot = bot
        self._dp = Dispatcher()
        self._allow = allow
        self._router = router
        self._store = store
        self._pool = pool
        # ОДНА конвенция косметики §6.1 (K10): объект есть — фича включена, None — выключена
        # флагом .env. Отсутствие объекта, а не флаг внутри объекта: выключенную фичу нельзя
        # случайно вызвать — её просто нет, а состояния «включено, но недонастроено»
        # (send_file_enabled=True при state_dir=None) больше не существует.
        self._buttons = buttons
        self._intake = intake
        self._send_file = send_file
        # Окна (chat:thread), в которых прямо сейчас идёт генерация — см. _is_busy (ADV-13)
        self._busy: set[str] = set()
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
        # Финальный хендлер — ПОСЛЕДНИМ и без фильтра (C18/K11). До него доходят голос,
        # видео, аудио, стикеры, локации: раньше хендлера не было вовсе, и бот на них
        # молчал. Для ученика молчание неотличимо от «бот умер», особенно когда /help
        # обещает приём файлов. Честный отказ дешевле паники и письма автору.
        self._dp.message()(self._on_unsupported)
        self._dp.callback_query()(self._on_callback)

    def _guard(self, message: Message) -> bool:
        uid = message.from_user.id if message.from_user else None
        ok = self._allow.should_handle(uid, message.chat.type)
        if not ok:
            logger.warning("BLOCKED user=%s chat=%s", uid, message.chat.type)
        return ok

    def _guard_callback(self, callback: CallbackQuery) -> bool:
        """Тот же гейт для нажатий. Нет сообщения (слишком старое) → отказ (fail-closed).

        `callback.message` — это `Message | InaccessibleMessage | None`, и у всех трёх
        вариантов чат читается по-разному только в None-случае: getattr здесь был лишним
        (K4), тип уже всё говорит.
        """
        uid = callback.from_user.id
        source = callback.message
        ok = self._allow.should_handle(uid, source.chat.type if source is not None else None)
        if not ok:
            logger.warning("BLOCKED callback user=%s", uid)
        return ok

    @staticmethod
    def _coords(message: Message) -> tuple[int, int | None, int]:
        """Координаты окна: чат, топик, автор.

        `from_user` тут уже не None — сообщение без автора отвергает `_guard`
        (`should_handle(None, ...)` = False). Явное исключение вместо `type: ignore`:
        если инвариант когда-нибудь сломается, мы увидим причину в логе, а не молча
        уроним хендлер на None (K4, K19).
        """
        user = message.from_user
        if user is None:
            raise ValueError("сообщение без from_user не должно доходить до хендлера")
        return message.chat.id, message.message_thread_id, user.id

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

    async def _on_unsupported(self, message: Message) -> None:
        """Тип сообщения, который движок пока не умеет (голос, видео, стикер, локация).

        Гейт первым и здесь: чужому не отвечаем даже «не принимаю» — сам факт ответа
        подтверждает, что бот живой и чей-то (fail-closed §8.2).
        """
        if not self._guard(message):
            return
        await self._reply(message, _UNSUPPORTED)

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

        source = callback.message
        if source is None or isinstance(source, InaccessibleMessage):
            # Явная ветка вместо getattr (K8): у InaccessibleMessage нет message_thread_id,
            # и getattr тихо отдавал None — ответ уходил в General вместо топика, где
            # человек нажал кнопку. Честный отказ лучше ответа не туда.
            await self._answer_callback(callback, "Сообщение слишком старое — напиши заново.")
            return
        chat_id = source.chat.id
        thread_id = source.message_thread_id
        # автор — тот, кто НАЖАЛ, а не автор сообщения с кнопкой
        user_id = callback.from_user.id

        if isinstance(action, SystemAction):
            await self._answer_callback(callback)
            await self._run_system_action(action.action, chat_id, thread_id)
            return

        button = self._buttons.resolve(action.index) if self._buttons is not None else None
        # ADV-13: индекс сходится, а кнопка уже другая — `buttons.yaml` переписали, пока
        # старое сообщение висело в истории. Выполнить «промпт с этого места» = выполнить
        # ЧУЖУЮ инструкцию под старой подписью, поэтому отпечаток обязателен.
        if button is None or button_digest(button) != action.digest:
            await self._answer_callback(callback, "Кнопка устарела — обновлю ряд под ответом.")
            return
        if self._is_busy(chat_id, thread_id):
            # ADV-13: второе нажатие = второй полный прогон агента и второй расход квоты
            # подписки. Тост дешевле и честнее, чем удвоенный счёт.
            await self._answer_callback(callback, "Уже работаю над этим — секунду.")
            return
        await self._answer_callback(callback, button.label)
        await self._handle_prompt(
            chat_id=chat_id, thread_id=thread_id, user_id=user_id, prompt=button.prompt
        )

    async def _run_system_action(
        self, action: SystemActionName, chat_id: int, thread_id: int | None
    ) -> None:
        """Системные кнопки = уже существующие команды движка (§5.5), без обращения к LLM.

        Принимает готовый Literal из parse_callback (K3): раньше был `action: str` с
        цепочкой `elif` и молчаливым «ничего не делаем» в конце — новое системное действие
        добавлялось бы в keyboard.py и тихо не работало. Теперь `assert_never` заставит
        mypy показать пропущенную ветку.
        """
        if action == "projects":
            await self._send(chat_id, thread_id, self._router.list_projects_text())
        elif action == "status":
            await self._send(chat_id, thread_id, self._router.status_text(chat_id, thread_id))
        elif action == "stop":
            await self._router.stop(chat_id, thread_id)
            await self._send(chat_id, thread_id, "⏹ Прервал.")
        else:
            assert_never(action)

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

        Тут же отмечается «окно занято» — по этому признаку `_on_callback` отказывает
        повторному нажатию (ADV-13). Снимается в finally: упавший прогон не должен запирать
        окно навсегда.
        """
        busy_key = self._router.surface_key(chat_id, thread_id)
        self._busy.add(busy_key)
        try:
            await self._run_prompt(
                chat_id=chat_id, thread_id=thread_id, user_id=user_id, prompt=prompt
            )
        finally:
            self._busy.discard(busy_key)

    def _is_busy(self, chat_id: int, thread_id: int | None) -> bool:
        """Идёт ли прямо сейчас генерация в этом окне (chat:thread).

        Замок именно на окно, а не на бота: топики Telegram — независимые окна (§5.2), и
        занятость одного не должна глушить остальные.
        """
        return self._router.surface_key(chat_id, thread_id) in self._busy

    async def _run_prompt(
        self, *, chat_id: int, thread_id: int | None, user_id: int, prompt: str
    ) -> None:
        try:
            await self._bot.send_chat_action(chat_id, "typing", message_thread_id=thread_id)
        except Exception:  # noqa: BLE001 — typing косметический, не должен рвать обработку
            pass

        status = self._make_status_handler(chat_id, thread_id)
        draft = DraftStreamer(self._bot, chat_id=chat_id, thread_id=thread_id)

        def on_event(event: Event) -> None:
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
        if self._send_file is not None:
            text, paths = extract_send_files(text)
        files, notes = self._resolve_send_files(chat_id, thread_id, paths)

        # Очередь типизирована union'ом, а не парой (str, object) с `type: ignore` (K2):
        # раньше «doc» и «text» различались строкой, и mypy не мог проверить, что в
        # send_document уходит Path, а в send_message — str.
        items: list[Outgoing] = [OutText(c) for c in split_message(text) if c.strip()]
        items += [OutDocument(p) for p in files]
        items += [OutText(n) for n in notes]
        if not items:  # ответ состоял из одного маркера, и тот не прошёл
            items = [OutText("…")]

        markup = self._keyboard()
        last = len(items) - 1
        for i, item in enumerate(items):
            reply_markup = markup if i == last else None
            if isinstance(item, OutDocument):
                await self._send_document(chat_id, thread_id, item.path, reply_markup)
            else:
                await self._send(chat_id, thread_id, item.text, reply_markup)

    def _resolve_send_files(
        self, chat_id: int, thread_id: int | None, paths: list[str]
    ) -> tuple[list[Path], list[str]]:
        """Прогнать пути через проверку (§8.2). Непрошедшие → строки-объяснения человеку."""
        if not paths:
            return [], []
        files: list[Path] = []
        notes: list[str] = []
        sandbox = self._sandbox(chat_id, thread_id)
        for raw in paths:
            try:
                files.append(sandbox.resolve(raw))
            except SandboxError as exc:
                logger.warning("SEND_FILE отклонён (%s): %s", exc.reason, raw)
                notes.append(blocked_message(raw, exc))
        return files, notes

    def _sandbox(self, chat_id: int, thread_id: int | None) -> FileSandbox:
        """Разрешённые корни: мозг ТЕКУЩЕГО проекта окна + state/ инстанса.

        Считаются на каждый ответ, а не один раз при старте: /switch меняет проект, и мозг
        другого проекта отдавать нельзя (§8.2 — изоляция путей). base = папка-мозг: cwd
        агента именно там (§5.2), от неё и считаются относительные пути.
        """
        roots: list[Path] = []
        brain = self._router.brain_path(chat_id, thread_id)
        base = Path(brain) if brain else None
        if base is not None:
            roots.append(base)
        if self._send_file is not None:
            roots.append(self._send_file.state_root)
        return FileSandbox(roots, base=base)

    def _keyboard(self) -> InlineKeyboardMarkup | None:
        if self._buttons is None:
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
