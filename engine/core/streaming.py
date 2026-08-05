"""Разбор сообщений Agent SDK + нарезка текста под лимиты Telegram.

Читатели потока живут в `events.py` (он же собирает ответ); здесь — опознание кусков и
арифметика длин, которой пользуются и ядро, и черновик адаптера.

Duck-typing по структуре SDK (не импортируем классы SDK — тестируемо и устойчиво к версиям):
- text-блок  → есть строковый атрибут .text
- сообщение  → есть итерируемый .content (список блоков)
- финал      → ResultMessage (по имени класса) или наличие .total_cost_usd
"""

from __future__ import annotations

from typing import Any

TELEGRAM_LIMIT = 4096  # лимит Telegram — в единицах UTF-16, см. split_message
# Запас на экранирование MarkdownV2: нарезка идёт ДО конвертации, а `markdownify` добавляет
# `\` перед спецсимволами — кусок ровно на 4096 после эскейпа перевалит лимит, Telegram
# ответит 400, и весь кусок уйдёт плейном без разметки.
DELIVERY_LIMIT = 3900
_EMPTY_PLACEHOLDER = "…"
_PARAGRAPH = "\n\n"


def extract_text(message: Any) -> str:
    """Склеить текст из всех text-блоков сообщения. Не-text блоки (tool_use) игнор."""
    content = getattr(message, "content", None)
    if content is None:
        return ""
    parts: list[str] = []
    for block in content:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def is_result(message: Any) -> bool:
    """Признак финального ResultMessage — сигнал остановить сбор."""
    if type(message).__name__ == "ResultMessage":
        return True
    return hasattr(message, "total_cost_usd")


def utf16_units(s: str) -> int:
    """Длина строки в единицах UTF-16 (как считает лимит Telegram)."""
    return len(s.encode("utf-16-le")) // 2


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Нарезать текст на куски ≤ limit единиц UTF-16.

    Считаем по UTF-16 (не по кодпойнтам): символы вне BMP (эмодзи) = 2 единицы,
    иначе эмодзи-плотный ответ переполнит лимит Telegram и получит 400.
    Итерируем по кодпойнтам — суррогатную пару не разрежем. Пустой текст → плейсхолдер.
    """
    if not text:
        return [_EMPTY_PLACEHOLDER]
    chunks: list[str] = []
    cur: list[str] = []
    cur_units = 0
    for ch in text:
        ch_units = utf16_units(ch)
        if cur and cur_units + ch_units > limit:
            chunks.append("".join(cur))
            cur, cur_units = [], 0
        cur.append(ch)
        cur_units += ch_units
    if cur:
        chunks.append("".join(cur))
    return chunks


def tail_by_units(text: str, units: int) -> str:
    """Последние `units` единиц UTF-16 текста (для скользящего окна черновика §9).

    Идём по кодпойнтам с конца — суррогатную пару не разорвём, иначе строка перестаёт
    кодироваться в UTF-16 и Telegram отвергает сообщение целиком.
    """
    if units <= 0:
        return ""
    acc: list[str] = []
    total = 0
    for ch in reversed(text):
        ch_units = utf16_units(ch)
        if total + ch_units > units:
            break
        acc.append(ch)
        total += ch_units
    return "".join(reversed(acc))


def split_for_delivery(text: str, limit: int = DELIVERY_LIMIT) -> list[str]:
    """Нарезка ФИНАЛЬНОГО ответа: с запасом на эскейп и по границам абзацев.

    От `split_message` отличается двумя вещами, и обе — про разметку:
      • лимит по умолчанию ниже телеграмного (DELIVERY_LIMIT) — запас на `\\`-эскейп MarkdownV2;
      • разрез ищется по границе абзаца, чтобы не рвать пополам ```-блок или список: обе
        половинки такого куска теряют форматирование.
    Абзац длиннее лимита (одна простыня без пустых строк) — жёсткая резка `split_message`:
    отдать кусок, который Telegram отвергнет, хуже неудачного разреза.
    """
    if not text:
        return [_EMPTY_PLACEHOLDER]
    chunks: list[str] = []
    cur = ""
    for para in text.split(_PARAGRAPH):
        candidate = f"{cur}{_PARAGRAPH}{para}" if cur else para
        if utf16_units(candidate) <= limit:
            cur = candidate
            continue
        if cur:
            chunks.append(cur)
            cur = ""
        if utf16_units(para) <= limit:
            cur = para
            continue
        hard = split_message(para, limit)
        chunks.extend(hard[:-1])
        cur = hard[-1]
    if cur:
        chunks.append(cur)
    return chunks or [_EMPTY_PLACEHOLDER]
