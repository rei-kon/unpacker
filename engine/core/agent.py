"""AgentCore §5.1 — фасад между поверхностями и пулом клиентов.

Одна точка, куда приходит сообщение из любой поверхности (ТГ, веб). Ядро смотрит в Store:
какой проект (папка-мозг) и какая сессия — берёт cwd из projects.brain_path (C2b: чужой
cwd рушится с ProcessError), resume из claude_session_id, продолжает диалог.

Два плана разведены (§5.1):
  • data-plane — `ask()` под per-session lock: сообщения одной сессии сериализуются. ВСЁ
    состояние сессии (чтение claude_session_id для resume И запись его из ответа) — ВНУТРИ
    lock, иначе два окна одной сессии форкают claude_session_id (находки ревью B: до-локовый
    снимок resume теряет контекст после evict; запись вне lock даёт out-of-order);
  • control-plane — `interrupt()` идёт МИМО lock по живой ссылке, иначе /stop встал бы в
    очередь за генерацией, которую должен прервать.

Порядок ресурсов: session_lock → lease (connect ВНЕ семафора) → semaphore (только вокруг
генерации) — иначе тёплая сессия ждала бы чужой 12с холодный старт под общим семафором.

Отказ (§5.4 базовый): любой таймаут/исключение → drop клиента из пула (evict), иначе битый
тёплый клиент залипает и следующее сообщение получает хвост прошлого ответа. Полный контракт
ошибок по subtype (error_max_turns / error_during_execution, плейсхолдеры, health-алерт) —
Срез C. От живого SDK ядро не зависит: options_builder и client-фабрика инъектируются.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from typing import Any

from engine.core.pool import ClientPool
from engine.core.store import Store
from engine.core.streaming import collect_response_with_session

OptionsBuilder = Callable[..., Any]
_QUERY_TIMEOUT = 60.0  # отправка запроса в subprocess — не должна висеть вечно (держит permit)


def detect_ram_bytes() -> int:
    """Физическая память машины (Linux и macOS). Потолок пула считается отсюда (§5.1)."""
    try:
        return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError, AttributeError):
        return 4 * 1024**3  # консервативный дефолт, если sysconf недоступен


class AgentCore:
    def __init__(
        self,
        *,
        store: Store,
        pool: ClientPool,
        options_builder: OptionsBuilder,
        response_timeout: float,
        query_timeout: float = _QUERY_TIMEOUT,
    ):
        self._store = store
        self._pool = pool
        self._build_options = options_builder
        self._response_timeout = response_timeout
        self._query_timeout = query_timeout

    async def ask(self, session_id: str, prompt: str) -> str:
        """Обработать сообщение сессии и вернуть текст финального ответа.

        Всё — под per-session lock: чтение resume, генерация, запись claude_session_id.
        Семафор — только вокруг генерации (не connect). Ошибка → drop клиента (§5.4).
        """
        async with self._pool.session_lock(session_id):
            session = self._store.sessions.get(session_id)
            if session is None:
                raise LookupError(f"нет сессии: {session_id!r}")
            project = self._store.projects.get(session.project_slug)
            if project is None:
                raise LookupError(f"нет проекта сессии: {session.project_slug!r}")

            options = self._build_options(
                cwd=project.brain_path,
                resume=session.claude_session_id,
                model=session.model,
            )

            try:
                async with self._pool.lease(session_id, options) as client:
                    async with self._pool.semaphore:  # backpressure на генерацию, не на connect
                        await asyncio.wait_for(client.query(prompt), timeout=self._query_timeout)
                        text, claude_session_id = await asyncio.wait_for(
                            collect_response_with_session(client.receive_response()),
                            timeout=self._response_timeout,
                        )
            except BaseException:
                # битый/зависший клиент нельзя переиспользовать — вон из пула (§5.4 базовый).
                # Полное ветвление по subtype и плейсхолдер-ответ — Срез C.
                await self._pool.evict(session_id)
                raise

            if claude_session_id:
                # переписываем из ответа (урок C2a) — ВНУТРИ session_lock, без гонки/out-of-order
                self._store.sessions.set_claude_session_id(session_id, claude_session_id)
            return text

    async def interrupt(self, session_id: str) -> None:
        """Control-plane: прервать идущую генерацию сессии. МИМО session-lock.

        Безопасный no-op, если клиента нет или его параллельно вытеснили (get_live→interrupt —
        окно гонки с evict): /stop не должен падать исключением в хендлере поверхности.
        """
        client = self._pool.get_live(session_id)
        if client is None:
            return
        try:
            await client.interrupt()
        except Exception:  # noqa: BLE001 — interrupt по вытесняемому клиенту = no-op, не ошибка
            pass
