"""Отдача файлов агентом: маркер `[SEND_FILE:путь]` + проверка путей — §9, §8.2.

Механизм отдачи в движке ОДИН (§9, §14): агент пишет в текст ответа маркер, движок вырезает
его и отправляет файл документом. MCP-тулы под это не заводим — лишняя поверхность.

## Что это на самом деле (честно, SEC-9)

Это **guardrail против ошибки модели, а не sandbox против агента**. Агент внутреннего контура
работает с `bypassPermissions`, у него есть Bash — и `cp ~/.claude/.credentials.json
brain/notes.txt` обходит любую проверку пути, потому что после копии файл действительно лежит
внутри разрешённого корня под невинным именем. Так же не ловятся hardlink (у жёсткой ссылки нет
«цели», `resolve()` её не раскроет) и TOCTOU-подмена содержимого после проверки.

Настоящая защита от агента — урезанный ToolPolicy клиентского контура (Фаза 4). Здесь мы решаем
задачу поменьше, но реальную: путь диктует МОДЕЛЬ, а модель читает недоверенные файлы (uploads)
и чужие мозги. Значит путь — недоверенный ввод даже когда «агент свой», и модель, которую
уговорили строкой в файле, не должна одним маркером выдать секрет:

  • `/etc/passwd`, `/root/.ssh/id_rsa`      — абсолютный путь наружу;
  • `../../.env`                            — токен бота инстанса;
  • `~/.claude/.credentials.json`           — токен подписки владельца;
  • симлинк из мозга на файл снаружи        — легальное имя, чужая цель.

## Две проверки, а не одна

1. **Корни.** `Path.resolve()` (раскрывает `..` И симлинки) обязан лежать внутри одного из
   разрешённых корней: папка-мозг проекта и `state/` инстанса (там же uploads — файл,
   пришедший от владельца, можно вернуть). Проверять текст пути на «..» — путь в никуда:
   симлинки так не поймать.
2. **Deny-list по имени, безусловный.** Корни приходят из конфига, а конфиг ученик правит
   руками: `STATE_DIR=.` или `DB_PATH=state.db` в старой версии делали корнем каталог
   инстанса вместе с `.env`. Поэтому секретные ИМЕНА запрещены всегда, независимо от корней —
   ремень поверх подтяжек (K1/SEC-5).
"""

from __future__ import annotations

import fnmatch
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

logger = logging.getLogger("unpacker.engine")

# Маркер намеренно однострочный и без `]` внутри: жадный матч через абзац съел бы полответа.
_MARKER_RE = re.compile(r"\[SEND_FILE:([^\]\n]*)\]")
# Незакрытый хвост маркера — только для ЧЕРНОВИКА псевдо-стриминга: пока дельты идут,
# `[SEND_FILE:state/отч` — нормальный кадр, и показывать его человеку нечего. В финале
# такой обрывок остаётся текстом (там маркер либо целый, либо это и не маркер).
#
# Вложенные опциональные группы — это «любой ПРЕФИКС слова SEND_FILE:», а не «любой текст
# после `[`». Разница принципиальная: `[ссылку` или незакрытая markdown-ссылка `[тек` не
# должны исчезать из черновика, иначе лечим одно мигание другим.
_PARTIAL_MARKER_RE = re.compile(
    r"\[(?:S(?:E(?:N(?:D(?:_(?:F(?:I(?:L(?:E(?::[^\]\n]*)?)?)?)?)?)?)?)?)?)?$",
    re.MULTILINE,
)
MAX_SEND_BYTES = 50 * 1024 * 1024  # предел Bot API на отправку документа ботом

# Имена, которые не отдаём НИКОГДА — даже лежащие внутри разрешённого корня.
# Список короткий и про секреты: разрастись он не должен, потому что каждый лишний шаблон
# отнимает у агента легальный файл (отказ виден человеку строкой в чате, но всё равно шум).
_DENY_GLOBS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.credentials.json",
    "id_*",  # id_rsa, id_ed25519 и их .pub
    "*.pem",
)
# `.git/config` держит токен приватного репо в URL remote — проверяется парой (parent, name)
_DENY_PAIRS: tuple[tuple[str, str], ...] = ((".git", "config"),)

