"""Событийный разбор потока §5.3 — verbose 1."""

from engine.core.errors import Outcome
from engine.core.events import Final, ToolStarted, stream_events


class Text:
    def __init__(self, text):
        self.text = text


class ToolUseBlock:  # имя класса важно: stream_events узнаёт tool_use по type(...).__name__
    def __init__(self, name):
        self.name = name


class Msg:
    def __init__(self, blocks, session_id=None):
        self.content = blocks
        self.session_id = session_id


class Result:
    def __init__(self, session_id, subtype="success", is_error=False, api_error_status=None):
        self.session_id = session_id
        self.total_cost_usd = 0.001
        self.subtype = subtype
        self.is_error = is_error
        self.api_error_status = api_error_status
        self.content = []


async def _collect(stream):
    return [ev async for ev in stream]


async def test_emits_tool_started_then_final():
    async def gen():
        yield Msg([ToolUseBlock("Read")], session_id="s1")
        yield Msg([Text("готово")], session_id="s1")
        yield Result("s1")

    events = await _collect(stream_events(gen()))
    assert any(isinstance(e, ToolStarted) and e.name == "Read" for e in events)
    final = events[-1]
    assert isinstance(final, Final)
    assert final.text == "готово"
    assert final.session_id == "s1"
    assert final.outcome.kind == "ok"


async def test_final_carries_outcome_max_turns():
    async def gen():
        yield Msg([Text("частично")], session_id="s2")
        yield Result("s2", subtype="error_max_turns", is_error=True)

    events = await _collect(stream_events(gen()))
    final = events[-1]
    assert isinstance(final, Final)
    assert final.outcome.kind == "max_turns"


async def test_no_tool_no_toolstarted():
    async def gen():
        yield Msg([Text("просто ответ")], session_id="s3")
        yield Result("s3")

    events = await _collect(stream_events(gen()))
    assert not any(isinstance(e, ToolStarted) for e in events)
    assert isinstance(events[-1], Final)


async def test_final_outcome_is_outcome_type():
    async def gen():
        yield Result("s4")

    events = await _collect(stream_events(gen()))
    assert isinstance(events[-1].outcome, Outcome)
