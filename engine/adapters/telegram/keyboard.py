"""Кнопки-вкладки под ответом (§9) и разбор callback-данных.

Раскладка: ряды кнопок-триггеров агента (из инстансного buttons.yaml) + последний ряд
системных — «Проекты» / «Статус» / «Стоп». Системные дёргают уже существующие команды
движка (§5.5), триггеры отправляют свой prompt в сессию тем же путём, что обычное
сообщение (паттерн action_buttons боевого движка).

ГЛАВНЫЙ ИНВАРИАНТ (§8.2). В callback_data НЕ лежит текст промпта — никогда. Если бы промпт
ехал в callback, то любой, у кого есть callback-кнопка (а её видно в дампе сообщения),
инжектил бы движку произвольную инструкцию с правами владельца. Здесь инжекта нет по
конструкции: разбор умеет читать лишь `sys:<известное-действие>` и `btn:<N>:<отпечаток>`,
а сам prompt берётся из инстансного файла по индексу.

ВТОРОЙ ИНВАРИАНТ (ADV-13). Индекса одного недостаточно. `buttons.yaml` живой: владелец правит
его руками, а кнопки остаются висеть в истории чата навсегда. Кнопка «Отчёт» из вчерашнего
сообщения после перестановки строк в файле выполняла бы промпт СОСЕДНЕЙ кнопки — тихо и с
правами владельца. Поэтому рядом с индексом едет короткий отпечаток пары (label|prompt):
не совпал — «кнопка устарела», а не «выполню что-нибудь похожее».

Отпечаток не секрет и не подпись: подделать его тривиально, но подделывать нечего — промпт
всё равно берётся из файла. Его работа — отличить «та же кнопка» от «другая кнопка на том же
месте». Поэтому 4 байта sha256 хватает: лимит callback_data в Bot API — 64 байта.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from engine.core.brain import ButtonSpec

CB_SYSTEM_PREFIX = "sys:"
CB_TRIGGER_PREFIX = "btn:"
TRIGGERS_PER_ROW = 2
DIGEST_LEN = 8  # hex-символов от sha256 = 4 байта

SystemActionName = Literal["projects", "status", "stop"]
# label → действие; порядок задаёт вид системного ряда
SYSTEM_BUTTONS: tuple[tuple[str, SystemActionName], ...] = (
    ("Проекты", "projects"),
    ("Статус", "status"),
    ("Стоп", "stop"),
)
# Словарь строка → Literal: mypy сужает тип на выходе, `type: ignore` не нужен (K3)
_SYSTEM_BY_NAME: dict[str, SystemActionName] = {action: action for _, action in SYSTEM_BUTTONS}


@dataclass(frozen=True)
class SystemAction:
    """Нажата системная кнопка — дальше идёт команда движка (§5.5)."""

    action: SystemActionName


@dataclass(frozen=True)
class TriggerPress:
    """Нажата кнопка-триггер агента: индекс в инстансном buttons.yaml + её отпечаток."""

    index: int
    digest: str


Callback = SystemAction | TriggerPress


def button_digest(button: ButtonSpec) -> str:
    """Короткий отпечаток кнопки. `\\x00` как разделитель — в label/prompt его быть не может
    (`safe_label` вырезает управляющие символы), поэтому «ab|c» и «a|bc» не схлопнутся."""
    payload = f"{button.label}\x00{button.prompt}".encode()
    return hashlib.sha256(payload).hexdigest()[:DIGEST_LEN]


def encode_trigger(index: int, button: ButtonSpec) -> str:
    return f"{CB_TRIGGER_PREFIX}{index}:{button_digest(button)}"


def encode_system(action: SystemActionName) -> str:
    return f"{CB_SYSTEM_PREFIX}{action}"


def build_keyboard(buttons: list[ButtonSpec]) -> InlineKeyboardMarkup:
    """Собрать inline-ряд под ответ: триггеры агента + системный ряд последним."""
    rows: list[list[InlineKeyboardButton]] = []
    # enumerate по всему списку один раз (K18: `list(enumerate(...))` внутри цикла
    # пересобирал весь список на каждый ряд)
    numbered = list(enumerate(buttons))
    for start in range(0, len(numbered), TRIGGERS_PER_ROW):
        chunk = numbered[start : start + TRIGGERS_PER_ROW]
        rows.append(
            [
                InlineKeyboardButton(text=b.label, callback_data=encode_trigger(i, b))
                for i, b in chunk
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(text=label, callback_data=encode_system(action))
            for label, action in SYSTEM_BUTTONS
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def parse_callback(data: str | None) -> Callback | None:
    """Разобрать callback_data. Fail-closed: всё непонятное → None (движок ответит «устарела»).

    Ветки «взять инструкцию из callback» тут нет и быть не может — см. docstring модуля.
    """
    if not data:
        return None
    if data.startswith(CB_SYSTEM_PREFIX):
        action = data[len(CB_SYSTEM_PREFIX) :]
        # Literal сужается через словарь, а не `type: ignore`: mypy проверяет ветку,
        # а не верит на слово (K3)
        known = _SYSTEM_BY_NAME.get(action)
        return SystemAction(action=known) if known is not None else None
    if data.startswith(CB_TRIGGER_PREFIX):
        parts = data[len(CB_TRIGGER_PREFIX) :].split(":")
        if len(parts) != 2:
            return None
        raw_index, digest = parts
        # только ASCII-цифры: str.isdigit() пропускает '٣'/'½', и int() их бы съел
        if not raw_index or not all("0" <= ch <= "9" for ch in raw_index):
            return None
        if len(digest) != DIGEST_LEN or not all(ch in "0123456789abcdef" for ch in digest):
            return None
        return TriggerPress(index=int(raw_index), digest=digest)
    return None
