"""Тёплый пул клиентов §5.1.

Проблема: холодный запуск ClaudeSDKClient ≈ 12с на сообщение — убийца UX чата. Решение —
держать живой клиент на активную сессию, с добавками из боевого опыта:
  • idle-evict — клиент без активности N минут отключается; сессия остаётся в SQLite,
    следующее сообщение поднимет её через resume (заплатив 12с один раз);
  • потолок пула — max N живых клиентов, вытеснение LRU; N вычисляется из RAM машины
    (ориентир ~1 ГБ на активного агента), а не константа — иначе дефолт «10» на 4 ГБ = OOM.

Учёт занятости (ревью Среза B — несущая находка). Клиент, который прямо сейчас стримит
ответ, НЕЛЬЗЯ вытеснять: disconnect посреди receive_response рвёт генерацию. Поэтому доступ
к клиенту — только через `lease()`: она поднимает refcount на время работы, а LRU/idle-evict
пропускают клиентов с refcount > 0.

Data-plane и control-plane разведены (§5.1): сообщения сессии сериализуются per-session
lock'ом (session_lock), НО /stop и evict идут МИМО замка — interrupt по живой ссылке
(get_live). Дисциплина отказа: disconnect всегда ВНЕ _pool_lock и под таймаутом (иначе один
зависший teardown заморозил бы весь пул).

Пул от живого SDK не зависит: работает с любым client-объектом фабрики. Живой resume/
interrupt проверяется интеграционным смоуком на VPS (canaries/test_int_agentcore.py).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, Protocol

_GB = 1024**3
_RAM_RESERVE_GB = 1.5  # резерв под ОС и сам процесс движка (офиц. ориентир hosting)
_RAM_PER_AGENT_GB = 1.0
_DISCONNECT_TIMEOUT = 15.0
_CONNECT_TIMEOUT = 60.0  # реальный connect ≈12с; wedged-connect иначе висит бесконечно


class Client(Protocol):
    """Контракт клиента — подмножество ClaudeSDKClient, нужное движку.

    Пул использует connect/disconnect/interrupt; AgentCore — ещё query/receive_response.
    Один протокол на один объект: это тот же ClaudeSDKClient, дробить не за чем.
    """

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def interrupt(self) -> None: ...
    async def query(self, prompt: str) -> None: ...
    def receive_response(self) -> AsyncIterator[Any]: ...


ClientFactory = Callable[[Any], Client]


def compute_pool_ceiling(ram_bytes: int) -> int:
    """Потолок живых клиентов из RAM: (RAM − резерв) / на_агента, но не меньше 1."""
    ram_gb = ram_bytes / _GB
    ceiling = int((ram_gb - _RAM_RESERVE_GB) // _RAM_PER_AGENT_GB)
    return max(1, ceiling)


@dataclass
class _Entry:
    client: Client
    last_activity: float
    in_use: int = 0  # refcount активных lease — занятого клиента не вытесняем
    # Отпечаток опций, под которые клиент поднят (у нас — модель сессии). Клиент собирается
    # ОДИН раз при connect: сменить модель у живого коннекта нельзя, поэтому расхождение
    # отпечатка = клиент устарел и должен быть пересобран.
    fingerprint: Any = None


class ClientPool:
    def __init__(
        self,
        *,
        factory: ClientFactory,
        ceiling: int,
        idle_timeout: float,
        time_fn: Callable[[], float] = time.monotonic,
        concurrency: int | None = None,
        connect_timeout: float = _CONNECT_TIMEOUT,
    ):
        self._factory = factory
        self._ceiling = max(1, ceiling)
        self._idle_timeout = idle_timeout
        self._now = time_fn
        self._connect_timeout = connect_timeout
        # LRU-порядок: самый старый — в начале, свежий — в конце
        self._clients: OrderedDict[str, _Entry] = OrderedDict()
        self._pool_lock = asyncio.Lock()  # защищает структуру пула; НЕ держится через disconnect
        self._session_locks: dict[str, asyncio.Lock] = {}  # data-plane, per-session
        self.concurrency = concurrency or self._ceiling
        self._semaphore = asyncio.Semaphore(self.concurrency)

    @property
    def semaphore(self) -> asyncio.Semaphore:
        """Общий backpressure на процесс — вокруг ГЕНЕРАЦИИ (не connect), см. AgentCore."""
        return self._semaphore

    def session_lock(self, session_id: str) -> asyncio.Lock:
        """Data-plane: сериализация сообщений одной сессии. Control-plane его НЕ берёт."""
        lock = self._session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_id] = lock
        return lock

    def get_live(self, session_id: str) -> Client | None:
        """Живая ссылка на клиента для control-plane (interrupt) — МИМО session-lock.

        Не обновляет last_activity: /stop не продлевает жизнь клиента.
        """
        entry = self._clients.get(session_id)
        return entry.client if entry else None

    @contextlib.asynccontextmanager
    async def lease(
        self, session_id: str, options: Any, fingerprint: Any = None
    ) -> AsyncIterator[Client]:
        """Взять клиента сессии в работу: поднимает refcount, чтобы его не вытеснили посреди
        стрима. Обязательный путь доступа для генерации — не `get_live` (тот для interrupt).

        `fingerprint` — отпечаток значимых опций. Не совпал с тем, под который поднят тёплый
        клиент → клиента пересоздаём: иначе `/model` обещает смену модели, а человек
        продолжает платить за старую (ревью SDK-ядра, находка 6).
        """
        client = await self._acquire(session_id, options, fingerprint)
        try:
            yield client
        finally:
            await self._release(session_id)

    async def _acquire(self, session_id: str, options: Any, fingerprint: Any = None) -> Client:
        stale: Client | None = None
        async with self._pool_lock:
            entry = self._clients.get(session_id)
            if entry is not None and entry.fingerprint != fingerprint:
                # опции сессии изменились — тёплый клиент собран под старые и уже не годится
                self._clients.pop(session_id)
                stale, entry = entry.client, None
            if entry is not None:
                entry.last_activity = self._now()
                entry.in_use += 1
                self._clients.move_to_end(session_id)
                return entry.client
        if stale is not None:
            await _safe_disconnect(stale)  # disconnect ВНЕ pool_lock

        # connect вне pool_lock (реальный ≈12с) и под таймаутом (wedged connect иначе вечен)
        client = self._factory(options)
        try:
            await asyncio.wait_for(client.connect(), timeout=self._connect_timeout)
        except BaseException:
            await _safe_disconnect(client)
            raise

        victims: list[Client] = []
        async with self._pool_lock:
            existing = self._clients.get(session_id)
            if existing is not None:
                # пока коннектились, другой lease той же сессии успел — наш лишний
                victims.append(client)
                existing.last_activity = self._now()
                existing.in_use += 1
                self._clients.move_to_end(session_id)
                result = existing.client
            else:
                self._clients[session_id] = _Entry(
                    client, self._now(), in_use=1, fingerprint=fingerprint
                )
                self._clients.move_to_end(session_id)
                victims.extend(self._collect_over_ceiling())
                result = client
        for v in victims:  # disconnect ВНЕ pool_lock
            await _safe_disconnect(v)
        return result

    async def _release(self, session_id: str) -> None:
        async with self._pool_lock:
            entry = self._clients.get(session_id)
            if entry is not None:
                entry.in_use = max(0, entry.in_use - 1)
                entry.last_activity = self._now()  # heartbeat: активный клиент не «стареет»
                self._clients.move_to_end(session_id)

    def _collect_over_ceiling(self) -> list[Client]:
        """Под _pool_lock: снять самых старых НЕ занятых, пока клиентов больше потолка.

        Занятых (in_use>0) не трогаем — их вытеснение оборвало бы активный стрим. Если все
        сверх потолка заняты, пул временно превышает ceiling (отпустится при release).
        Возвращает список клиентов на disconnect (его делаем вне lock).
        """
        victims: list[Client] = []
        for sid in list(self._clients.keys()):
            if len(self._clients) - len(victims) <= self._ceiling:
                break
            entry = self._clients[sid]
            if entry.in_use == 0:
                self._clients.pop(sid)
                self._drop_lock_if_free(sid)
                victims.append(entry.client)
        return victims

    async def evict(self, session_id: str) -> None:
        """Control-plane: выкинуть клиента сессии (disconnect + забыть). Мимо session-lock.

        Принудительно, даже если in_use>0: зовётся при ошибке (§5.4 — битый клиент вон) и
        при /close. Обрыв активного стрима здесь осознан (сессию закрывают/чинят).
        """
        async with self._pool_lock:
            entry = self._clients.pop(session_id, None)
            self._drop_lock_if_free(session_id)
        if entry is not None:
            await _safe_disconnect(entry.client)

    async def evict_idle(self) -> None:
        """Вытеснить НЕ занятых клиентов, простаивающих дольше idle_timeout. Зовётся reaper'ом."""
        now = self._now()
        async with self._pool_lock:
            stale = [
                sid
                for sid, e in self._clients.items()
                if e.in_use == 0 and now - e.last_activity > self._idle_timeout
            ]
            entries = [self._clients.pop(sid) for sid in stale]
            for sid in stale:
                self._drop_lock_if_free(sid)
        for entry in entries:
            await _safe_disconnect(entry.client)

    async def run_idle_reaper(self, interval: float) -> None:
        """Фоновый цикл idle-освобождения RAM. Рантайм (Срез D) поднимает его create_task'ом
        и отменяет при shutdown — без него evict_idle никогда не срабатывает (§5.1 инертен).
        """
        while True:
            await asyncio.sleep(interval)
            await self.evict_idle()

    async def close_all(self) -> None:
        async with self._pool_lock:
            entries = list(self._clients.values())
            self._clients.clear()
            self._session_locks.clear()
        for entry in entries:
            await _safe_disconnect(entry.client)

    def _drop_lock_if_free(self, session_id: str) -> None:
        """Убрать per-session lock, если он свободен — иначе _session_locks течёт по числу
        когда-либо виденных сессий (ревью надёжности). Вызывать только под _pool_lock.
        """
        lock = self._session_locks.get(session_id)
        if lock is not None and not lock.locked():
            del self._session_locks[session_id]


async def _safe_disconnect(client: Client) -> None:
    try:
        await asyncio.wait_for(client.disconnect(), timeout=_DISCONNECT_TIMEOUT)
    except Exception:  # noqa: BLE001 — disconnect (в т.ч. зависший) не должен ронять пул
        pass