Reason = Literal[
    "empty",  # маркер без пути
    "home",  # путь начинается с ~
    "outside",  # вне разрешённых корней
    "denied",  # секретное имя (deny-list) — независимо от корней
    "missing",  # файла нет
    "not_file",  # каталог/устройство/сокет
    "too_big",  # больше лимита Bot API
    "bad_path",  # NUL-байт, цикл симлинков, относительный путь без base
]


# Описание маркера для system-prompt (M-04). Живёт рядом с регекспом намеренно: правка
# формата маркера и правка того, что о нём знает модель обязаны ехать одним коммитом.
# Иначе получается ровно то, что нашло ревью: движок маркер вырезает и файл отправляет,
# а модель про маркер никогда не слышала — фича мертва, а примеры обещают «пришлю файлом».
SEND_FILE_INSTRUCTIONS = (
    "## Как отправить файл владельцу\n"
    "Чтобы отдать готовый файл (отчёт, картинку, документ), допиши в текст ответа маркер\n"
    "отдельной строкой:\n"
    "\n"
    "    [SEND_FILE:путь/к/файлу.pdf]\n"
    "\n"
    "Движок вырежет маркер из текста и отправит файл документом в Telegram. Правила:\n"
    "• один маркер — один файл; маркеров в ответе может быть несколько;\n"
    "• относительный путь считается от папки-мозга проекта (это твой cwd), абсолютный тоже "
    "можно;\n"
    "• отдавать можно только файлы из папки-мозга проекта и из каталога состояния инстанса "
    "(там лежат файлы, присланные владельцем);\n"
    "• секреты (`.env`, `*.credentials.json`, ключи, `*.pem`) движок не отдаст и напишет об "
    "этом человеку — не пробуй обойти это копированием под другим именем;\n"
    "• предел размера — 50 МБ.\n"
    "Без маркера файл не уйдёт: не обещай «прислал файлом», если маркера в ответе нет."
)


class SandboxError(Exception):
    """Путь не прошёл проверку. `reason` — машинный код, текст — для лога."""

    def __init__(self, reason: Reason, detail: str = ""):
        super().__init__(detail or reason)
        self.reason: Reason = reason
        self.detail = detail


@dataclass(frozen=True)
class SendFilePolicy:
    """Отдача файлов включена; `state_root` — второй корень проверки путей (§8.2).

    Одна конвенция косметики (K10): объект есть — фича включена, `None` — выключена флагом
    `.env`. Раньше «включено» и «а корень-то известен?» были двумя независимыми полями, и
    состояние «send_file_enabled=True, state_dir=None» было тихо-полурабочим.
    """

    state_root: Path


def extract_send_files(text: str) -> tuple[str, list[str]]:
    """Вырезать маркеры из текста. Возвращает (текст без маркеров, список сырых путей).

    Пустой маркер `[SEND_FILE:]` тоже вырезается, но в список не идёт: показывать человеку
    служебный мусор — хуже, чем молча его убрать.
    """
    paths: list[str] = []

    def take(match: re.Match[str]) -> str:
        raw = match.group(1).strip()
        if raw:
            paths.append(raw)
        return ""

    cleaned = _MARKER_RE.sub(take, text)
    # маркер часто стоит на своей строке — убираем осиротевшие пробелы/пустые строки
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip("\n"), paths


def hide_partial_marker(text: str) -> str:
    """Убрать из ЧЕРНОВИКА и готовые маркеры, и недописанный хвост `[SEND_FILE:...`.

    Псевдо-стриминг (§9) показывает текст по мере генерации, поэтому кадр «маркер ещё не
    закрыт `]`» — самый частый. Показывать его нельзя: человек видит служебную строку и
    вместе с ней абсолютный путь на сервере, а через секунду всё исчезает. Вырезаем и
    целые маркеры (тем же регекспом, что финал), и открытый хвост до конца строки.
    """
    cleaned, _ = extract_send_files(text)
    return _PARTIAL_MARKER_RE.sub("", cleaned).rstrip()


def is_denied_name(path: Path) -> bool:
    """Секретное имя из deny-list? Смотрим на РАЗРЕШЁННЫЙ путь, а не на то, что дал агент.

    Симлинк `readme.txt` → `.env` ловится именно здесь: `resolve()` уже вернул настоящее имя.
    """
    name = path.name
    if any(fnmatch.fnmatch(name, glob) for glob in _DENY_GLOBS):
        return True
    return any(path.parent.name == parent and name == leaf for parent, leaf in _DENY_PAIRS)


