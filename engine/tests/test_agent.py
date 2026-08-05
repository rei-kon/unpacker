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


# ── B4: лимит/перегруз — один повтор, но С паузой ────────────────────────────


class _Sleeper:
    """Дублёр asyncio.sleep: запоминает паузы, чтобы тест не ждал их по-настоящему."""

    def __init__(self):
        self.slept = []

    async def __call__(self, seconds):
        self.slept.append(seconds)


class RateLimitedThenOk(FakeStreamClient):
    _created = 0

    def __init__(self, options):
        super().__init__(options)
        type(self)._created += 1
        self._first = type(self)._created == 1

    async def receive_response(self):
        if self._first:
            yield FakeResult(self._reply_session, is_error=True, api_error_status=429)
            return
        yield FakeMessage("ответ агента", self._reply_session)
        yield FakeResult(self._reply_session)


async def test_rate_limited_retried_once_after_pause(store):
    """Мгновенный повтор влетает в тот же лимит — ретрай обязан быть с паузой."""
    RateLimitedThenOk._created = 0
    sleeper = _Sleeper()
    core, pool, _, created = _core(
        store, client_cls=RateLimitedThenOk, sleep=sleeper, retry_backoff=2.5
    )
    sess = store.sessions.create(owner_user_id=111, project_slug="office")
    result = await core.ask(sess.id, "привет")
    assert result.outcome.kind == "ok"
    assert sleeper.slept == [2.5]
    assert len(created) == 2
    await pool.close_all()


class AlwaysRateLimited(FakeStreamClient):
    async def receive_response(self):
        yield FakeResult(self._reply_session, is_error=True, api_error_status=429)


async def test_rate_limited_gives_up_honestly(store):
    sleeper = _Sleeper()
    core, pool, _, created = _core(
        store, client_cls=AlwaysRateLimited, sleep=sleeper, retry_backoff=1.0
    )
    sess = store.sessions.create(owner_user_id=111, project_slug="office")
    result = await core.ask(sess.id, "привет")
    assert result.outcome.kind == "rate_limited"
    assert len(created) == 2  # ровно один повтор, не бесконечность
    assert len(sleeper.slept) == 1
    await pool.close_all()


class OverloadedThenOk(RateLimitedThenOk):
    _created = 0

    async def receive_response(self):
        if self._first:
            yield FakeResult(self._reply_session, is_error=True, api_error_status=529)
            return
        yield FakeMessage("ответ агента", self._reply_session)
        yield FakeResult(self._reply_session)


async def test_overloaded_retried_after_pause(store):
    OverloadedThenOk._created = 0
    sleeper = _Sleeper()
    core, pool, _, _ = _core(store, client_cls=OverloadedThenOk, sleep=sleeper, retry_backoff=3.0)
    sess = store.sessions.create(owner_user_id=111, project_slug="office")
    result = await core.ask(sess.id, "привет")
    assert result.outcome.kind == "ok"
    assert sleeper.slept == [3.0]
    await pool.close_all()


class RateLimitExceptionThenOk(RateLimitedThenOk):
    """Живой баг SDK #812: 429 прилетает исключением и роняет клиента."""

    _created = 0

    async def receive_response(self):
        if self._first:
            raise RuntimeError("API Error: rate_limit_error")
        yield FakeMessage("ответ агента", self._reply_session)
        yield FakeResult(self._reply_session)


async def test_rate_limit_exception_also_retried(store):
    RateLimitExceptionThenOk._created = 0
    sleeper = _Sleeper()
    core, pool, _, created = _core(
        store, client_cls=RateLimitExceptionThenOk, sleep=sleeper, retry_backoff=2.0
    )
    sess = store.sessions.create(owner_user_id=111, project_slug="office")
    result = await core.ask(sess.id, "привет")
    assert result.outcome.kind == "ok"
    assert sleeper.slept == [2.0]
    assert len(created) == 2
    await pool.close_all()


class OverloadExceptionThenOk(RateLimitedThenOk):
    """Тот же баг SDK, но с перегрузом: 529 приезжает исключением, а не результатом."""

    _created = 0

    async def receive_response(self):
        if self._first:
            raise RuntimeError("API Error: overloaded_error")
        yield FakeMessage("ответ агента", self._reply_session)
        yield FakeResult(self._reply_session)


