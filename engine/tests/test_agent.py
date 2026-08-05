"""AgentCore §5.1 + §5.4 — фасад пул↔сессии, без живого SDK (фейковый стрим-клиент).

Логика связывания (cwd/resume/claude_session_id, per-session сериализация, interrupt мимо
lock) + контракт ошибок §5.4 (drop-on-error, exec_error retry, auth→health+alert).
"""

import asyncio

import pytest

from engine.core.agent import AgentCore
from engine.core.health import HealthMarker
from engine.core.pool import ClientPool
from engine.core.store import Store


class FakeResult:
    def __init__(self, session_id, subtype="success", is_error=False, api_error_status=None):
        self.session_id = session_id
        self.total_cost_usd = 0.001
        self.subtype = subtype
        self.is_error = is_error
        self.api_error_status = api_error_status
        self.content = []


class FakeText:
    def __init__(self, text):
        self.text = text


class FakeMessage:
    def __init__(self, text, session_id):
        self.content = [FakeText(text)]
        self.session_id = session_id


class FakeStreamClient:
    """Дублёр ClaudeSDKClient с настраиваемым финалом (subtype/api_error_status)."""

    subtype: str = "success"
    api_error_status: int | None = None

    def __init__(self, options):
        self.options = options
        self.disconnected = False
        self.interrupts = 0
        self.queries = []
        self._reply_session = "claude-generated-id"

    async def connect(self):
        pass

    async def disconnect(self):
        self.disconnected = True

    async def interrupt(self):
        self.interrupts += 1

    async def query(self, prompt):
        self.queries.append(prompt)

    async def receive_response(self):
        yield FakeMessage("ответ агента", self._reply_session)
        yield FakeResult(
            self._reply_session, subtype=self.subtype, api_error_status=self.api_error_status
        )


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "state.db"))
    s.projects.create(
        slug="office", name="Офис", brain_path="/brains/office", default_model="sonnet"
    )
    yield s
    s.close()


def _core(store, client_cls=FakeStreamClient, response_timeout=30, **core_kw):
    built = {}
    created = []

    def options_builder(*, cwd, resume, model):
        built.update(cwd=cwd, resume=resume, model=model)
        return {"cwd": cwd, "resume": resume, "model": model}

    def factory(o):
        c = client_cls(o)
        created.append(c)
        return c

    pool = ClientPool(factory=factory, ceiling=5, idle_timeout=1800, time_fn=lambda: 0.0)
    core = AgentCore(
        store=store,
        pool=pool,
        options_builder=options_builder,
        response_timeout=response_timeout,
        **core_kw,
    )
    return core, pool, built, created


async def test_ask_uses_brain_path_as_cwd(store):
    core, pool, built, _ = _core(store)
    sess = store.sessions.create(owner_user_id=111, project_slug="office")
    await core.ask(sess.id, "привет")
    assert built["cwd"] == "/brains/office"  # cwd жёстко из projects.brain_path (C2b)
    assert built["model"] == "sonnet"
    await pool.close_all()


async def test_ask_returns_result_and_rewrites_claude_session_id(store):
    core, pool, _, _ = _core(store)
    sess = store.sessions.create(owner_user_id=111, project_slug="office")
    assert store.sessions.get(sess.id).claude_session_id is None

    result = await core.ask(sess.id, "привет")
    assert result.outcome.kind == "ok"
    assert "ответ агента" in result.text
    assert store.sessions.get(sess.id).claude_session_id == "claude-generated-id"
    await pool.close_all()


async def test_ask_warm_reuse(store):
    core, pool, _, _ = _core(store)
    sess = store.sessions.create(owner_user_id=111, project_slug="office")
    await core.ask(sess.id, "раз")
    live1 = pool.get_live(sess.id)
    await core.ask(sess.id, "два")
    live2 = pool.get_live(sess.id)
    assert live1 is live2
    assert live1.queries == ["раз", "два"]
    await pool.close_all()


async def test_on_event_receives_final(store):
    """verbose-путь: on_event получает события, включая Final."""
    from engine.core.events import Final

    core, pool, _, _ = _core(store)
    sess = store.sessions.create(owner_user_id=111, project_slug="office")
    seen = []
    await core.ask(sess.id, "привет", on_event=seen.append)
    assert any(isinstance(e, Final) for e in seen)
    await pool.close_all()


async def test_interrupt_bypasses_session_lock(store):
    core, pool, _, _ = _core(store)
    sess = store.sessions.create(owner_user_id=111, project_slug="office")
    await core.ask(sess.id, "прогрев")
    async with pool.session_lock(sess.id):
        await asyncio.wait_for(core.interrupt(sess.id), timeout=1.0)
    assert pool.get_live(sess.id).interrupts == 1
    await pool.close_all()


