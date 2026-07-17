"""Конвертация обычного Markdown (вывод агента) → Telegram MarkdownV2.

Агент отдаёт стандартный Markdown (**bold**, # heading, списки, ```code```).
Telegram рендерит только свой MarkdownV2 со строгим эскейпом — ручной эскейп хрупок,
поэтому используем telegramify-markdown. При любой ошибке → None (вызов уйдёт плейн-текстом).
"""

from __future__ import annotations

import logging

logger = logging.getLogger("unpacker.engine")

try:
    import telegramify_markdown
except Exception:  # noqa: BLE001 — либа опциональна; без неё шлём плейн
    telegramify_markdown = None


def to_telegram_markdown(text: str) -> str | None:
    """Стандартный Markdown → Telegram MarkdownV2. None → отправлять плейном."""
    if telegramify_markdown is None:
        return None
    try:
        return telegramify_markdown.markdownify(text)
    except Exception:  # noqa: BLE001
        logger.warning("markdownify failed, fallback to plain", exc_info=True)
        return None
