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


# ── Псевдо-стриминг (Велс-трюк): TextStart/TextDelta из StreamEvent ──────────


class StreamEvent:  # узнаётся duck-typing'ом: по атрибуту .event с dict-значением (не по имени)
    def __init__(self, event, session_id="s1", parent_tool_use_id=None):
        self.event = event
        self.session_id = session_id
        self.parent_tool_use_id = parent_tool_use_id


def _delta(text):
    return {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}}


async def test_emits_text_start_and_deltas():
    from engine.core.events import TextDelta, TextStart

    async def gen():
        yield StreamEvent({"type": "message_start"})
        yield StreamEvent(_delta("При"))
        yield StreamEvent(_delta("вет"))
        yield Msg([Text("Привет")], session_id="s1")
        yield Result("s1")

    events = await _collect(stream_events(gen()))
    assert isinstance(events[0], TextStart)
    deltas = [e.text for e in events if isinstance(e, TextDelta)]
    assert deltas == ["При", "вет"]
    final = events[-1]
    assert isinstance(final, Final)
    assert final.text == "Привет"  # финал по-прежнему из целого сообщения


async def test_subagent_deltas_are_ignored():
    from engine.core.events import TextDelta

    async def gen():
        yield StreamEvent(_delta("субагентная болтовня"), parent_tool_use_id="tu_1")
        yield Msg([Text("ответ")], session_id="s1")
        yield Result("s1")

    events = await _collect(stream_events(gen()))
    assert not any(isinstance(e, TextDelta) for e in events)


async def test_non_text_deltas_are_ignored():
    from engine.core.events import TextDelta

    async def gen():
        json_delta = {"type": "input_json_delta", "partial_json": "{"}
        yield StreamEvent({"type": "content_block_delta", "delta": json_delta})
        yield StreamEvent({"type": "content_block_stop"})
        yield Result("s1")

    events = await _collect(stream_events(gen()))
    assert not any(isinstance(e, TextDelta) for e in events)
