"""Кнопки-вкладки под ответом (§9) и разбор callback-данных.

Раскладка: ряды кнопок-триггеров агента (из инстансного buttons.yaml) + последний ряд
системных — «Проекты» / «Статус» / «Стоп». Системные дёргают уже существующие команды
движка (§5.5), триггеры отправляют свой prompt в сессию тем же путём, что обычное
сообщение (паттерн action_buttons боевого движка).

ГЛАВНЫЙ ИНВАРИАНТ (§8.2). В callback_data лежит только ИНДЕКС кнопки — никогда не текст
промпта. Если бы промпт ехал в callback, то любой, у кого есть callback-кнопка (а её видно
в дампе сообщения), инжектил бы движку произвольную инструкцию с правами владельца. Здесь
инжекта нет по конструкции: разбор умеет читать лишь `sys:<известное-действие>` и `btn:<N>`,
а сам prompt берётся из инстансного файла по индексу. Плюс это дёшево: лимит callback_data
в Bot API — 64 байта, промпт туда всё равно не влез бы.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from engine.core.brain import ButtonSpec

CB_SYSTEM_PREFIX = "sys:"
CB_TRIGGER_PREFIX = "btn:"
TRIGGERS_PER_ROW = 2

SystemActionName = Literal["projects", "status", "stop"]
# label → действие; порядок задаёт вид системного ряда
SYSTEM_BUTTONS: tuple[tuple[str, SystemActionName], ...] = (
    ("Проекты", "projects"),
    ("Статус", "status"),
    ("Стоп", "stop"),
)
_SYSTEM_ACTIONS: frozenset[str] = frozenset(action for _, action in SYSTEM_BUTTONS)


@dataclass(frozen=True)
class SystemAction:
    """Нажата системная кнопка — дальше идёт команда движка (§5.5)."""

    action: SystemActionName


@dataclass(frozen=True)
class TriggerPress:
    """Нажата кнопка-триггер агента: индекс в инстансном buttons.yaml."""

    index: int


Callback = SystemAction | TriggerPress


def encode_trigger(index: int) -> str:
    return f"{CB_TRIGGER_PREFIX}{index}"


def encode_system(action: SystemActionName) -> str:
    return f"{CB_SYSTEM_PREFIX}{action}"


def build_keyboard(buttons: list[ButtonSpec]) -> InlineKeyboardMarkup:
    """Собрать inline-ряд под ответ: триггеры агента + системный ряд последним."""
    rows: list[list[InlineKeyboardButton]] = []
    for start in range(0, len(buttons), TRIGGERS_PER_ROW):
        chunk = list(enumerate(buttons))[start : start + TRIGGERS_PER_ROW]
        rows.append(
            [InlineKeyboardButton(text=b.label, callback_data=encode_trigger(i)) for i, b in chunk]
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
        if action in _SYSTEM_ACTIONS:
            return SystemAction(action=action)  # type: ignore[arg-type]
        return None
    if data.startswith(CB_TRIGGER_PREFIX):
        raw = data[len(CB_TRIGGER_PREFIX) :]
        # только ASCII-цифры: str.isdigit() пропускает '٣'/'½', и int() их бы съел
        if raw and all("0" <= ch <= "9" for ch in raw):
            return TriggerPress(index=int(raw))
        return None
    return None
