"""Отдача файлов агентом: маркер `[SEND_FILE:путь]` + песочница путей — §9, §8.2.

Механизм отдачи в движке ОДИН (§9, §14): агент пишет в текст ответа маркер, движок вырезает
его и отправляет файл документом. MCP-тулы под это не заводим — лишняя поверхность.

Плата за простоту: путь диктует МОДЕЛЬ. А модель читает недоверенные файлы (uploads) и чужие
мозги, то есть путь — недоверенный ввод даже когда «агент свой». Поэтому разрешены ровно два
корня: папка-мозг проекта и `state/` инстанса (там же uploads — файл, пришедший от владельца,
можно вернуть). Всё остальное — блок с понятной строкой в чат:

  • `/etc/passwd`, `/root/.ssh/id_rsa`      — абсолютный путь наружу;
  • `../../.env`                            — токен бота инстанса;
  • `~/.claude/.credentials.json`           — токен подписки владельца;
  • симлинк из мозга на файл снаружи        — легальное имя, чужая цель.

Проверка одна и надёжная: `Path.resolve()` (раскрывает `..` И симлинки) обязан лежать внутри
одного из корней. Проверять текст пути на «..» — путь в никуда: симлинки так не поймать.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

# Маркер намеренно однострочный и без `]` внутри: жадный матч через абзац съел бы полответа.
_MARKER_RE = re.compile(r"\[SEND_FILE:([^\]\n]*)\]")
MAX_SEND_BYTES = 50 * 1024 * 1024  # предел Bot API на отправку документа ботом

Reason = str  # "empty" | "home" | "outside" | "missing" | "not_file" | "too_big" | "bad_path"


class SandboxError(Exception):
    """Путь не прошёл песочницу. `reason` — машинный код, текст — для лога."""

    def __init__(self, reason: Reason, detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


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


class FileSandbox:
    """Разрешённые корни для отдачи файлов. Пустой список корней = не отдаём ничего."""

    def __init__(self, roots: Sequence[str | Path], *, max_bytes: int = MAX_SEND_BYTES):
        # Корни резолвим сразу: сам корень может быть симлинком (/tmp → /private/tmp на macOS),
        # и тогда сравнение «внутри корня» ложно провалилось бы на легальном файле.
        self._roots: list[Path] = []
        for root in roots:
            try:
                self._roots.append(Path(root).resolve())
            except OSError:
                continue
        self._max_bytes = max_bytes

    @property
    def roots(self) -> list[Path]:
        return list(self._roots)

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
            # cwd агента = папка-мозг (§5.2), поэтому относительный путь считаем от неё
            path = self._roots[0] / path
        try:
            real = path.resolve()  # раскрывает и `..`, и симлинки — главная защита
        except (OSError, RuntimeError) as exc:  # RuntimeError — цикл симлинков
            raise SandboxError("bad_path", f"путь не разрешается: {exc}") from exc

        if not any(real == root or real.is_relative_to(root) for root in self._roots):
            raise SandboxError("outside", f"{real} вне разрешённых корней")
        if not real.exists():
            raise SandboxError("missing", f"{real} не существует")
        if not real.is_file():
            raise SandboxError("not_file", f"{real} не обычный файл")
        size = real.stat().st_size
        if size > self._max_bytes:
            raise SandboxError("too_big", f"{size} байт больше лимита {self._max_bytes}")
        return real


_REASON_TEXT = {
    "empty": "путь пустой",
    "home": "путь ведёт в домашний каталог — такие файлы я не отдаю",
    "outside": "файл вне папки проекта и состояния агента — не отдаю по правилам безопасности",
    "missing": "такого файла нет",
    "not_file": "это не файл (каталог или устройство)",
    "too_big": "файл больше лимита Telegram на отправку",
    "bad_path": "путь битый",
}


def blocked_message(raw: str, error: SandboxError) -> str:
    """Строка человеку вместо файла. Несостоявшаяся отдача — не повод ронять ответ (§9)."""
    return f"📎 Не отправил «{raw}»: {_REASON_TEXT.get(error.reason, 'путь не прошёл проверку')}."