@pytest.mark.parametrize("client_cls", [RateLimitExceptionThenOk, OverloadExceptionThenOk])
async def test_provider_failure_before_first_event_keeps_resume(store, client_cls):
    """Перегруз провайдера ИСКЛЮЧЕНИЕМ до первого события неотличим по форме от «не
    поднялась прошлая сессия»: событий нет, поток мёртв. Но цена ошибки разная — сброс
    resume стоит человеку всей истории диалога за чужой лимит, да ещё и мгновенный повтор
    влетает в тот же лимит. Значит: пауза и повтор С ТЕМ ЖЕ resume."""
    client_cls._created = 0
    sleeper = _Sleeper()
    core, pool, built, created = _core(
        store, client_cls=client_cls, sleep=sleeper, retry_backoff=2.0
    )
    sess = store.sessions.create(owner_user_id=111, project_slug="office")
    store.sessions.set_claude_session_id(sess.id, "old-id")

    result = await core.ask(sess.id, "привет")

    assert result.outcome.kind == "ok"
    assert built["resume"] == "old-id"  # опции с resume=None не пересобирались
    assert sleeper.slept == [2.0]  # повтор после паузы, а не мгновенный
    assert result.note == ""  # истории не теряли — и не врали, что потеряли
    assert len(created) == 2
    await pool.close_all()


async def test_response_timeout_keeps_resume(store):
    """Таймаут ответа — про «долго», а не про «сессия не поднялась». Событий он тоже не
    оставляет, но лечить его забвением истории нельзя: следующее сообщение должно уйти
    в тот же диалог."""
    core, pool, built, created = _core(store, client_cls=HangingClient, response_timeout=0.1)
    sess = store.sessions.create(owner_user_id=111, project_slug="office")
    store.sessions.set_claude_session_id(sess.id, "old-id")

    result = await core.ask(sess.id, "привет")

    assert result.outcome.kind == "other_error"
    assert built["resume"] == "old-id"  # опции с resume=None не пересобирались
    assert result.note == ""
    assert len(created) == 1  # ждать второй таймаут — минута молчания в чате впустую
    assert store.sessions.get(sess.id).claude_session_id == "old-id"
    await pool.close_all()


async def test_exec_error_retry_has_no_pause(store):
    """Сломанный мозг/MCP чинится пересозданием клиента — ждать тут незачем."""
    ExecErrorThenOk._created = 0
    sleeper = _Sleeper()
    core, pool, _, _ = _core(store, client_cls=ExecErrorThenOk, sleep=sleeper)
    sess = store.sessions.create(owner_user_id=111, project_slug="office")
    result = await core.ask(sess.id, "привет")
    assert result.outcome.kind == "ok"
    assert sleeper.slept == []
    await pool.close_all()


# ── FB2: подъём клиента (connect) — тоже ошибка SDK, а не повод бросить ───────


class ConnectFailsClient(FakeStreamClient):
    """Клиент не поднимается вовсе: CLI не стартовал, порт занят, диск кончился."""

    async def connect(self):
        raise RuntimeError("Command failed: не смог запустить CLI")


async def test_connect_failure_does_not_escape_ask(store):
    """Контракт «ask не бросает на ошибках SDK» держится и на подъёме клиента: иначе
    исключение летит сквозь фасад в адаптер, и окно чата залипает без ответа."""
    core, pool, _, _ = _core(store, client_cls=ConnectFailsClient)
    sess = store.sessions.create(owner_user_id=111, project_slug="office")

    result = await core.ask(sess.id, "привет")

    assert result.outcome.kind == "other_error"
    assert pool.get_live(sess.id) is None
    await pool.close_all()


class AuthFailsOnConnectClient(FakeStreamClient):
    """Протухший OAuth виден уже на connect — до единого сообщения в потоке."""

    async def connect(self):
        raise RuntimeError("authentication_failed: OAuth token expired")


async def test_auth_failure_on_connect_flags_health_and_alerts(store, tmp_path):
    """Токен протух — владелец обязан узнать, даже если развалилось на подъёме клиента."""
    health = HealthMarker(str(tmp_path / "health.json"))
    alerts = []
    core, pool, _, _ = _core(
        store, client_cls=AuthFailsOnConnectClient, health=health, on_alert=alerts.append
    )
    sess = store.sessions.create(owner_user_id=111, project_slug="office")

    result = await core.ask(sess.id, "привет")

    assert result.outcome.kind == "auth_error"
    assert health.read()["status"] == "degraded"
    assert len(alerts) == 1
    await pool.close_all()


