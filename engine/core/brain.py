"""Паспорт папки-мозга `.brain.yaml` и инстансный `buttons.yaml` — §4, §7.5.

Ключевая мысль конституции: паспорт — это **ДАННЫЕ под строгой схемой, а не инструкции**.
Мозг может прийти откуда угодно (чужой git, ученик скачал шаблон), поэтому:

- парсим типизированно (pydantic, `extra="forbid"`) — неизвестный ключ вида `system_prompt:`
  не «просочится», а получит понятную ошибку; чужой файл не может дописать движку настройку;
- `slug` — только `^[a-z0-9-]+$` (тот же инвариант, что в store.ProjectStore);
- свободный текст (`label`) проходит `safe_label`: без управляющих символов и переводов строк,
  обрезан по длине. Label живёт ТОЛЬКО на кнопке; в контекст LLM уходит `prompt`, и только
  когда владелец нажал кнопку (см. adapters/telegram/keyboard.py — callback несёт индекс,
  а не текст);
- битый yaml — `PassportError` с человеческим текстом, а не traceback в лицо и не смерть
  процесса: мозг ученика с опечаткой в паспорте не должен ронять бота.

Источник правды по кнопкам (§4): `.brain.yaml` мозга — ШАБЛОН; при первом деплое он
копируется в `buttons.yaml` инстанса (см. engine/brainkit.py), дальше владелец правит
инстансную копию, и синк мозга её НЕ перезатирает — та же дисциплина, что у `.env`.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from engine.core.models import SLUG_PATTERN, SLUG_RE

PASSPORT_NAME = ".brain.yaml"
BUTTONS_KEY = "buttons"
LABEL_MAX = 64
PROMPT_MAX = 4000
MAX_BUTTONS = 24

_BUTTONS_HEADER = (
    "# Кнопки-вкладки агента (§9). Правь этот файл — он инстансный, синк мозга его не трогает.\n"
    "# label — что видно на кнопке, prompt — что уйдёт агенту при нажатии.\n"
)


class PassportError(Exception):
    """Битый/чужой паспорт: понятная ошибка вместо падения процесса (§4)."""


def safe_label(raw: str) -> str:
    """Обезвредить свободный текст для показа на кнопке.

    Убираем управляющие символы (категория Unicode `C*` — сюда попадают \\n, \\t, \\x00 и
    zero-width/RTL-трюки, которыми можно нарисовать на кнопке одно, а хранить другое),
    сжимаем пробелы и режем по длине лимита Telegram.
    """
    cleaned = "".join(" " if unicodedata.category(ch).startswith("C") else ch for ch in raw)
    return " ".join(cleaned.split())[:LABEL_MAX]


class ButtonSpec(BaseModel):
    """Одна кнопка-триггер: подпись + готовый промпт (§9)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str
    prompt: str

    @field_validator("label", mode="after")
    @classmethod
    def _clean_label(cls, v: str) -> str:
        label = safe_label(v)
        if not label:
            raise ValueError("label кнопки пуст")
        return label

    @field_validator("prompt", mode="after")
    @classmethod
    def _check_prompt(cls, v: str) -> str:
        prompt = v.strip()
        if not prompt:
            raise ValueError("prompt кнопки пуст")
        if len(prompt) > PROMPT_MAX:
            raise ValueError(f"prompt кнопки длиннее {PROMPT_MAX} символов")
        return prompt


