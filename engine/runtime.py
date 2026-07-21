"""Сборка движка воедино из Settings → готовый TelegramBot (§10 путь новичка).

Строит внутренний контур (§8.1): подписка, bypassPermissions, setting_sources=["user","project"]
(канарейка C1 подтвердила — так скиллы мозга видны на Linux). cwd/resume берутся ядром из
projects.brain_path/claude_session_id (C2b), здесь только базовые опции. Потолок пула — из RAM
машины (§5.1). Владелец (для health-алертов §5.4) — первый из allow-list.

Клиентский контур (API-ключ, урезанный ToolPolicy) — фаза 4; здесь только внутренний.
"""

from __future__ import annotations

from typing import Any

from aiogram import Bot
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

from engine.adapters.telegram.bot import TelegramBot, make_owner_alert
from engine.adapters.telegram.router import SessionRouter
from engine.core.agent import AgentCore, detect_ram_bytes
from engine.core.config import Settings
from engine.core.health import HealthMarker
from engine.core.pool import ClientPool, compute_pool_ceiling
from engine.core.security import AllowList
from engine.core.store import Store


def _make_client(options: Any) -> ClaudeSDKClient:
    return ClaudeSDKClient(options=options)


def _options_builder(settings: Settings):
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
        if settings.system_prompt_append:
            kw["system_prompt"] = {
                "type": "preset",
                "preset": "claude_code",
                "append": settings.system_prompt_append,
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
    return TelegramBot(bot=bot, allow=allow, router=router, store=store, pool=pool)