# ── B2: не поднялась прошлая сессия — окно чата не умирает навсегда ───────────


class BrokenResumeThenOk(FakeStreamClient):
    """Первый клиент падает так, как падает CLI с мёртвым --resume, второй работает."""

    _created = 0

    def __init__(self, options):
        super().__init__(options)
        type(self)._created += 1
        self._first = type(self)._created == 1

    async def receive_response(self):
        if self._first:
            raise RuntimeError("No conversation found with session ID: old-id")
        yield FakeMessage("ответ агента", self._reply_session)
        yield FakeResult(self._reply_session)


async def test_broken_resume_retried_without_resume(store):
    BrokenResumeThenOk._created = 0
    core, pool, built, created = _core(store, client_cls=BrokenResumeThenOk)
    sess = store.sessions.create(owner_user_id=111, project_slug="office")
    store.sessions.set_claude_session_id(sess.id, "old-id")

    result = await core.ask(sess.id, "привет")

    assert result.outcome.kind == "ok"  # окно чата ожило, а не умерло навсегда
    assert built["resume"] is None  # повтор пошёл с чистого листа
    assert len(created) == 2
    assert result.note  # человеку сказали, что контекст потерян
    await pool.close_all()


async def test_broken_resume_rewrites_stored_session_id(store):
    """Битый id не должен остаться в базе — иначе следующее сообщение снова упрётся в него."""
    BrokenResumeThenOk._created = 0
    core, pool, _, _ = _core(store, client_cls=BrokenResumeThenOk)
    sess = store.sessions.create(owner_user_id=111, project_slug="office")
    store.sessions.set_claude_session_id(sess.id, "old-id")

    await core.ask(sess.id, "привет")

    assert store.sessions.get(sess.id).claude_session_id == "claude-generated-id"
    await pool.close_all()


class AlwaysBrokenResume(FakeStreamClient):
    """Оба захода падают так, как падает CLI с мёртвым `--resume`."""

    async def receive_response(self):
        raise RuntimeError("No conversation found with session ID: old-id")
        yield


async def test_double_failure_clears_dead_session_id(store):
    """Сказали «начинаю с чистого листа» — значит и в базе чисто.

    Иначе мёртвый id живёт дальше: каждое следующее сообщение сперва упирается в него,
    жжёт лишнюю попытку и заново «теряет контекст», который потерян давно.
    """
    core, pool, _, created = _core(store, client_cls=AlwaysBrokenResume)
    sess = store.sessions.create(owner_user_id=111, project_slug="office")
    store.sessions.set_claude_session_id(sess.id, "old-id")

    result = await core.ask(sess.id, "привет")

    assert result.note  # человеку сказали, что контекст потерян
    assert len(created) == 2  # второй заход был — уже без resume
    assert store.sessions.get(sess.id).claude_session_id is None
    await pool.close_all()


class ResumeErrorFinalThenOk(FakeStreamClient):
    """CLI не поднял прошлую сессию и сказал об этом ФИНАЛОМ, а не падением.

    Форма, которую легко проглядеть: клиент жив, поток штатно дошёл до результата — просто
    в результате написано «No conversation found». Ветка на этот случай в ядре есть, а
    красного на неё до сих пор не было ни одного.
    """

    _created = 0

    def __init__(self, options):
        super().__init__(options)
        type(self)._created += 1
        self._first = type(self)._created == 1

    async def receive_response(self):
        if self._first:
            result = FakeResult(
                self._reply_session, subtype="error_during_execution", is_error=True
            )
            result.errors = ["No conversation found with session ID: old-id"]
            yield result
            return
        yield FakeMessage("ответ агента", self._reply_session)
        yield FakeResult(self._reply_session)


async def test_resume_error_as_final_retried_without_resume(store):
    ResumeErrorFinalThenOk._created = 0
    core, pool, built, created = _core(store, client_cls=ResumeErrorFinalThenOk)
    sess = store.sessions.create(owner_user_id=111, project_slug="office")
    store.sessions.set_claude_session_id(sess.id, "old-id")

    result = await core.ask(sess.id, "привет")

    assert result.outcome.kind == "ok"  # окно чата ожило
    assert built["resume"] is None  # повтор пошёл с чистого листа
    assert result.note  # и человеку об этом сказали
    assert len(created) == 2
    await pool.close_all()


