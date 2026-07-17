"""C2 — переживает ли resume рестарт, и что делает чужой cwd.

Две разные вещи, и их важно не путать:

A. **На что мы опираемся** (жёсткий ассерт): resume с ТЕМ ЖЕ cwd продолжает историю.
   Это несущая опора §5.2 — «бот переживает рестарт». Ложно → вся модель сессий не работает.

B. **Что мы просто выясняем** (только фиксация факта): что происходит при resume с ЧУЖИМ cwd.
   Issue #555 говорит: SDK молча создаёт новую сессию. Мы на этом баге не строим — §5.2 жёстко
   берёт cwd из projects.brain_path. Поэтому ассерта тут нет: если баг починили, это хорошая
   новость, а не красная канарейка. Нам нужен записанный факт, а не приговор.
"""
from __future__ import annotations

from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from conftest import requires_live_sdk, write_results_note

from engine.core.streaming import collect_response_with_session

SECRET = "ОРЕХ"


def _options(cwd: Path, resume: str | None = None) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        cwd=str(cwd),
        permission_mode="bypassPermissions",
        setting_sources=["user", "project"],
        resume=resume,
        max_turns=5,
    )


@requires_live_sdk
async def test_c2a_resume_same_cwd_continues_history(brain: Path) -> None:
    """A. Опора: тот же cwd → агент помнит сказанное до «рестарта»."""
    async with ClaudeSDKClient(options=_options(brain)) as client:
        await client.query(f"Запомни слово {SECRET}. Ответь ровно: OK")
        _, first_session = await collect_response_with_session(client.receive_response())

    assert first_session, "SDK не вернул session_id — resume строить не на чем"

    # Новый клиент = имитация рестарта процесса: сессия должна подняться из id
    async with ClaudeSDKClient(options=_options(brain, resume=first_session)) as client:
        await client.query("Какое слово я просил запомнить? Ответь одним словом.")
        text, second_session = await collect_response_with_session(client.receive_response())

    write_results_note(
        "C2a",
        f"session_id до={first_session!r} после resume={second_session!r} "
        f"(совпали: {first_session == second_session}); ответ: {text!r}",
    )
    assert SECRET in text.upper(), (
        "resume с тем же cwd НЕ продолжил историю — несущее допущение §5.2 ложно. "
        f"Ответ: {text!r}"
    )


@requires_live_sdk
async def test_c2b_resume_foreign_cwd_behaviour(brain: Path, tmp_path: Path) -> None:
    """B. Разведка: тот же session_id, но чужой cwd. Фиксируем факт, не судим."""
    async with ClaudeSDKClient(options=_options(brain)) as client:
        await client.query(f"Запомни слово {SECRET}. Ответь ровно: OK")
        _, session = await collect_response_with_session(client.receive_response())

    assert session, "SDK не вернул session_id"

    foreign = tmp_path / "foreign-brain"
    foreign.mkdir()
    (foreign / "CLAUDE.md").write_text("# Чужой мозг\n\nОтвечай коротко.\n", encoding="utf-8")

    remembered: bool
    try:
        async with ClaudeSDKClient(options=_options(foreign, resume=session)) as client:
            await client.query("Какое слово я просил запомнить? Ответь одним словом.")
            text, foreign_session = await collect_response_with_session(client.receive_response())
        remembered = SECRET in text.upper()
        verdict = (
            "история сохранилась (issue #555 не воспроизвёлся)"
            if remembered
            else "история потеряна — SDK молча начал новую сессию (issue #555 жив)"
        )
        write_results_note(
            "C2b",
            f"{verdict}; session_id: было={session!r} стало={foreign_session!r}; ответ: {text!r}",
        )
    except Exception as exc:  # noqa: BLE001 — падение это тоже факт, и хороший (громкий отказ)
        write_results_note("C2b", f"resume с чужим cwd упал с ошибкой (громкий отказ): {exc!r}")