async def test_interrupt_absent_session_is_noop(store):
    core, pool, _, _ = _core(store)
    await core.interrupt("no-such")
    await pool.close_all()


async def test_ask_missing_session_raises(store):
    core, pool, _, _ = _core(store)
    with pytest.raises(LookupError):
        await core.ask("ghost", "привет")
    await pool.close_all()


# ── контракт ошибок §5.4 ──────────────────────────────────────────────────────


class ExplodingClient(FakeStreamClient):
    async def receive_response(self):
        raise RuntimeError("SDK упал")
        yield


class HangingClient(FakeStreamClient):
    async def receive_response(self):
        await asyncio.sleep(100)
        yield


async def test_ask_drops_client_on_exception(store):
    """SDK упал → outcome=other_error, клиент выкинут (не залипает), ask НЕ бросает."""
    core, pool, _, _ = _core(store, client_cls=ExplodingClient)
    sess = store.sessions.create(owner_user_id=111, project_slug="office")
    result = await core.ask(sess.id, "привет")
    assert result.outcome.kind == "other_error"
    assert pool.get_live(sess.id) is None
    await pool.close_all()


async def test_ask_drops_client_on_timeout(store):
    core, pool, _, _ = _core(store, client_cls=HangingClient, response_timeout=0.1)
    sess = store.sessions.create(owner_user_id=111, project_slug="office")
    result = await core.ask(sess.id, "привет")
    assert result.outcome.kind == "other_error"
    assert pool.get_live(sess.id) is None
    await pool.close_all()


class AuthFailClient(FakeStreamClient):
    api_error_status = 401


async def test_ask_auth_error_flags_health_and_alerts(store, tmp_path):
    """401 → health degraded + алерт владельцу (риск #13)."""
    health = HealthMarker(str(tmp_path / "health.json"))
    alerts = []
    core, pool, _, _ = _core(
        store, client_cls=AuthFailClient, health=health, on_alert=alerts.append
    )
    sess = store.sessions.create(owner_user_id=111, project_slug="office")
    result = await core.ask(sess.id, "привет")
    assert result.outcome.kind == "auth_error"
    assert health.read()["status"] == "degraded"
    assert len(alerts) == 1
    assert pool.get_live(sess.id) is None
    await pool.close_all()


class ExecErrorThenOk(FakeStreamClient):
    """Первый созданный клиент отдаёт exec_error, следующий — success (проверка retry)."""

    _created = 0

    def __init__(self, options):
        super().__init__(options)
        type(self)._created += 1
        self.subtype = "error_during_execution" if type(self)._created == 1 else "success"


async def test_ask_retries_once_on_exec_error(store):
    ExecErrorThenOk._created = 0
    core, pool, _, created = _core(store, client_cls=ExecErrorThenOk)
    sess = store.sessions.create(owner_user_id=111, project_slug="office")
    result = await core.ask(sess.id, "привет")
    assert result.outcome.kind == "ok"  # второй заход успешен
    assert len(created) == 2  # первый клиент выкинут (evict), поднят второй
    await pool.close_all()


class AlwaysExecError(FakeStreamClient):
    subtype = "error_during_execution"


async def test_ask_gives_up_after_second_exec_error(store):
    core, pool, _, created = _core(store, client_cls=AlwaysExecError)
    sess = store.sessions.create(owner_user_id=111, project_slug="office")
    result = await core.ask(sess.id, "привет")
    assert result.outcome.kind == "exec_error"  # оба захода — exec_error, сдаёмся
    assert len(created) == 2  # ретрай реально был: первый клиент evict, поднят второй
    await pool.close_all()


async def test_auth_error_not_retried(store):
    """auth не ретраится — ровно один клиент создан, немедленный возврат."""
    core, pool, _, created = _core(store, client_cls=AuthFailClient)
    sess = store.sessions.create(owner_user_id=111, project_slug="office")
    result = await core.ask(sess.id, "привет")
    assert result.outcome.kind == "auth_error"
    assert len(created) == 1  # без повторной попытки
    await pool.close_all()


class CancellingClient(FakeStreamClient):
    async def receive_response(self):
        raise asyncio.CancelledError()
        yield


async def test_cancellation_propagates(store):
    """§5.4/R1: отмена (shutdown) НЕ глотается — пробрасывается, не деградирует в other_error."""
    core, pool, _, _ = _core(store, client_cls=CancellingClient)
    sess = store.sessions.create(owner_user_id=111, project_slug="office")
    with pytest.raises(asyncio.CancelledError):
        await core.ask(sess.id, "привет")
    assert pool.get_live(sess.id) is None  # клиент всё равно выкинут
    await pool.close_all()


