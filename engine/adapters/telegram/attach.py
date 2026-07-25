"""Приём вложений из Telegram: документ/фото → файл в uploads инстанса (§5.5).

Тонкая прослойка над `engine.core.uploads`: политика безопасности (имя, каталог, рамка) живёт
в ядре и одинакова для всех поверхностей, здесь — только Telegram-специфика: как достать
file_id из сообщения и как скачать его Bot API.

Два не-очевидных решения:

1. **Размер проверяем ДВАЖДЫ.** `file_size` сообщает клиент, значит это заявление, а не факт:
   объявил 1 КБ — прислал 100 МБ. До скачивания отказываем по заявленному (не жжём сеть на
   заведомо большом), после — по фактически записанному, и перебор удаляем.
2. **Огрызки убираем.** Обрыв на середине оставляет полфайла. Отдать агенту путь на битый
   файл хуже, чем честно сказать «не смог»: агент начнёт «читать» мусор и придумает по нему
   ответ.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from engine.core.uploads import MAX_UPLOAD_BYTES, UploadStore, too_large_message

logger = logging.getLogger("unpacker.engine")

Kind = Literal["документ", "фото"]
_FALLBACK_NAMES: dict[str, str] = {"фото": "photo.jpg", "документ": "file.bin"}


@dataclass(frozen=True)
class Attachment:
    """Нормализованное вложение — без aiogram-типов, чтобы ядро тестировалось дублёрами."""

    file_id: str
    file_name: str | None
    size: int | None
    kind: Kind


@dataclass(frozen=True)
class IntakeResult:
    """Либо путь к принятому файлу, либо человеческое сообщение об отказе. Никогда оба.

    Инвариант держит `__post_init__`, а не комментарий (K12): смешанный результат хендлер
    прочитал бы как успех (он смотрит только на `path`) и молча потерял бы объяснение
    отказа, а пустой — ответил бы «Файл не принят» без причины.
    """

    path: Path | None
    error: str | None

    def __post_init__(self) -> None:
        if (self.path is None) == (self.error is None):
            raise ValueError("IntakeResult: ровно одно из path/error, не оба и не ничего")

    @classmethod
    def ok(cls, path: Path) -> IntakeResult:
        return cls(path=path, error=None)

    @classmethod
    def failed(cls, error: str) -> IntakeResult:
        return cls(path=None, error=error)


def attachment_from_message(message: Any) -> Attachment | None:
    """Достать вложение из сообщения. Не вложение → None (обработает текстовый хендлер)."""
    document = getattr(message, "document", None)
    if document is not None:
        return Attachment(
            file_id=document.file_id,
            file_name=getattr(document, "file_name", None),
            size=getattr(document, "file_size", None),
            kind="документ",
        )
    photo = getattr(message, "photo", None)
    if photo:
        # Telegram отдаёт лестницу размеров по возрастанию — берём самый большой
        best = photo[-1]
        return Attachment(
            file_id=best.file_id,
            file_name=f"photo-{getattr(best, 'file_unique_id', 'tg')}.jpg",
            size=getattr(best, "file_size", None),
            kind="фото",
        )
    return None


class AttachmentIntake:
    def __init__(self, *, bot: Any, uploads: UploadStore, max_bytes: int = MAX_UPLOAD_BYTES):
        self._bot = bot
        self._uploads = uploads
        self._max_bytes = max_bytes

    async def take(self, *, session_id: str, attachment: Attachment) -> IntakeResult:
        """Скачать вложение в `state/uploads/<session_id>/`. Отказ — текстом, не исключением."""
        if attachment.size is not None and attachment.size > self._max_bytes:
            return IntakeResult.failed(
                too_large_message(size=attachment.size, limit=self._max_bytes)
            )

        # Имени может не быть вовсе. Расширение здесь не косметика: по нему агент решает,
        # чем открывать файл, и безымянный `file.bin` вместо картинки сбивает его с толку.
        name = attachment.file_name or _FALLBACK_NAMES[attachment.kind]
        path = self._uploads.reserve(session_id, name)
        try:
            await self._bot.download(attachment.file_id, destination=path)
        except Exception as exc:  # noqa: BLE001 — сеть/Bot API: сообщаем, а не роняем чат
            logger.warning("не скачал вложение %s: %s", attachment.file_id, exc)
            _unlink(path)
            return IntakeResult.failed(
                "Не смог забрать файл из Telegram. Если он больше "
                f"{self._max_bytes // (1024 * 1024)} МБ — Bot API его не отдаёт; "
                "иначе пришли ещё раз."
            )

        actual = path.stat().st_size if path.exists() else 0
        if actual > self._max_bytes:
            _unlink(path)  # соврали в file_size — файл на диске не оставляем
            return IntakeResult.failed(too_large_message(size=actual, limit=self._max_bytes))
        return IntakeResult.ok(path)


def _unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:  # noqa: S110 — не смогли убрать огрызок: не повод рушить ответ
        logger.warning("не удалил недокачанный файл %s", path)
