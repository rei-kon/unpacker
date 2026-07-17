"""C2 — переживает ли resume рестарт, и что делает чужой cwd.

A. Опора (жёсткий ассерт): resume с ТЕМ ЖЕ cwd продолжает историю. Несущая опора §5.2.
B. Разведка (факт): resume с ЧУЖИМ cwd (issue #555). На баге не строим — ассерта нет.

Две ловушки первой редакции, закрыты здесь:
  • Секрет-константа «ОРЕХ» могла осесть в user-памяти и красить C2a зелёным вечно, а
    заодно отравлять соседние канарейки. Теперь секрет уникален на прогон.
  • Агент с bypassPermissions мог достать секрет не из истории, а из ~/.claude/projects/*.jsonl
    или из записанной памяти. Теперь файловые тулы отрезаны (canary_options) — ответ на
    «какое слово?» может прийти ТОЛЬКО из восстановленного контекста сессии.

Про тождество session_id: план хранит один id на сессию (§5.2). Совпал он после resume
или SDK выдал форк с копией истории — это параметр того, как SessionStore обновляет id,
а НЕ допущение «resume работает». Поэтому id фиксируется фактом (важным для 1a), но
жёсткий ассерт — на память, а не на равенство id.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from claude_agent_sdk import ClaudeSDKClient
from conftest import canary_options, collect_stream, requires_live_sdk, write_results_note


@requires_live_sdk
async def test_c2a_resume_same_cwd_continues_history(brain: Path) -> None:
    secret = f"ОРЕХ{uuid.uuid4().hex[:6].upper()}"

    async with ClaudeSDKClient(options=canary_options(brain)) as client:
        await client.query(f"Запомни слово {secret}. Ответь ровно: OK")
        first = await collect_stream(client.receive_response())

    assert first.session_id, "SDK не вернул session_id — resume строить не на чем"

    # Новый клиент с тем же resume-id и cwd = имитация рестарта процесса.
    resumed_opts = canary_options(brain, resume=first.session_id)
    async with ClaudeSDKClient(options=resumed_opts) as client:
        await client.query("Какое слово я просил запомнить? Ответь одним словом.")
        second = await collect_stream(client.receive_response())

    same_id = first.session_id == second.session_id
    id_note = (
        "id стабилен — SessionStore хранит один id"
        if same_id
        else "ВНИМАНИЕ: resume форкает id — §5.2/1a: SessionStore обязан обновлять "
        "claude_session_id из каждого ResultMessage, иначе 2-й рестарт теряет историю"
    )
    write_results_note(
        "C2a",
        f"секрет={secret}; session_id до={first.session_id} после={second.session_id} "
        f"(совпал={same_id}); файловые тулы отрезаны; ответ={second.full_text!r}. {id_note}",
    )
    assert secret in second.full_text.upper(), (
        "resume с тем же cwd НЕ восстановил историю (файловый путь к секрету отрезан) — "
        f"несущее допущение §5.2 ложно. Ответ: {second.full_text!r}"
    )


@requires_live_sdk
async def test_c2b_resume_foreign_cwd_behaviour(brain: Path, tmp_path: Path) -> None:
    """Разведка: тот же session_id, но чужой cwd. Фиксируем факт, не судим (issue #555)."""
    secret = f"ОРЕХ{uuid.uuid4().hex[:6].upper()}"

    async with ClaudeSDKClient(options=canary_options(brain)) as client:
        await client.query(f"Запомни слово {secret}. Ответь ровно: OK")
        first = await collect_stream(client.receive_response())

    assert first.session_id, "SDK не вернул session_id"

    foreign = tmp_path / "foreign-brain"
    foreign.mkdir()
    (foreign / "CLAUDE.md").write_text("# Чужой мозг\n\nОтвечай коротко.\n", encoding="utf-8")

    try:
        opts = canary_options(foreign, resume=first.session_id)
        async with ClaudeSDKClient(options=opts) as client:
            await client.query("Какое слово я просил запомнить? Ответь одним словом.")
            second = await collect_stream(client.receive_response())
        remembered = secret in second.full_text.upper()
        verdict = (
            "история сохранилась (issue #555 не воспроизвёлся)"
            if remembered
            else "история потеряна — SDK молча начал новую сессию "
            "(issue #555 жив, cwd брать из brain_path)"
        )
        write_results_note(
            "C2b",
            f"{verdict}; session_id было={first.session_id} стало={second.session_id}; "
            f"ответ={second.full_text!r}",
        )
    except Exception as exc:  # noqa: BLE001 — падение это тоже факт, и хороший (громкий отказ)
        write_results_note("C2b", f"resume с чужим cwd упал с ошибкой (громкий отказ): {exc!r}")