async def test_on_event_exception_does_not_kill_generation(store):
    """Сбой рендера (on_event бросил) не роняет генерацию и не теряет ответ."""
    core, pool, _, _ = _core(store)
    sess = store.sessions.create(owner_user_id=111, project_slug="office")

    def bad_event(_ev):
        raise RuntimeError("рендер упал")

    result = await core.ask(sess.id, "привет", on_event=bad_event)
    assert result.outcome.kind == "ok"  # ответ дошёл несмотря на сбой рендера
    assert "ответ агента" in result.text
    await pool.close_all()


async def test_health_recovers_on_max_turns(store, tmp_path):
    """Восстановление здоровья не залипает: max_turns (токен жив) сбрасывает degraded→ok."""
    health = HealthMarker(str(tmp_path / "health.json"))
    health.degraded("была auth-проблема")

    class MaxTurnsClient(FakeStreamClient):
        subtype = "error_max_turns"

    core, pool, _, _ = _core(store, client_cls=MaxTurnsClient, health=health)
    sess = store.sessions.create(owner_user_id=111, project_slug="office")
    result = await core.ask(sess.id, "привет")
    assert result.outcome.kind == "max_turns"
    assert health.read()["status"] == "ok"  # max_turns означает живой токен → health ok
    await pool.close_all()


async def test_auth_alert_deduplicated(store, tmp_path):
    """Флуд: два auth-сообщения подряд дают ОДИН алерт (только на переходе ok→degraded)."""
    health = HealthMarker(str(tmp_path / "health.json"))
    alerts = []
    core, pool, _, _ = _core(
        store, client_cls=AuthFailClient, health=health, on_alert=alerts.append
    )
    sess = store.sessions.create(owner_user_id=111, project_slug="office")
    await core.ask(sess.id, "раз")
    await core.ask(sess.id, "два")
    assert len(alerts) == 1  # второй auth не переход — алерт не повторяется
    await pool.close_all()


# ── B1: после ЛЮБОГО ошибочного финала CLI мёртв — клиента вон ────────────────


class ErrorResultClient(FakeStreamClient):
    """Финал с is_error=True: SDK в этот момент уже завершил CLI ненулевым кодом."""

    subtype = "error_max_turns"

    async def receive_response(self):
        yield FakeMessage("успел написать", self._reply_session)
        yield FakeResult(self._reply_session, subtype=self.subtype, is_error=True)


async def test_error_result_evicts_dead_client(store):
    """max_turns: человеку обещают «напиши продолжи» — значит следующий заход обязан
    получить ЖИВОГО клиента, а не съеденное впустую сообщение."""
    core, pool, _, _ = _core(store, client_cls=ErrorResultClient)
    sess = store.sessions.create(owner_user_id=111, project_slug="office")
    result = await core.ask(sess.id, "привет")
    assert result.outcome.kind == "max_turns"
    assert pool.get_live(sess.id) is None
    await pool.close_all()


async def test_error_result_still_saves_claude_session_id(store):
    """Эвикция не должна стоить контекста: id сессии CLI сохраняется (resume поднимет её)."""
    core, pool, _, _ = _core(store, client_cls=ErrorResultClient)
    sess = store.sessions.create(owner_user_id=111, project_slug="office")
    await core.ask(sess.id, "привет")
    assert store.sessions.get(sess.id).claude_session_id == "claude-generated-id"
    await pool.close_all()


class OtherErrorClient(ErrorResultClient):
    subtype = "success"  # is_error=True при subtype success — так SDK отдаёт сбой API


async def test_other_error_result_evicts_client(store):
    core, pool, _, _ = _core(store, client_cls=OtherErrorClient)
    sess = store.sessions.create(owner_user_id=111, project_slug="office")
    result = await core.ask(sess.id, "привет")
    assert result.outcome.kind == "other_error"
    assert pool.get_live(sess.id) is None
    await pool.close_all()


async def test_ok_result_keeps_warm_client(store):
    """Обратная сторона замка: штатный ответ клиента НЕ выселяет (иначе теряем весь смысл пула)."""
    core, pool, _, created = _core(store)
    sess = store.sessions.create(owner_user_id=111, project_slug="office")
    await core.ask(sess.id, "раз")
    await core.ask(sess.id, "два")
    assert pool.get_live(sess.id) is not None
    assert len(created) == 1
    await pool.close_all()
