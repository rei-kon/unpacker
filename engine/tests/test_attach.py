"""Приём документов и фото в Telegram — §5.5 + §8.2.

Скачивание тестируется с дублёром Bot (как FakeCore в test_router): реальный Bot API для
проверки лимитов и имён не нужен, а нужен контроль над тем, что «прислал клиент».

Отдельно проверяется ЛЖИВЫЙ `file_size`: его сообщает клиент, и верить ему нельзя —
объявил 1 КБ, прислал 100 МБ. Значит после скачивания размер проверяется по факту.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from engine.adapters.telegram.attach import (
    Attachment,
    AttachmentIntake,
    IntakeResult,
    attachment_from_message,
)
from engine.core.uploads import MAX_UPLOAD_BYTES, UploadStore

SID = "0b3f5c9a-1111-2222-3333-444455556666"


class FakeBot:
    """Дублёр aiogram.Bot: пишет заданные байты в destination, считает вызовы."""

    def __init__(self, payload: bytes = b"content", *, fail: Exception | None = None):
        self.payload = payload
        self.fail = fail
        self.downloads: list[str] = []

    async def download(self, file_id, destination):  # noqa: ANN001 — сигнатура aiogram
        self.downloads.append(file_id)
        if self.fail is not None:
            # частичная запись до сбоя — как при обрыве сети на середине файла
            Path(destination).write_bytes(b"partial")
            raise self.fail
        Path(destination).write_bytes(self.payload)


@pytest.fixture
def uploads(tmp_path):
    return UploadStore(tmp_path / "state" / "uploads")


def _intake(bot, uploads, **kw):
    return AttachmentIntake(bot=bot, uploads=uploads, **kw)


def _doc(name="отчёт.pdf", size=100, file_id="F1"):
    return Attachment(file_id=file_id, file_name=name, size=size, kind="документ")


# ── green path ───────────────────────────────────────────────────────────────


async def test_saves_document_into_session_dir(uploads):
    bot = FakeBot(b"PDF-DATA")
    result = await _intake(bot, uploads).take(session_id=SID, attachment=_doc())
    assert result.error is None
    assert result.path is not None
    assert result.path.parent == uploads.dir_for(SID)
    assert result.path.read_bytes() == b"PDF-DATA"
    assert bot.downloads == ["F1"]


async def test_hostile_file_name_is_neutralized(uploads):
    result = await _intake(FakeBot(), uploads).take(
        session_id=SID, attachment=_doc(name="../../.env")
    )
    assert result.path is not None
    assert result.path.name == "env"
    assert result.path.parent == uploads.dir_for(SID)


async def test_two_files_same_name_both_kept(uploads):
    intake = _intake(FakeBot(), uploads)
    first = await intake.take(session_id=SID, attachment=_doc())
    second = await intake.take(session_id=SID, attachment=_doc())
    assert first.path != second.path
    assert first.path.exists() and second.path.exists()


async def test_photo_without_name_gets_one(uploads):
    photo = Attachment(file_id="P1", file_name=None, size=50, kind="фото")
    result = await _intake(FakeBot(), uploads).take(session_id=SID, attachment=photo)
    assert result.path is not None and result.path.suffix == ".jpg"


# ── лимит 20 МБ ──────────────────────────────────────────────────────────────


async def test_declared_oversize_refused_without_download(uploads):
    """Объявленный размер больше лимита → даже не дёргаем Bot API (§5.5, честный ответ)."""
    bot = FakeBot()
    result = await _intake(bot, uploads).take(
        session_id=SID, attachment=_doc(size=MAX_UPLOAD_BYTES + 1)
    )
    assert result.path is None
    assert result.error and "20" in result.error
    assert bot.downloads == [], "качать заведомо большой файл незачем"


async def test_lying_file_size_caught_after_download(uploads):
    """Клиент объявил 10 байт, прислал 5000 — ловим по факту и удаляем."""
    bot = FakeBot(b"x" * 5000)
    result = await _intake(bot, uploads, max_bytes=1000).take(
        session_id=SID, attachment=_doc(size=10)
    )
    assert result.path is None
    assert result.error
    assert list(uploads.dir_for(SID).iterdir()) == [], "перебор по факту не оставляет файл"


async def test_unknown_size_is_downloaded_then_checked(uploads):
    # size=None (Telegram не всегда его даёт) — не повод отказывать заранее
    bot = FakeBot(b"ok")
    result = await _intake(bot, uploads).take(session_id=SID, attachment=_doc(size=None))
    assert result.error is None and result.path is not None


# ── сбои: понятная строка, без огрызков на диске ─────────────────────────────


async def test_download_failure_reports_and_cleans_up(uploads):
    bot = FakeBot(fail=RuntimeError("file is too big"))
    result = await _intake(bot, uploads).take(session_id=SID, attachment=_doc())
    assert result.path is None
    assert result.error and "не" in result.error.lower()
    assert list(uploads.dir_for(SID).iterdir()) == [], "недокачанный огрызок удаляем"


async def test_download_failure_message_mentions_limit_hint(uploads):
    # частый реальный случай — Bot API отказывает на >20 МБ уже при getFile
    bot = FakeBot(fail=RuntimeError("file is too big"))
    result = await _intake(bot, uploads).take(session_id=SID, attachment=_doc())
    assert result.error and "20" in result.error


# ── разбор сообщения → Attachment ────────────────────────────────────────────


def test_from_message_reads_document():
    message = SimpleNamespace(
        document=SimpleNamespace(file_id="D1", file_name="файл.docx", file_size=42),
        photo=None,
    )
    att = attachment_from_message(message)
    assert att == Attachment(file_id="D1", file_name="файл.docx", size=42, kind="документ")


def test_from_message_picks_largest_photo():
    # Telegram присылает лестницу размеров; берём последний (самый большой)
    message = SimpleNamespace(
        document=None,
        photo=[
            SimpleNamespace(file_id="small", file_size=100, file_unique_id="u1"),
            SimpleNamespace(file_id="big", file_size=9000, file_unique_id="u2"),
        ],
    )
    att = attachment_from_message(message)
    assert att is not None and att.file_id == "big" and att.kind == "фото"


def test_from_message_none_for_plain_text():
    assert attachment_from_message(SimpleNamespace(document=None, photo=None)) is None


# ── K12: инвариант IntakeResult «никогда оба» держится кодом, а не комментарием ─


def test_intake_result_cannot_hold_both_path_and_error(tmp_path):
    """Docstring обещал «либо путь, либо сообщение об отказе. Никогда оба» — и всё.

    Смешанный результат хендлер обработал бы как успех (проверяется только `path`), молча
    потеряв объяснение отказа. Инвариант должен ломаться в конструкторе.
    """
    with pytest.raises(ValueError):
        IntakeResult(path=tmp_path / "f.bin", error="и то и другое")


def test_intake_result_cannot_be_empty():
    """Пустой результат — тоже не состояние: хендлер ответил бы «Файл не принят» без причины."""
    with pytest.raises(ValueError):
        IntakeResult(path=None, error=None)


def test_intake_result_accepts_each_side_alone(tmp_path):
    assert IntakeResult(path=tmp_path / "f.bin", error=None).error is None
    assert IntakeResult(path=None, error="не смог").path is None
