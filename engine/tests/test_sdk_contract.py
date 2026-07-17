"""Smoke: duck-typing ядра против РЕАЛЬНЫХ типов claude-agent-sdk 0.2.121.

Зачем отдельно от остальных тестов. Ядро (`streaming.py`) читает объекты SDK по
duck-typing и потому НЕ импортирует SDK — переносимо и устойчиво к версиям, но есть
оборотная сторона: 41 тест ядра гоняется по нашим ЖЕ фейкам и доказывает лишь «код
согласован с нашим представлением о SDK», а не «согласован с SDK 0.2.121». Если SDK
переименует поле или сменит структуру блоков, фейки этого не заметят, а бот у ученика
начнёт отвечать плейсхолдером «…».

Этот тест закрывает дыру дёшево: конструирует НАСТОЯЩИЕ TextBlock/AssistantMessage/
ResultMessage из пина и прогоняет их через предикаты ядра. Без сети и токена — гоняется
в CI. Красный здесь = пин 0.2.121 несовместим с нашим duck-typing, ловится до VPS.
"""

from __future__ import annotations

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from engine.core.streaming import collect_response_with_session, extract_text, is_result


def _result(session_id: str = "sess-1") -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id=session_id,
        total_cost_usd=0.0,
    )


def test_extract_text_reads_real_assistant_message():
    msg = AssistantMessage(content=[TextBlock("привет")], model="claude-sonnet")
    assert extract_text(msg) == "привет"


def test_is_result_recognises_real_result_message():
    assert is_result(_result()) is True
    assert is_result(AssistantMessage(content=[TextBlock("x")], model="m")) is False


async def test_collect_reads_real_stream():
    """Полный путь ядра по реальным объектам SDK: последний текст + session_id из ResultMessage."""

    async def stream():
        yield AssistantMessage(content=[TextBlock("финальный ответ")], model="m")
        yield _result(session_id="sess-42")

    text, session_id = await collect_response_with_session(stream())
    assert text == "финальный ответ"
    assert session_id == "sess-42"