class AuthExceptionClient(FakeStreamClient):
    """Протухший токен роняет заход исключением, до единого события в потоке."""

    async def receive_response(self):
        raise RuntimeError("API Error: 401 unauthorized")
        yield


async def test_auth_failure_keeps_resume(store):
    """Протухший токен убивает заход так же рано, как мёртвый resume, — и по уликам они
    неразличимы. Списать auth на историю значит сжечь её из-за проблемы, которая к ней
    отношения не имеет: токен починят, а диалога уже не будет."""
    core, pool, built, created = _core(store, client_cls=AuthExceptionClient)
    sess = store.sessions.create(owner_user_id=111, project_slug="office")
    store.sessions.set_claude_session_id(sess.id, "old-id")

    result = await core.ask(sess.id, "привет")

    assert result.outcome.kind == "auth_error"
    assert built["resume"] == "old-id"  # опции с resume=None не пересобирались
    assert len(created) == 1  # и повтора не было: чинить нечего
    assert store.sessions.get(sess.id).claude_session_id == "old-id"
    await pool.close_all()


class SilentDeathThenOk(BrokenResumeThenOk):
    """Клиент умер, не сказав ни слова — типовой симптом непонятого resume."""

    _created = 0

    async def receive_response(self):
        if self._first:
            raise RuntimeError("Command failed with exit code 1")
        yield FakeMessage("ответ агента", self._reply_session)
        yield FakeResult(self._reply_session)


async def test_silent_failure_with_resume_retries_without_it(store):
    SilentDeathThenOk._created = 0
    core, pool, built, created = _core(store, client_cls=SilentDeathThenOk)
    sess = store.sessions.create(owner_user_id=111, project_slug="office")
    store.sessions.set_claude_session_id(sess.id, "old-id")

    result = await core.ask(sess.id, "привет")

    assert result.outcome.kind == "ok"
    assert built["resume"] is None
    assert len(created) == 2
    await pool.close_all()


class DiesAfterFirstWord(FakeStreamClient):
    """Сбой ПОСРЕДИ генерации: контекст жив, resume сбрасывать нельзя."""

    async def receive_response(self):
        yield FakeMessage("успел написать", self._reply_session)
        raise RuntimeError("Command failed with exit code 1")


async def test_midstream_failure_keeps_resume(store):
    core, pool, _, created = _core(store, client_cls=DiesAfterFirstWord)
    sess = store.sessions.create(owner_user_id=111, project_slug="office")
    store.sessions.set_claude_session_id(sess.id, "old-id")

    result = await core.ask(sess.id, "привет")

    assert result.outcome.kind == "other_error"
    assert len(created) == 1  # повтора нет: терять живой контекст на пустом месте нельзя
    assert store.sessions.get(sess.id).claude_session_id == "claude-generated-id"
    await pool.close_all()


async def test_failure_without_resume_is_not_retried(store):
    """Резюмировать нечего — второй заход бессмыслен, не жжём бюджет впустую."""
    core, pool, _, created = _core(store, client_cls=ExplodingClient)
    sess = store.sessions.create(owner_user_id=111, project_slug="office")
    result = await core.ask(sess.id, "привет")
    assert result.outcome.kind == "other_error"
    assert len(created) == 1
    await pool.close_all()


# ── FB4: «стоп» — это просьба человека, а не поломка ─────────────────────────


async def test_stop_before_first_event_keeps_history_and_does_not_restart(store):
    """`/stop` сразу после отправки: событий не было, поток мёртв — по форме неотличимо
    от не поднявшейся сессии. Разбираться «по форме» тут стоит дорого вдвойне: человек
    получает стёртую историю и заново запущенную задачу, которую только что попросил
    прекратить."""
    holder = {}

    class StoppedByUser(FakeStreamClient):
        async def receive_response(self):
            await holder["core"].interrupt(holder["sid"])
            return  # SDK рвёт поток на interrupt: Final не приедет
            yield

    core, pool, built, created = _core(store, client_cls=StoppedByUser)
    sess = store.sessions.create(owner_user_id=111, project_slug="office")
    store.sessions.set_claude_session_id(sess.id, "old-id")
    holder["core"], holder["sid"] = core, sess.id

    result = await core.ask(sess.id, "привет")

    assert result.outcome.kind == "stopped"
    assert len(created) == 1  # остановленную задачу не перезапускаем
    assert result.note == ""  # и историю не теряли
    assert store.sessions.get(sess.id).claude_session_id == "old-id"
    await pool.close_all()


