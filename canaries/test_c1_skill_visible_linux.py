"""C1 — виден и вызываем ли скилл из папки-мозга на Linux.

Допущение плана (§4, риск #268): скиллы из `.claude/skills/` папки-мозга подхватываются
на Linux. Половина ценности мозга — в скиллах; тихий отказ здесь = «пустой» агент у
ученика без единой ошибки в логах.

Провенанс — сердце этой канарейки. Первая редакция искала маркер в ответе, но агент с
полным тулингом мог просто прочитать SKILL.md через Read и повторить строку — зелёный
цвет обманывал. Теперь:
  • файловые тулы отрезаны (canary_options) — прочитать SKILL.md физически нечем;
  • маркер не лежит в файле готовой строкой — его надо ВЫЧИСЛИТЬ по инструкции скилла,
    а инструкция видна только после загрузки скилла тулом Skill.
Значит появление CANARY-PONG-7F3A доказуемо означает: скилл загружен как скилл.

Два прогона, потому что §4 утверждает КАУЗАЛЬНОСТЬ («setting_sources — обязательная
настройка, иначе не подхватятся»), а SDK 0.2.121 добавил отдельную опцию `skills`:
  • C1a (опора): штатный способ SDK `skills=[...]` — работает ли механизм вообще;
  • C1b (разведка): только `setting_sources` без `skills=` — верен ли механизм плана §4.
Если C1b красная, а C1a зелёная — §4 п.3 неточен: нужен `skills=`, и это факт Фазы 0.
"""

from __future__ import annotations

from pathlib import Path

from claude_agent_sdk import ClaudeSDKClient
from conftest import (
    canary_options,
    collect_stream,
    requires_linux,
    requires_live_sdk,
    write_results_note,
)

PONG = "CANARY-PONG-7F3A"


@requires_live_sdk
@requires_linux
async def test_c1a_skill_via_skills_option(brain_with_skill: Path) -> None:
    """Опора: штатный способ включения скиллов SDK (`skills=[name]`) реально грузит скилл мозга."""
    options = canary_options(brain_with_skill, skills=["canary-ping"])
    async with ClaudeSDKClient(options=options) as client:
        await client.query("Сделай ping.")
        stream = await collect_stream(client.receive_response())

    write_results_note(
        "C1a",
        f"skills=['canary-ping']; tool_uses={stream.tool_uses}; ответ={stream.full_text!r}",
    )
    assert PONG in stream.full_text, (
        "скилл мозга не сработал даже штатным skills=[...] при отрезанных файловых тулах — "
        f"допущение §4 (скиллы Linux) ложно. Ответ: {stream.full_text!r}, tools={stream.tool_uses}"
    )


@requires_live_sdk
@requires_linux
async def test_c1b_skill_via_setting_sources_only(brain_with_skill: Path) -> None:
    """Разведка: достаточно ли ОДНОГО setting_sources (механизм §4 п.3), без опции skills.

    Skill-тул даём вручную (allowed_tools) — иначе проверяли бы «нет Skill в allowed»,
    а не «setting_sources не обнаружил скилл». Вопрос строго: виден ли скилл мозга модели.
    """
    options = canary_options(
        brain_with_skill,
        setting_sources=["user", "project"],
        allowed_tools=["Skill"],
    )
    async with ClaudeSDKClient(options=options) as client:
        await client.query("Сделай ping.")
        stream = await collect_stream(client.receive_response())

    worked = PONG in stream.full_text
    write_results_note(
        "C1b",
        f"только setting_sources, без skills=: сработал={worked}; "
        f"tool_uses={stream.tool_uses}; ответ={stream.full_text!r}. "
        f"{'§4 п.3 точен' if worked else '§4 п.3 НЕТОЧЕН — нужна опция skills=, поправить план'}",
    )
