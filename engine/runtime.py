"""Сборка движка воедино из Settings → готовый TelegramBot (§10 путь новичка).

Строит внутренний контур (§8.1): подписка, bypassPermissions, setting_sources=["user","project"]
(канарейка C1 подтвердила — так скиллы мозга видны на Linux). cwd/resume берутся ядром из
projects.brain_path/claude_session_id (C2b), здесь только базовые опции. Потолок пула — из RAM
машины (§5.1). Владелец (для health-алертов §5.4) — первый из allow-list.

Клиентский контур (API-ключ, урезанный ToolPolicy) — фаза 4; здесь только внутренний.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aiogram import Bot
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

from engine.adapters.telegram.attach import AttachmentIntake
from engine.adapters.telegram.bot import TelegramBot, make_owner_alert
from engine.adapters.telegram.draft import DraftTuning
from engine.adapters.telegram.router import SessionRouter
from engine.core.agent import AgentCore, OptionsBuilder, detect_ram_bytes
from engine.core.buttons import ButtonRegistry
from engine.core.config import Settings
from engine.core.health import HealthMarker
from engine.core.pool import ClientPool, compute_pool_ceiling
from engine.core.security import AllowList
from engine.core.sendfile import SEND_FILE_INSTRUCTIONS, SendFilePolicy
from engine.core.store import Store
from engine.core.uploads import UploadStore


def _make_client(options: Any) -> ClaudeSDKClient:
    return ClaudeSDKClient(options=options)


def _options_builder(settings: Settings) -> OptionsBuilder:
    # Выключенную фичу не описываем: обещать модели неработающий маркер — та же ложь,
    # только с другой стороны.
    send_file_docs = SEND_FILE_INSTRUCTIONS if settings.send_file_enabled else None

    def build(*, cwd: str, resume: str | None, model: str | None) -> ClaudeAgentOptions:
        kw: dict[str, Any] = {
            "cwd": cwd,
            "permission_mode": "bypassPermissions",
            "setting_sources": ["user", "project"],
            # Псевдо-стриминг §9 (Велс-трюк): токен-дельты для черновика «печатает…».
            # Событий становится на порядок больше — on_event обязан оставаться дешёвым.
            "include_partial_messages": True,
        }
        if resume:
            kw["resume"] = resume
        if model:
            kw["model"] = model
        if settings.max_turns:
            kw["max_turns"] = settings.max_turns
        # M-04: про маркер `[SEND_FILE:]` модель узнаёт ТОЛЬКО отсюда. Раньше append был
        # пуст, в шаблонах мозга маркера не было — движок вырезал маркер, которого агент
        # никогда не писал, и обещание «пришлю файлом» из примеров было ложью. Описание
        # маркера — часть контракта движка, поэтому дописывает его движок, а не мозг:
        # мозгов много, маркер один.
        parts = [p for p in (settings.system_prompt_append, send_file_docs) if p]
        if parts:
            kw["system_prompt"] = {
                "type": "preset",
                "preset": "claude_code",
                "append": "\n\n".join(parts),
            }
        return ClaudeAgentOptions(**kw)

    return build


def build_bot(settings: Settings) -> TelegramBot:
    """Собрать все части движка из настроек. НЕ запускает polling — это делает .run()."""
    owner = settings.owner_user_id
    if owner is None:
        # пустой allow-list = бот, который не ответит никому и не пошлёт алертов — мёртвый
        # молча. Падаем громко на старте, а не поднимаемся зомби (находка ревью D).
        raise ValueError("ALLOWED_USER_IDS пуст — некому отвечать и некому слать health-алерты")

    # Bot валидирует токен в конструкторе — создаём ДО открытия Store, чтобы битый токен
    # не оставил висеть sqlite-соединение (находка ревью D).
    bot = Bot(token=settings.telegram_bot_token)
    store = Store(settings.db_path)
    ceiling = compute_pool_ceiling(detect_ram_bytes())
    pool = ClientPool(factory=_make_client, ceiling=ceiling, idle_timeout=settings.idle_timeout)
    health = HealthMarker(settings.health_path)
    alert = make_owner_alert(bot, owner)

    core = AgentCore(
        store=store,
        pool=pool,
        options_builder=_options_builder(settings),
        response_timeout=settings.response_timeout,
        health=health,
        on_alert=alert,
    )
    router = SessionRouter(
        store=store, core=core, default_project_slug=settings.default_project_slug
    )
    allow = AllowList(settings.allowed_user_ids)

    # Косметика Фазы 1b (§6.1: включена по умолчанию, выключается флагом в .env).
    # Кнопки: реестр создаём всегда и передаём флаг внутрь — так «выключено» и «в файле нет
    # кнопок» остаются разными состояниями (первое убирает ряд целиком, второе оставляет
    # системные кнопки). Приём файлов при выключенном флаге НЕ создаётся вовсе: нет объекта —
    # нечего случайно позвать.
    buttons = ButtonRegistry(settings.buttons_path) if settings.buttons_enabled else None
    intake = None
    if settings.uploads_enabled:
        intake = AttachmentIntake(
            bot=bot,
            uploads=UploadStore(settings.uploads_path),
            max_bytes=settings.max_upload_bytes,
        )
    # Второй корень проверки путей `[SEND_FILE:]` — state/ инстанса (там же uploads):
    # принятый файл агент вправе вернуть. Берём ЯВНЫЙ STATE_DIR, а не parent(db_path):
    # при дефолтном `DB_PATH=state.db` корнем становился каталог инстанса с `.env`,
    # и `[SEND_FILE:.env]` проходил проверку штатно (K1/M-07/SEC-5/C17).
    send_file = (
        SendFilePolicy(state_root=Path(settings.state_dir).resolve())
        if settings.send_file_enabled
        else None
    )
    # Темп черновика — из .env: в живой группе стриминг можно притушить или выключить,
    # не правя код движка (§6.1, та же конвенция «нет объекта — нет фичи»).
    draft = (
        DraftTuning(interval=settings.stream_interval, max_units=settings.stream_max_units)
        if settings.stream_enabled
        else None
    )

    return TelegramBot(
        bot=bot,
        allow=allow,
        router=router,
        store=store,
        pool=pool,
        buttons=buttons,
        intake=intake,
        send_file=send_file,
        draft=draft,
    )