async def test_stop_midway_is_not_a_failed_turn(store):
    """Прерванный SDK честно отдаёт ошибочный финал — и по нему движок обычно перезапускает
    заход. Здесь перезапуск означал бы «нажал стоп — получил задачу заново»."""
    holder = {}

    class StoppedMidAnswer(FakeStreamClient):
        async def receive_response(self):
            yield FakeMessage("успел написать", self._reply_session)
            await holder["core"].interrupt(holder["sid"])
            yield FakeResult(self._reply_session, subtype="error_during_execution", is_error=True)

    core, pool, _, created = _core(store, client_cls=StoppedMidAnswer)
    sess = store.sessions.create(owner_user_id=111, project_slug="office")
    holder["core"], holder["sid"] = core, sess.id

    result = await core.ask(sess.id, "привет")

    assert result.outcome.kind == "stopped"
    assert len(created) == 1  # exec_error обычно даёт повтор — но не по просьбе «стоп»
    assert "успел написать" in result.text  # написанное до стопа не прячем
    assert store.sessions.get(sess.id).claude_session_id == "claude-generated-id"
    await pool.close_all()


# ── B6: обрыв потока без Final не должен стирать контекст ────────────────────


class TruncatedClient(FakeStreamClient):
    """Поток кончился без ResultMessage — CLI умер после первых слов."""

    async def receive_response(self):
        yield FakeMessage("успел написать", self._reply_session)


async def test_truncated_stream_saves_session_id(store):
    core, pool, _, _ = _core(store, client_cls=TruncatedClient)
    sess = store.sessions.create(owner_user_id=111, project_slug="office")

    result = await core.ask(sess.id, "привет")

    assert result.outcome.kind == "other_error"  # исход честный
    assert pool.get_live(sess.id) is None  # клиент выкинут
    # сессия CLI на диске уже создана — её handle сохраняем, иначе диалог теряет кусок истории
    assert store.sessions.get(sess.id).claude_session_id == "claude-generated-id"
    await pool.close_all()


# ── B3: расход турна доезжает до базы ────────────────────────────────────────


class CostlyClient(FakeStreamClient):
    async def receive_response(self):
        yield FakeMessage("ответ агента", self._reply_session)
        result = FakeResult(self._reply_session)
        result.total_cost_usd = 0.017
        result.usage = {"input_tokens": 1200, "output_tokens": 340}
        result.num_turns = 3
        yield result


async def test_usage_written_after_ask(store):
    """Таблица usage была написана и мертва: ни одного боевого вызова add."""
    core, pool, _, _ = _core(store, client_cls=CostlyClient)
    sess = store.sessions.create(owner_user_id=111, project_slug="office")
    await core.ask(sess.id, "привет")
    assert abs(store.usage.session_cost(sess.id) - 0.017) < 1e-9
    await pool.close_all()


async def test_usage_accumulates_over_turns(store):
    core, pool, _, _ = _core(store, client_cls=CostlyClient)
    sess = store.sessions.create(owner_user_id=111, project_slug="office")
    await core.ask(sess.id, "раз")
    await core.ask(sess.id, "два")
    assert abs(store.usage.session_cost(sess.id) - 0.034) < 1e-9
    await pool.close_all()


class CostlyErrorClient(FakeStreamClient):
    async def receive_response(self):
        result = FakeResult(self._reply_session, subtype="error_max_turns", is_error=True)
        result.total_cost_usd = 0.009
        result.usage = {"input_tokens": 500, "output_tokens": 10}
        yield result


async def test_usage_written_even_on_error_outcome(store):
    """Токены на упавшем турне сгорели по-настоящему — не учитывать их значит врать себе."""
    core, pool, _, _ = _core(store, client_cls=CostlyErrorClient)
    sess = store.sessions.create(owner_user_id=111, project_slug="office")
    await core.ask(sess.id, "привет")
    assert abs(store.usage.session_cost(sess.id) - 0.009) < 1e-9
    await pool.close_all()


