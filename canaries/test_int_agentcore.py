"""Интеграционный смоук Среза B — AgentCore на ЖИВОМ SDK.

Юнит-тесты (engine/tests/test_pool.py, test_agent.py) доказали логику пула на фейке. Здесь —
что пул реально работает с настоящим ClaudeSDKClient: тёплое переиспользование, resume после
evict (несущий сценарий §5.1: клиента вытеснили → следующее сообщение подняло сессию из
claude_session_id и помнит контекст), interrupt мимо session-lock.

Гоняется на VPS под canary (make canary). На маке скипается.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from conftest import DANGEROUS_TOOLS, requires_live_sdk, write_results_note

from engine.core.agent import AgentCore
from engine.core.pool import ClientPool
from engine.core.store import Store


def _options_builder(*, cwd, resume, model):
    kwargs = {
        "cwd": cwd,
        "permission_mode": "bypassPermissions",
        "setting_sources": ["user", "project"],
        "disallowed_tools": list(DANGEROUS_TOOLS),  # безопасность живого VPS (как канарейки)
        "max_turns": 6,
    }
    if resume:
        kwargs["resume"] = resume
    if model:
        kwargs["model"] = model
    return ClaudeAgentOptions(**kwargs)


def _make(store: Store) -> tuple[AgentCore, ClientPool]:
    pool = ClientPool(
        factory=lambda options: ClaudeSDKClient(options=options),
        ceiling=3,
        idle_timeout=1800,
    )
    core = AgentCore(store=store, pool=pool, options_builder=_options_builder, response_timeout=180)
    return core, pool


@pytest.fixture
def store(tmp_path: Path, brain: Path) -> Store:
    s = Store(str(tmp_path / "state.db"))
    s.projects.create(slug="office", name="Офис", brain_path=str(brain), default_model=None)
    return s


@requires_live_sdk
async def test_resume_after_evict_remembers(store: Store) -> None:
    """Несущий сценарий §5.1: evict → следующий ask поднимает сессию через resume и помнит."""
    core, pool = _make(store)
    secret = f"ОРЕХ{uuid.uuid4().hex[:6].upper()}"
    sess = store.sessions.create(owner_user_id=111, project_slug="office")

    try:
        await core.ask(sess.id, f"Запомни слово {secret}. Ответь ровно: OK")
        cid1 = store.sessions.get(sess.id).claude_session_id
        assert cid1, "claude_session_id не записан после первого ответа"

        # Имитация idle-evict: клиента выкинули, сессия осталась в SQLite
        await pool.evict(sess.id)
        assert pool.get_live(sess.id) is None

        # Следующий ask поднимает НОВЫЙ клиент с resume=claude_session_id
        answer = await core.ask(sess.id, "Какое слово я просил запомнить? Одним словом.")
        write_results_note(
            "INT-B-resume",
            f"claude_session_id={cid1}; ответ после evict+resume={answer!r}",
        )
        assert secret in answer.upper(), (
            f"resume после evict не восстановил контекст — §5.1 сломан. Ответ: {answer!r}"
        )
    finally:
        await pool.close_all()


@requires_live_sdk
async def test_warm_reuse_no_reconnect(store: Store) -> None:
    """Два ask по одной сессии — тот же тёплый клиент (без пере-connect)."""
    core, pool = _make(store)
    sess = store.sessions.create(owner_user_id=111, project_slug="office")
    try:
        await core.ask(sess.id, "Скажи ровно одно слово: раз")
        live1 = pool.get_live(sess.id)
        await core.ask(sess.id, "Скажи ровно одно слово: два")
        live2 = pool.get_live(sess.id)
        write_results_note("INT-B-warm", f"тёплый клиент переиспользован: {live1 is live2}")
        assert live1 is live2, "клиент пересоздан вместо тёплого переиспользования"
    finally:
        await pool.close_all()


@requires_live_sdk
async def test_interrupt_via_core_bypasses_lock(store: Store) -> None:
    """interrupt через AgentCore идёт мимо session-lock и не убивает клиента (§5.1, C7)."""
    core, pool = _make(store)
    sess = store.sessions.create(owner_user_id=111, project_slug="office")
    try:
        # Прогрев — поднять клиента в пул
        await core.ask(sess.id, "Скажи ровно одно слово: привет")

        # Долгая генерация в фоне
        gen = asyncio.create_task(
            core.ask(sess.id, "Считай от 1 до 500, по числу на строку, только числа.")
        )
        await asyncio.sleep(6)
        # interrupt МИМО session-lock (который держит gen) — по живой ссылке
        await asyncio.wait_for(core.interrupt(sess.id), timeout=5)
        try:
            await asyncio.wait_for(gen, timeout=60)
        except Exception:  # noqa: BLE001 — прерванная генерация может завершиться исключением
            pass

        # Клиент жив: следующий ask обслужен
        answer = await core.ask(sess.id, "Скажи ровно: ЖИВ")
        write_results_note("INT-B-interrupt", f"ответ после interrupt={answer!r}")
        assert "ЖИВ" in answer.upper(), f"клиент не пережил interrupt: {answer!r}"
    finally:
        await pool.close_all()
