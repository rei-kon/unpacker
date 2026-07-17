"""AgentCore §5.1 — фасад пул↔сессии, без живого SDK (фейковый стрим-клиент).

Проверяем логику связывания: cwd из brain_path, resume из claude_session_id, переписывание
claude_session_id после ответа (урок C2a), per-session сериализация (data-plane), interrupt
мимо session-lock (control-plane). Живой resume — интеграционный смоук на VPS.
"""

import asyncio

import pytest

from engine.core.agent import AgentCore
from engine.core.pool import ClientPool
from engine.core.store import Store


class FakeResult:
    """Дублёр ResultMessage: несёт session_id и total_cost_usd (маркер финала для collect)."""

    def __init__(self, session_id, cost=0.001):
        self.session_id = session_id
        self.total_cost_usd = cost
        self.content = []


class FakeText:
    def __init__(self, text):
        self.text = text


class FakeMessage:
    def __init__(self, text, session_id):
        self.content = [FakeText(text)]
        self.session_id = session_id


class FakeStreamClient:
    """Дублёр ClaudeSDKClient: помнит переданные options и отвечает фиксированным потоком."""

    last_options = None

    def __init__(self, options):
        self.options = options
        FakeStreamClient.last_options = options
        self.connected = False
        self.disconnected = False
        self.interrupts = 0
        self.queries = []
        self._reply_session = "claude-generated-id"

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.disconnected = True

    async def interrupt(self):
        self.interrupts += 1

    async def query(self, prompt):
        self.queries.append(prompt)

    async def receive_response(self):
        yield FakeMessage("ответ агента", self._reply_session)
        yield FakeResult(self._reply_session)


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "state.db"))
    s.projects.create(
        slug="office", name="Офис", brain_path="/brains/office", default_model="sonnet"
    )
    yield s
    s.close()


def _core(store):
    built = {}

    def options_builder(*, cwd, resume, model):
        built["cwd"] = cwd
        built["resume"] = resume
        built["model"] = model
        return {"cwd": cwd, "resume": resume, "model": model}

    pool = ClientPool(
        factory=lambda o: FakeStreamClient(o),
        ceiling=5,
        idle_timeout=1800,
        time_fn=lambda: 0.0,
    )
    core = AgentCore(store=store, pool=pool, options_builder=options_builder, response_timeout=30)
    return core, pool, built


async def test_ask_uses_brain_path_as_cwd(store):
    core, pool, built = _core(store)
    sess = store.sessions.create(owner_user_id=111, project_slug="office")
    await core.ask(sess.id, "привет")
    assert built["cwd"] == "/brains/office"  # cwd жёстко из projects.brain_path (C2b)
    assert built["model"] == "sonnet"
    await pool.close_all()


async def test_ask_returns_text_and_rewrites_claude_session_id(store):
    core, pool, _ = _core(store)
    sess = store.sessions.create(owner_user_id=111, project_slug="office")
    assert store.sessions.get(sess.id).claude_session_id is None

    text = await core.ask(sess.id, "привет")
    assert "ответ агента" in text
    # claude_session_id переписан из ответа (урок C2a)
    assert store.sessions.get(sess.id).claude_session_id == "claude-generated-id"
    await pool.close_all()


async def test_ask_passes_resume_on_warm_reuse(store):
    """Второй ask по той же сессии переиспользует тёплый клиент (без нового connect)."""
    core, pool, _ = _core(store)
    sess = store.sessions.create(owner_user_id=111, project_slug="office")
    await core.ask(sess.id, "раз")
    live1 = pool.get_live(sess.id)
    await core.ask(sess.id, "два")
    live2 = pool.get_live(sess.id)
    assert live1 is live2  # тёплый клиент, не пересоздан
    assert live1.queries == ["раз", "два"]
    await pool.close_all()


async def test_interrupt_bypasses_session_lock(store):
    """§5.1: interrupt по живой ссылке идёт МИМО session-lock — не ждёт генерацию."""
    core, pool, _ = _core(store)
    sess = store.sessions.create(owner_user_id=111, project_slug="office")
    await core.ask(sess.id, "прогрев")  # поднять клиента в пул

    async with pool.session_lock(sess.id):  # держим data-plane занятым
        await asyncio.wait_for(core.interrupt(sess.id), timeout=1.0)

    assert pool.get_live(sess.id).interrupts == 1
    await pool.close_all()


async def test_interrupt_absent_session_is_noop(store):
    core, pool, _ = _core(store)
    await core.interrupt("no-such")  # не падает
    await pool.close_all()


async def test_ask_missing_session_raises(store):
    core, pool, _ = _core(store)
    with pytest.raises(LookupError):
        await core.ask("ghost", "привет")
    await pool.close_all()


class ExplodingClient(FakeStreamClient):
    async def receive_response(self):
        raise RuntimeError("SDK упал")
        yield  # делает метод async-генератором (недостижим)


class HangingClient(FakeStreamClient):
    async def receive_response(self):
        await asyncio.sleep(100)  # висит дольше response_timeout
        yield


def _core_with(store, client_cls, response_timeout=30):
    def options_builder(*, cwd, resume, model):
        return {"cwd": cwd, "resume": resume, "model": model}

    pool = ClientPool(
        factory=lambda o: client_cls(o), ceiling=5, idle_timeout=1800, time_fn=lambda: 0.0
    )
    core = AgentCore(
        store=store,
        pool=pool,
        options_builder=options_builder,
        response_timeout=response_timeout,
    )
    return core, pool


async def test_ask_drops_client_on_exception(store):
    """§5.4 базовый: SDK упал → клиент выкинут из пула, не залипает тёплым битым."""
    core, pool = _core_with(store, ExplodingClient)
    sess = store.sessions.create(owner_user_id=111, project_slug="office")
    with pytest.raises(RuntimeError):
        await core.ask(sess.id, "привет")
    assert pool.get_live(sess.id) is None  # битый клиент удалён
    await pool.close_all()


async def test_ask_drops_client_on_timeout(store):
    """§5.4 базовый: ответ не пришёл за response_timeout → клиент выкинут (не хвост в чат)."""
    core, pool = _core_with(store, HangingClient, response_timeout=0.1)
    sess = store.sessions.create(owner_user_id=111, project_slug="office")
    with pytest.raises(asyncio.TimeoutError):
        await core.ask(sess.id, "привет")
    assert pool.get_live(sess.id) is None
    await pool.close_all()
