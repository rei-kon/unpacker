"""Распознавание речи: приём речевых типов, словарь, поведение на пустом и на сбое.

Сеть здесь не трогаем: HTTP-слой Deepgram подменяется, потому что проверяем НЕ провайдера,
а свои решения — что пустой результат становится ошибкой, что таймаут не висит вечно, что
имя файла получает правильное расширение и что пересланная запись помечается.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from engine.adapters.telegram.attach import SPEECH_KINDS, attachment_from_message
from engine.core.transcribe import (
    DeepgramTranscriber,
    TranscriptionError,
    frame_voice_prompt,
    load_keyterms,
)

# ── приём речевых типов из сообщения ─────────────────────────────────────────


def test_voice_gets_extension_from_mime() -> None:
    """У голосового нет имени вовсе — расширение выводим из MIME, иначе распознаватель
    получит `file.bin` и будет гадать по сигнатуре."""
    msg = SimpleNamespace(
        document=None,
        photo=None,
        voice=SimpleNamespace(
            file_id="v1", file_unique_id="uniq", mime_type="audio/ogg", file_size=1234
        ),
        audio=None,
        video_note=None,
    )
    att = attachment_from_message(msg)
    assert att is not None
    assert att.kind == "голосовое"
    assert att.file_name == "voice-uniq.ogg"
    assert att.size == 1234


def test_voice_without_mime_falls_back_to_ogg() -> None:
    msg = SimpleNamespace(
        document=None,
        photo=None,
        voice=SimpleNamespace(file_id="v1", file_unique_id=None, mime_type=None, file_size=None),
        audio=None,
        video_note=None,
    )
    att = attachment_from_message(msg)
    assert att is not None and att.file_name == "voice-tg.ogg"


def test_audio_keeps_its_own_name() -> None:
    """У музыкального файла имя обычно есть — не выдумываем своё, оно осмысленное."""
    msg = SimpleNamespace(
        document=None,
        photo=None,
        voice=None,
        audio=SimpleNamespace(
            file_id="a1",
            file_unique_id="u",
            mime_type="audio/mpeg",
            file_size=10,
            file_name="Запись – 2109.m4a",
        ),
        video_note=None,
    )
    att = attachment_from_message(msg)
    assert att is not None and att.file_name == "Запись – 2109.m4a" and att.kind == "аудио"


def test_video_note_wins_over_photo_preview() -> None:
    """У кружка есть превью-фото. Если хендлер фото перехватит его первым, агент получит
    картинку вместо слов человека — молча и необратимо."""
    msg = SimpleNamespace(
        document=None,
        photo=[SimpleNamespace(file_id="p", file_unique_id="pu", file_size=1)],
        voice=None,
        audio=None,
        video_note=SimpleNamespace(file_id="vn", file_unique_id="u", file_size=99),
    )
    att = attachment_from_message(msg)
    assert att is not None and att.kind == "видеозаметка" and att.file_name == "video_note-u.mp4"


def test_document_still_wins_and_speech_kinds_are_declared() -> None:
    msg = SimpleNamespace(
        document=SimpleNamespace(file_id="d", file_name="a.pdf", file_size=1),
        photo=None,
        voice=SimpleNamespace(file_id="v", file_unique_id="u", mime_type="audio/ogg", file_size=1),
        audio=None,
        video_note=None,
    )
    att = attachment_from_message(msg)
    assert att is not None and att.kind == "документ"
    assert SPEECH_KINDS == {"голосовое", "аудио", "видеозаметка"}


# ── словарь подсказок ────────────────────────────────────────────────────────


def test_keyterms_skip_comments_and_blanks(tmp_path: Path) -> None:
    f = tmp_path / "keyterms.txt"
    f.write_text("# коммент\n\nClaude Code\n  OpenClaw  \n\n# ещё\nGroq\n", encoding="utf-8")
    assert load_keyterms(f) == ["Claude Code", "OpenClaw", "Groq"]


def test_missing_keyterms_file_is_not_an_error(tmp_path: Path) -> None:
    """Нет словаря — работаем без него. Иначе ученик без файла получил бы падение
    на ровном месте."""
    assert load_keyterms(tmp_path / "нет-такого.txt") == []
    assert load_keyterms(None) == []


def test_keyterms_go_into_url_one_by_one() -> None:
    t = DeepgramTranscriber(api_key="k", keyterms=["Claude Code", "OpenClaw"], language="multi")
    q = parse_qs(urlparse(t._url()).query)
    assert q["keyterm"] == ["Claude Code", "OpenClaw"]
    assert q["language"] == ["multi"]
    assert q["model"] == ["nova-3"]


# ── поведение распознавателя ─────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, *, status: int = 200, payload: object = None, text: str = ""):
        self.status = status
        self._payload = payload
        self._text = text

    async def json(self) -> object:
        return self._payload

    async def text(self) -> str:
        return self._text

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakeSession:
    def __init__(self, response: object, **_: object):
        self._response = response

    def post(self, *_: object, **__: object) -> object:
        if isinstance(self._response, Exception):
            raise self._response
        return self._response

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def _patch_session(monkeypatch: pytest.MonkeyPatch, response: object) -> None:
    import engine.core.transcribe as mod

    monkeypatch.setattr(mod.aiohttp, "ClientSession", lambda **kw: _FakeSession(response, **kw))


@pytest.fixture
def audio(tmp_path: Path) -> Path:
    p = tmp_path / "voice.ogg"
    p.write_bytes(b"OggS-fake")
    return p


async def test_transcript_returned(monkeypatch: pytest.MonkeyPatch, audio: Path) -> None:
    payload = {"results": {"channels": [{"alternatives": [{"transcript": " Привет. "}]}]}}
    _patch_session(monkeypatch, _FakeResponse(payload=payload))
    assert await DeepgramTranscriber(api_key="k").transcribe(audio) == "Привет."


async def test_empty_transcript_is_an_error_not_silence(
    monkeypatch: pytest.MonkeyPatch, audio: Path
) -> None:
    """Ключевое решение модуля: тишина возвращается человеку словами, а не пустым промптом.
    Пустая строка агенту неотличима от «владелец ничего не сказал»."""
    payload = {"results": {"channels": [{"alternatives": [{"transcript": "   "}]}]}}
    _patch_session(monkeypatch, _FakeResponse(payload=payload))
    with pytest.raises(TranscriptionError, match="Не разобрал"):
        await DeepgramTranscriber(api_key="k").transcribe(audio)


async def test_unexpected_structure_is_an_error(
    monkeypatch: pytest.MonkeyPatch, audio: Path
) -> None:
    _patch_session(monkeypatch, _FakeResponse(payload={"что-то": "другое"}))
    with pytest.raises(TranscriptionError):
        await DeepgramTranscriber(api_key="k").transcribe(audio)


async def test_http_error_becomes_human_message(
    monkeypatch: pytest.MonkeyPatch, audio: Path
) -> None:
    _patch_session(monkeypatch, _FakeResponse(status=401, text="unauthorized"))
    with pytest.raises(TranscriptionError, match="ответил ошибкой"):
        await DeepgramTranscriber(api_key="k").transcribe(audio)


async def test_timeout_does_not_hang_forever(monkeypatch: pytest.MonkeyPatch, audio: Path) -> None:
    """Ни один из разобранных живых ботов таймаут не ставит: зависший провайдер запирает
    окно чата навсегда."""
    _patch_session(monkeypatch, TimeoutError())
    with pytest.raises(TranscriptionError, match="не уложилось"):
        await DeepgramTranscriber(api_key="k", timeout=5).transcribe(audio)


async def test_network_failure_becomes_human_message(
    monkeypatch: pytest.MonkeyPatch, audio: Path
) -> None:
    _patch_session(monkeypatch, OSError("сеть недоступна"))
    with pytest.raises(TranscriptionError, match="связаться"):
        await DeepgramTranscriber(api_key="k").transcribe(audio)


# ── рамка промпта ────────────────────────────────────────────────────────────


def test_own_speech_goes_first_and_without_untrusted_frame() -> None:
    """Речь владельца — его собственное сообщение. Untrusted-рамка стоит на файлах,
    а здесь человек говорит сам, и его слова должны идти первой строкой."""
    p = frame_voice_prompt(text="Привет, как дела", kind="голосовое", path=Path("/tmp/v.ogg"))
    assert p.startswith("Привет, как дела")
    assert "ПЕРЕСЛАННОЕ" not in p
    assert "/tmp/v.ogg" in p


def test_forwarded_speech_is_marked() -> None:
    """Пересланную запись говорит не владелец. Без пометки чужие слова уедут в долгую
    память как его собственные."""
    p = frame_voice_prompt(
        text="чужой голос", kind="голосовое", path=Path("/tmp/v.ogg"), forwarded=True
    )
    assert "ПЕРЕСЛАННОЕ" in p and "Не приписывай" in p
