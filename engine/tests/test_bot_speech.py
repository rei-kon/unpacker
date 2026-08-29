"""Хендлер речи в Telegram-адаптере: голосовое → текст → обычный путь промпта.

Стенд взят из test_bot_attachments (тот же инстанс «как после deploy.sh»), потому что
проверяется то же место склейки, только другой исход: у файла судьба «лечь на диск и
получить путь», у речи — «стать текстом владельца».
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from engine.adapters.telegram.attach import AttachmentIntake
from engine.adapters.telegram.bot import TelegramBot
from engine.adapters.telegram.router import SessionRouter
from engine.core.buttons import ButtonRegistry
from engine.core.pool import ClientPool
from engine.core.security import AllowList
from engine.core.sendfile import SendFilePolicy
from engine.core.store import Store
from engine.core.transcribe import TranscriptionError
from engine.core.uploads import UploadStore
from engine.tests.test_bot_attachments import BUTTONS_YAML, OWNER, FakeBot, FakeCore


class FakeTranscriber:
    """Дублёр распознавателя: сеть не нужна, проверяем склейку."""

    def __init__(self, *, text: str = "Привет, это я голосом", fail: Exception | None = None):
        self.text = text
        self.fail = fail
        self.calls: list[Path] = []

    async def transcribe(self, path: Path) -> str:
        self.calls.append(path)
        if self.fail is not None:
            raise self.fail
        return self.text


def _voice_message(
    *,
    caption: str | None = None,
    forwarded: bool = False,
    user_id: int = OWNER,
    size: int | None = 2048,
):
    reacted: list = []
    msg = SimpleNamespace(
        text=None,
        caption=caption,
        document=None,
        photo=None,
        voice=SimpleNamespace(
            file_id="voice-1", file_unique_id="uq", mime_type="audio/ogg", file_size=size
        ),
        audio=None,
        video_note=None,
        forward_origin=SimpleNamespace(type="user") if forwarded else None,
        forward_date=None,
        message_thread_id=7,
        chat=SimpleNamespace(id=100, type="private"),
        from_user=SimpleNamespace(id=user_id),
        reacted=reacted,
        replies=[],
    )

    async def react(items):
        reacted.append(items)

    async def reply(text, **kw):
        msg.replies.append(text)

    msg.react = react
    msg.reply = reply
    return msg


@pytest.fixture
def stand(tmp_path):
    brain = tmp_path / "brains" / "office"
    brain.mkdir(parents=True)
    (brain / "CLAUDE.md").write_text("# мозг\n", encoding="utf-8")
    inst = tmp_path / "agents" / "office"
    state = inst / "state"
    (state / "uploads").mkdir(parents=True)
    (inst / "buttons.yaml").write_text(BUTTONS_YAML, encoding="utf-8")
    store = Store(str(state / "state.db"))
    store.projects.create(slug="office", name="Офис", brain_path=str(brain), default_model=None)
    yield SimpleNamespace(inst=inst, state=state, store=store)
    store.close()


def _build(stand, *, transcriber=None, uploads=True):
    bot = FakeBot()
    core = FakeCore()
    router = SessionRouter(store=stand.store, core=core, default_project_slug="office")
    tg = TelegramBot(
        bot=bot,
        allow=AllowList([OWNER]),
        router=router,
        store=stand.store,
        pool=ClientPool(factory=lambda options: None, ceiling=2, idle_timeout=60.0),
        buttons=ButtonRegistry(stand.inst / "buttons.yaml"),
        intake=AttachmentIntake(bot=bot, uploads=UploadStore(stand.state / "uploads"))
        if uploads
        else None,
        send_file=SendFilePolicy(state_root=stand.state),
        transcriber=transcriber,
    )
    return SimpleNamespace(tg=tg, bot=bot, core=core)


# ── главный критерий ─────────────────────────────────────────────────────────


async def test_voice_becomes_prompt_in_session(stand):
    """Ради этого всё и делалось: наговорил голосом — агент получил текст тем же путём,
    что обычное сообщение."""
    tr = FakeTranscriber(text="Сегодня закрыл кинжал")
    s = _build(stand, transcriber=tr)
    await s.tg._on_speech(_voice_message())
    assert s.core.prompts, "промпт не ушёл в сессию"
    assert s.core.prompts[0].startswith("Сегодня закрыл кинжал")


async def test_original_recording_is_kept_on_disk(stand):
    """Расшифровка без оригинала — потеря: кривой текст нечем перепроверить."""
    tr = FakeTranscriber()
    s = _build(stand, transcriber=tr)
    await s.tg._on_speech(_voice_message())
    assert len(tr.calls) == 1
    saved = tr.calls[0]
    assert saved.exists() and saved.suffix == ".ogg"


async def test_caption_goes_before_transcript(stand):
    """Подпись к голосовому — отдельная мысль владельца, она не должна теряться."""
    s = _build(stand, transcriber=FakeTranscriber(text="тело записи"))
    await s.tg._on_speech(_voice_message(caption="это про вчерашнее"))
    assert s.core.prompts[0].startswith("это про вчерашнее")
    assert "тело записи" in s.core.prompts[0]


async def test_forwarded_voice_is_marked_as_someone_elses(stand):
    """Чужая пересланная запись не должна уехать в долгую память как слова владельца."""
    s = _build(stand, transcriber=FakeTranscriber(text="чужие слова"))
    await s.tg._on_speech(_voice_message(forwarded=True))
    assert "ПЕРЕСЛАННОЕ" in s.core.prompts[0]


# ── отказы: человек всегда получает причину ──────────────────────────────────


async def test_without_transcriber_says_why(stand):
    """Не настроен сервис — говорим ПРИЧИНУ. «Не принимаю» без причины выглядит поломкой,
    а тут не хватает одной строки в .env."""
    s = _build(stand, transcriber=None)
    msg = _voice_message()
    await s.tg._on_speech(msg)
    assert "не настроен" in s.bot.text
    assert not s.core.prompts


async def test_transcription_error_reaches_the_human(stand):
    """Текст ошибки написан для человека — уходит как есть, промпт не отправляется."""
    s = _build(stand, transcriber=FakeTranscriber(fail=TranscriptionError("Не разобрал речь")))
    await s.tg._on_speech(_voice_message())
    assert "Не разобрал речь" in s.bot.text
    assert not s.core.prompts


async def test_oversized_audio_is_refused_before_download(stand):
    """Голосовые в лимит Bot API не упираются, а вот музыка и WAV с диктофона — легко.
    Проверка размера обязана работать для ВСЕХ речевых типов, не только для voice."""
    s = _build(stand, transcriber=FakeTranscriber())
    huge = 25 * 1024 * 1024
    await s.tg._on_speech(_voice_message(size=huge))
    assert "20 МБ" in s.bot.text
    assert not s.core.prompts


async def test_stranger_gets_nothing(stand):
    """Гейт первым: чужому не отвечаем даже отказом — ответ подтверждает, что бот живой."""
    s = _build(stand, transcriber=FakeTranscriber())
    msg = _voice_message(user_id=OWNER + 1)
    await s.tg._on_speech(msg)
    assert not s.bot.messages and not s.core.prompts