class CostlyExecErrorThenOk(FakeStreamClient):
    """Первый заход упал после дорогой работы инструментами, второй прошёл штатно."""

    _created = 0

    def __init__(self, options):
        super().__init__(options)
        type(self)._created += 1
        self._first = type(self)._created == 1

    async def receive_response(self):
        result = FakeResult(
            self._reply_session,
            subtype="error_during_execution" if self._first else "success",
            is_error=self._first,
        )
        result.total_cost_usd = 0.05 if self._first else 0.01
        result.usage = {"input_tokens": 900, "output_tokens": 40}
        yield result


async def test_usage_of_failed_attempt_is_not_lost(store):
    """Повтор после exec_error — это ВТОРОЙ оплаченный заход. Учитывать только последний
    значит занижать расход ровно на самых дорогих турнах: инструменты уже отработали."""
    CostlyExecErrorThenOk._created = 0
    core, pool, _, created = _core(store, client_cls=CostlyExecErrorThenOk)
    sess = store.sessions.create(owner_user_id=111, project_slug="office")

    await core.ask(sess.id, "привет")

    assert len(created) == 2  # повтор реально был
    assert abs(store.usage.session_cost(sess.id) - 0.06) < 1e-9
    await pool.close_all()


async def test_usage_write_failure_does_not_cost_the_answer(store):
    """Учёт расхода — служебная запись. Уронить из-за неё готовый ответ значит обменять
    строчку в статистике на работу, за которую человек уже заплатил."""
    core, pool, _, _ = _core(store, client_cls=CostlyClient)
    sess = store.sessions.create(owner_user_id=111, project_slug="office")

    def boom(**kwargs):
        raise RuntimeError("база расхода недоступна")

    store.usage.add = boom

    result = await core.ask(sess.id, "привет")

    assert result.outcome.kind == "ok"
    assert "ответ агента" in result.text
    await pool.close_all()


async def test_final_without_cost_writes_nothing(store):
    """Финал без полей расхода не должен плодить пустые строки в usage."""

    class NoCostClient(FakeStreamClient):
        async def receive_response(self):
            result = FakeResult(self._reply_session)
            del result.total_cost_usd
            yield result

    core, pool, _, _ = _core(store, client_cls=NoCostClient)
    sess = store.sessions.create(owner_user_id=111, project_slug="office")
    await core.ask(sess.id, "привет")
    assert store.usage.session_cost(sess.id) == 0.0
    await pool.close_all()


# ── B5: /model на тёплом клиенте ─────────────────────────────────────────────


async def test_model_change_rebuilds_warm_client(store):
    """`/model` отвечает «применится к следующему сообщению» — а тёплый клиент собран под
    старую модель, и `_acquire` игнорирует новые опции. Обещание было ложным."""
    core, pool, built, created = _core(store)
    sess = store.sessions.create(owner_user_id=111, project_slug="office")
    await core.ask(sess.id, "раз")
    assert built["model"] == "sonnet"

    store.sessions.set_model(sess.id, "haiku")
    await core.ask(sess.id, "два")

    assert built["model"] == "haiku"
    assert len(created) == 2  # старый клиент выселен, новый собран с новой моделью
    assert created[1].options["model"] == "haiku"
    await pool.close_all()


async def test_same_model_keeps_warm_client(store):
    """Обратная сторона: без смены модели пул остаётся тёплым (иначе теряем 12с на сообщение)."""
    core, pool, _, created = _core(store)
    sess = store.sessions.create(owner_user_id=111, project_slug="office")
    await core.ask(sess.id, "раз")
    await core.ask(sess.id, "два")
    assert len(created) == 1
    await pool.close_all()


async def test_resume_change_does_not_rebuild_client(store):
    """resume меняется после КАЖДОГО первого ответа — реагировать на это пересозданием
    значило бы убить тёплый пул целиком. Живому клиенту resume уже не нужен."""
    core, pool, _, created = _core(store)
    sess = store.sessions.create(owner_user_id=111, project_slug="office")
    await core.ask(sess.id, "раз")
    assert store.sessions.get(sess.id).claude_session_id  # resume появился
    await core.ask(sess.id, "два")
    assert len(created) == 1
    await pool.close_all()