class FileSandbox:
    """Разрешённые корни для отдачи файлов. Пустой список корней = не отдаём ничего."""

    def __init__(
        self,
        roots: Sequence[str | Path],
        *,
        base: str | Path | None = None,
        max_bytes: int = MAX_SEND_BYTES,
    ):
        # Корни резолвим сразу: сам корень может быть симлинком (/tmp → /private/tmp на macOS),
        # и тогда сравнение «внутри корня» ложно провалилось бы на легальном файле.
        self._roots: list[Path] = []
        for root in roots:
            try:
                self._roots.append(Path(root).resolve())
            except (OSError, RuntimeError, ValueError) as exc:
                # K13: выпавший корень — не пустяк, а урезанная песочница. Молчать нельзя:
                # человек получит «файл вне разрешённых корней» на легальном файле и не
                # поймёт, почему. ValueError — NUL-байт в пути (Path.resolve его бросает).
                logger.warning("корень песочницы %r не резолвится, пропускаю: %s", str(root), exc)
        # base — от чего считать ОТНОСИТЕЛЬНЫЙ путь (cwd агента = папка-мозг, §5.2).
        # Явное поле вместо «первого корня» (K5): порядок списка корней был неявным
        # контрактом, который ломался при любой перестановке аргументов.
        self._base: Path | None = None
        if base is not None:
            try:
                self._base = Path(base).resolve()
            except (OSError, RuntimeError, ValueError) as exc:
                logger.warning("base песочницы %r не резолвится: %s", str(base), exc)
        self._max_bytes = max_bytes

    @property
    def roots(self) -> list[Path]:
        return list(self._roots)

    @property
    def base(self) -> Path | None:
        return self._base

    def resolve(self, raw: str) -> Path:
        """Проверить путь и вернуть реальный файл. Всё непрошедшее → SandboxError."""
        candidate = raw.strip()
        if not candidate:
            raise SandboxError("empty", "пустой путь")
        if "\x00" in candidate:
            raise SandboxError("bad_path", "в пути NUL-байт")
        if candidate.startswith("~"):
            # НЕ раскрываем ~ намеренно: это всегда попытка выйти в домашний каталог
            # (там ~/.claude/.credentials.json), а легальные пути живут в мозге и state/
            raise SandboxError("home", "домашние пути (~) не отдаём")
        if not self._roots:
            raise SandboxError("outside", "нет разрешённых корней")

        path = Path(candidate)
        if not path.is_absolute():
            if self._base is None:
                raise SandboxError("bad_path", "относительный путь, а базового каталога нет")
            path = self._base / path
        try:
            real = path.resolve()  # раскрывает и `..`, и симлинки — главная защита
        except (OSError, RuntimeError, ValueError) as exc:  # RuntimeError — цикл симлинков
            raise SandboxError("bad_path", f"путь не разрешается: {exc}") from exc

        if not any(real == root or real.is_relative_to(root) for root in self._roots):
            raise SandboxError("outside", f"{real} вне разрешённых корней")
        if is_denied_name(real):
            raise SandboxError("denied", f"{real.name} в deny-list секретных имён")
        if not real.exists():
            raise SandboxError("missing", f"{real} не существует")
        if not real.is_file():
            raise SandboxError("not_file", f"{real} не обычный файл")
        size = real.stat().st_size
        if size > self._max_bytes:
            raise SandboxError("too_big", f"{size} байт больше лимита {self._max_bytes}")
        return real


_REASON_TEXT: dict[Reason, str] = {
    "empty": "путь пустой",
    "home": "путь ведёт в домашний каталог — такие файлы я не отдаю",
    "outside": "файл вне папки проекта и состояния агента — не отдаю по правилам безопасности",
    "denied": "такие файлы я не отдаю никогда (секреты и ключи)",
    "missing": "такого файла нет",
    "not_file": "это не файл (каталог или устройство)",
    "too_big": "файл больше лимита Telegram на отправку",
    "bad_path": "путь битый",
}


def blocked_message(raw: str, error: SandboxError) -> str:
    """Строка человеку вместо файла. Несостоявшаяся отдача — не повод ронять ответ (§9)."""
    return f"📎 Не отправил «{raw}»: {_REASON_TEXT.get(error.reason, 'путь не прошёл проверку')}."