class BrainPassport(BaseModel):
    """Паспорт мозга. Все поля опциональны — минимум валидного мозга это один CLAUDE.md."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str | None = None
    slug: str | None = None
    default_model: Literal["opus", "sonnet", "haiku"] | None = None
    buttons: list[ButtonSpec] = []
    proactivity: bool = False

    @field_validator("slug", mode="after")
    @classmethod
    def _check_slug(cls, v: str | None) -> str | None:
        # K16: import был локальным внутри валидатора — на каждый разбор паспорта лишний
        # поиск в sys.modules и ложное впечатление «тут что-то тяжёлое»
        if v is not None and not SLUG_RE.match(v):
            raise ValueError(f"slug должен быть {SLUG_PATTERN}, получено: {v!r}")
        return v

    @field_validator("buttons", mode="after")
    @classmethod
    def _check_count(cls, v: list[ButtonSpec]) -> list[ButtonSpec]:
        if len(v) > MAX_BUTTONS:
            raise ValueError(f"слишком много кнопок: {len(v)} (максимум {MAX_BUTTONS})")
        return v


# ── парсинг ──────────────────────────────────────────────────────────────────


def _load_mapping(text: str, *, where: str) -> dict[str, Any]:
    """yaml → словарь. safe_load (никаких `!!python/object`), не-словарь — ошибка."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PassportError(f"{where}: битый YAML — {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise PassportError(
            f"{where}: ожидался список полей «ключ: значение», а не {type(data).__name__}"
        )
    return data


def _humanize(exc: ValidationError, *, where: str) -> PassportError:
    """ValidationError pydantic → одна человеческая строка с именами полей."""
    parts = []
    for err in exc.errors():
        field = ".".join(str(p) for p in err["loc"]) or "(корень)"
        parts.append(f"{field}: {err['msg']}")
    return PassportError(f"{where}: " + "; ".join(parts))


def parse_passport(text: str, *, where: str = PASSPORT_NAME) -> BrainPassport:
    """Разобрать текст `.brain.yaml`. Ошибки — PassportError, а не падение процесса."""
    data = _load_mapping(text, where=where)
    try:
        return BrainPassport(**data)
    except ValidationError as exc:
        raise _humanize(exc, where=where) from exc


def parse_buttons(text: str, *, where: str = "buttons.yaml") -> list[ButtonSpec]:
    """Разобрать инстансный `buttons.yaml` — тот же корневой ключ `buttons` (контракт срезов).

    Разбор СТРОГИЙ, в отличие от снисходительного «нет ключа — значит кнопок нет» (K7).
    Этот файл правит ЧЕЛОВЕК руками, и `button:` вместо `buttons:` — самая частая опечатка.
    Раньше она давала тихий ноль кнопок: владелец дописал строку, перечитал, ряда нет,
    подсказки нет. Теперь это PassportError — реестр её логирует и держит прошлый набор,
    то есть живой бот не раздевается, а причина видна в journalctl.
    """
    data = _load_mapping(text, where=where)
    if not data:  # пустой файл — честное «кнопок нет», а не опечатка
        return []
    unknown = sorted(set(data) - {BUTTONS_KEY})
    if unknown:
        raise PassportError(
            f"{where}: неизвестные поля {', '.join(unknown)} — "
            f"кнопки объявляются под корневым ключом «{BUTTONS_KEY}:»"
        )
    raw = data.get(BUTTONS_KEY)
    if raw is None:
        return []
    try:
        return BrainPassport(buttons=raw).buttons
    except ValidationError as exc:
        raise _humanize(exc, where=where) from exc


def load_passport(brain_dir: str | Path) -> BrainPassport | None:
    """Прочитать паспорт из папки-мозга. Нет файла → None (паспорт опционален, §4)."""
    path = Path(brain_dir) / PASSPORT_NAME
    if not path.is_file():
        return None
    return parse_passport(_read(path), where=str(path))


def load_instance_buttons(path: str | Path) -> list[ButtonSpec]:
    """Прочитать инстансный buttons.yaml. Нет файла → пусто (останется системный ряд)."""
    p = Path(path)
    if not p.is_file():
        return []
    return parse_buttons(_read(p), where=str(p))


def dump_buttons_yaml(buttons: list[ButtonSpec]) -> str:
    """Сериализовать кнопки в инстансный файл — с шапкой-подсказкой владельцу.

    allow_unicode=True: файл владелец правит руками, `\\u041e\\u0442...` вместо «Отчёт» —
    издевательство. sort_keys=False, чтобы label шёл перед prompt, как человек и пишет.
    """
    body = yaml.safe_dump(
        {BUTTONS_KEY: [{"label": b.label, "prompt": b.prompt} for b in buttons]},
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    return _BUTTONS_HEADER + body


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PassportError(f"{path}: не читается — {exc}") from exc
    except UnicodeDecodeError as exc:
        # бинарь вместо yaml (положили zip и переименовали) — тоже понятная ошибка
        raise PassportError(f"{path}: не текст в UTF-8") from exc
