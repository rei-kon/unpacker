"""Тёплый пул клиентов §5.1 — логика без живого SDK (фейковый клиент).

Проверяем без сети: RAM-формула, тёплое переиспользование, LRU/idle-вытеснение, учёт
занятости (занятый клиент не вытесняется — несущая находка ревью B), разведение планов,
чистка per-session locks, фоновый reaper. Живой resume/interrupt — смоук на VPS.
"""

import asyncio

import pytest

from engine.core.pool import ClientPool, compute_pool_ceiling


class FakeClient:
    def __init__(self, options=None):
        self.options = options
        self.connected = False
        self.disconnected = False
        self.interrupts = 0

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.disconnected = True

    async def interrupt(self):
        self.interrupts += 1

    async def query(self, prompt):
        pass

    async def receive_response(self):
        return
        yield


class Clock:
    """Управляемые часы для детерминизма idle-evict (реальное время в тестах — флаки)."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def _pool(clock=None, ceiling=3, idle=1800):
    created = []

    def factory(options):
        c = FakeClient(options)
        created.append(c)
        return c

    pool = ClientPool(
        factory=factory,
        ceiling=ceiling,
        idle_timeout=idle,
        time_fn=clock or (lambda: 0.0),
    )
    return pool, created


async def _lease_once(pool, sid):
    """Взять и сразу отпустить lease — клиент остаётся тёплым в пуле (in_use=0)."""
    async with pool.lease(sid, options={}):
        pass


# ── RAM-формула ──────────────────────────────────────────────────────────────


def test_pool_ceiling_from_ram():
    gb = 1024**3
    assert compute_pool_ceiling(4 * gb) == 2  # (4-1.5)/1 = 2.5 → 2
    assert compute_pool_ceiling(8 * gb) == 6
    assert compute_pool_ceiling(2 * gb) == 1  # никогда меньше 1
    assert compute_pool_ceiling(1 * gb) == 1


# ── тёплое переиспользование ─────────────────────────────────────────────────


async def test_lease_creates_and_reuses():
    pool, created = _pool()
    async with pool.lease("s1", options={}) as c1:
        pass
    async with pool.lease("s1", options={}) as c2:
        pass
    assert c1 is c2  # тёплый: тот же клиент без нового connect
    assert len(created) == 1
    await pool.close_all()


async def test_get_live_none_when_absent():
    pool, _ = _pool()
    assert pool.get_live("nope") is None


# ── LRU-вытеснение ───────────────────────────────────────────────────────────


async def test_lru_evicts_oldest_over_ceiling():
    clock = Clock()
    pool, created = _pool(clock=clock, ceiling=2)
    await _lease_once(pool, "s1")
    clock.advance(1)
    await _lease_once(pool, "s2")
    clock.advance(1)
    await _lease_once(pool, "s3")  # переполнение → вытеснить самый старый (s1)

    assert pool.get_live("s1") is None
    assert pool.get_live("s2") is not None
    assert pool.get_live("s3") is not None
    assert created[0].disconnected is True
    await pool.close_all()


async def test_release_refreshes_lru_order():
    clock = Clock()
    pool, _ = _pool(clock=clock, ceiling=2)
    await _lease_once(pool, "s1")
    clock.advance(1)
    await _lease_once(pool, "s2")
    clock.advance(1)
    await _lease_once(pool, "s1")  # обращение к s1 обновляет свежесть (heartbeat в release)
    clock.advance(1)
    await _lease_once(pool, "s3")  # вытеснить самый старый — теперь s2

    assert pool.get_live("s1") is not None
    assert pool.get_live("s2") is None
    await pool.close_all()


# ── учёт занятости (несущая находка ревью B) ─────────────────────────────────


async def test_busy_client_not_evicted_by_lru():
    """Клиент под открытым lease (стримит) не вытесняется — иначе disconnect посреди стрима."""
    clock = Clock()
    pool, created = _pool(clock=clock, ceiling=1)
    async with pool.lease("s1", options={}):  # s1 занят
        clock.advance(1)
        await _lease_once(pool, "s2")  # переполнение, но s1 занят → его не трогаем
        assert pool.get_live("s1") is not None
        assert created[0].disconnected is False
    await pool.close_all()


async def test_busy_client_not_evicted_by_idle():
    clock = Clock()
    pool, created = _pool(clock=clock, ceiling=5, idle=100)
    async with pool.lease("s1", options={}):  # занят и «простаивает» по часам
        clock.advance(1000)
        await pool.evict_idle()
        assert pool.get_live("s1") is not None  # занятого idle не трогает
        assert created[0].disconnected is False
    await pool.close_all()


# ── idle-evict + reaper ──────────────────────────────────────────────────────


async def test_idle_evict_disconnects_stale():
    clock = Clock()
    pool, _ = _pool(clock=clock, ceiling=5, idle=1800)
    await _lease_once(pool, "s1")
    clock.advance(1000)
    await _lease_once(pool, "s2")
    clock.advance(1000)  # s1 простаивает 2000с > 1800

    await pool.evict_idle()
    assert pool.get_live("s1") is None
    assert pool.get_live("s2") is not None
    await pool.close_all()


async def test_reaper_calls_evict_idle():
    """run_idle_reaper реально дёргает evict_idle — иначе idle-освобождение RAM инертно."""
    clock = Clock()
    pool, _ = _pool(clock=clock, ceiling=5, idle=100)
    await _lease_once(pool, "s1")
    clock.advance(200)  # s1 stale

    task = asyncio.create_task(pool.run_idle_reaper(0.01))
    for _ in range(50):
        if pool.get_live("s1") is None:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert pool.get_live("s1") is None  # reaper вытеснил простаивающего
    await pool.close_all()


# ── control-plane ────────────────────────────────────────────────────────────


async def test_get_live_reaches_client_for_interrupt():
    pool, _ = _pool()
    await _lease_once(pool, "s1")
    live = pool.get_live("s1")
    await live.interrupt()
    assert live.interrupts == 1
    await pool.close_all()


async def test_evict_disconnects_and_forgets():
    pool, created = _pool()
    await _lease_once(pool, "s1")
    await pool.evict("s1")
    assert created[0].disconnected is True
    assert pool.get_live("s1") is None
    await pool.close_all()


# ── чистка per-session locks (утечка памяти в долгом процессе) ────────────────


async def test_session_lock_cleaned_on_evict():
    pool, _ = _pool()
    await _lease_once(pool, "s1")
    pool.session_lock("s1")  # лок создан
    assert "s1" in pool._session_locks
    await pool.evict("s1")
    assert "s1" not in pool._session_locks  # ушёл вместе с клиентом


async def test_close_all_clears_locks_and_clients():
    pool, created = _pool(ceiling=5)
    await _lease_once(pool, "s1")
    await _lease_once(pool, "s2")
    pool.session_lock("s1")
    await pool.close_all()
    assert all(c.disconnected for c in created)
    assert pool._session_locks == {}


# ── семафор ──────────────────────────────────────────────────────────────────


async def test_semaphore_concurrency_value():
    pool, _ = _pool(ceiling=10)
    assert pool.concurrency == 10
    custom = ClientPool(
        factory=lambda o: FakeClient(o),
        ceiling=10,
        idle_timeout=1800,
        time_fn=lambda: 0.0,
        concurrency=2,
    )
    assert custom.concurrency == 2
    await pool.close_all()
    await custom.close_all()


# ── B5: опции сессии изменились — тёплый клиент собран под старые ────────────


async def test_lease_rebuilds_client_when_fingerprint_changes():
    """SDK не меняет модель у уже поднятого коннекта, а `_acquire` возвращал тёплого клиента
    независимо от опций. Отпечаток опций — то, по чему пул понимает, что клиент устарел."""
    created = []

    def factory(options):
        c = FakeClient(options)
        created.append(c)
        return c

    pool = ClientPool(factory=factory, ceiling=5, idle_timeout=1800, time_fn=lambda: 0.0)
    async with pool.lease("s1", {"model": "sonnet"}, fingerprint="sonnet") as first:
        pass
    async with pool.lease("s1", {"model": "haiku"}, fingerprint="haiku") as second:
        pass

    assert first is not second
    assert second.options == {"model": "haiku"}
    assert first.disconnected  # старый клиент честно отключён, а не брошен
    assert len(created) == 2
    await pool.close_all()


async def test_lease_keeps_client_when_fingerprint_same():
    created = []

    def factory(options):
        c = FakeClient(options)
        created.append(c)
        return c

    pool = ClientPool(factory=factory, ceiling=5, idle_timeout=1800, time_fn=lambda: 0.0)
    async with pool.lease("s1", {"model": "sonnet"}, fingerprint="sonnet") as first:
        pass
    async with pool.lease("s1", {"model": "sonnet"}, fingerprint="sonnet") as second:
        pass

    assert first is second
    assert len(created) == 1
    await pool.close_all()
