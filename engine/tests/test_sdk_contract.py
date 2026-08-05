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


async def test_stream_events_reads_real_stream_event():
    """Живой контракт партиалов: настоящий StreamEvent пина 0.2.121 через разбор ядра.

    Все остальные тесты стриминга гоняются по нашему дублёру StreamEvent — они докажут лишь
    согласованность с нашим представлением. Здесь — с самим SDK: сменится форма события,
    и черновик «печатает…» замолчит у ученика на VPS, а не в CI.
    """
    from claude_agent_sdk import StreamEvent

    from engine.core.events import Final, TextDelta, TextStart, stream_events

    async def stream():
        yield StreamEvent(uuid="u1", session_id="sess-7", event={"type": "message_start"})
        yield StreamEvent(
            uuid="u2",
            session_id="sess-7",
            event={
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "при"},
            },
        )
        yield AssistantMessage(content=[TextBlock("привет")], model="m")
        yield _result(session_id="sess-7")

    events = [ev async for ev in stream_events(stream())]
    assert isinstance(events[0], TextStart)
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["при"]
    final = events[-1]
    assert isinstance(final, Final)
    assert final.text == "привет"
    assert final.session_id == "sess-7"


async def test_final_reads_cost_from_real_result_message():
    """Поля расхода читаются с НАСТОЯЩЕГО ResultMessage, а не только с нашего дублёра."""
    from claude_agent_sdk import ResultMessage

    from engine.core.events import stream_events

    async def stream():
        yield ResultMessage(
            subtype="success",
            duration_ms=10,
            duration_api_ms=8,
            is_error=False,
            num_turns=2,
            session_id="sess-8",
            total_cost_usd=0.017,
            usage={"input_tokens": 11, "output_tokens": 22},
        )

    final = [ev async for ev in stream_events(stream())][-1]
    assert final.total_cost_usd == 0.017
    assert final.num_turns == 2
    assert final.usage == {"input_tokens": 11, "output_tokens": 22}
