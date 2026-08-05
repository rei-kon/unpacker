"""Общая обвязка канареек.

Канарейка — не юнит-тест. Она проверяет ЧУЖОЕ поведение (Claude Agent SDK), а не наш код,
и потому требует живого окружения: `claude` CLI, токен подписки, Linux. В CI не гоняется.

Правило (PLAN.md §12): красная канарейка НЕ чинится подгонкой ассерта. Она означает,
что допущение плана ложно — фиксируем факт в docs/canaries/ и правим план. НО прежде
чем звать красное приговором, канарейка обязана исключить, что покраснела по своей вине
(потерянный маркер, настроение модели, чужой конфиг). Отсюда всё, что ниже.

Три вещи, которые эта обвязка обеспечивает всем канарейкам разом:
  1. Сборщик потока `collect_stream` — видит ВЕСЬ текст и ВСЕ tool-вызовы, а не только
     финал (ядровой сборщик событий прячет tool_use — канарейке это слепота).
  2. `canary_options` — режет опасный тулинг (Read/Bash/…): и безопасность на живом VPS,
     и провенанс (маркер, добытый не тем путём, — не доказательство).
  3. Гейт, который не проходит ТИХО: skip не должен выглядеть как успех (это худший
     провал критерия приёмки Фазы 0 — «канареек не было, а фаза принята»).
"""

from __future__ import annotations

import asyncio
import os
import platform
import re
import shutil
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

# Таймаут по умолчанию на сбор одного ответа: канарейки ждут реальных ответов модели.
DEFAULT_TIMEOUT = 180.0

# Тулы, которые канареечному агенту не нужны НИКОГДА и опасны на живом VPS под bypassPermissions.
# disallowed_tools переживает bypassPermissions (docstring SDK: «removed from the model's
# context and cannot be used, even if they would otherwise be allowed»). Отрезая файловый
# путь, мы заодно делаем маркерные канарейки доказательными: маркер из .md-файла больше
# не добыть через Read/Glob — только через проверяемый механизм.
DANGEROUS_TOOLS = [
    "Read",
    "Write",
    "Edit",
    "NotebookEdit",
    "Glob",
    "Grep",
    "Bash",
    "BashOutput",
    "KillShell",
    "WebFetch",
    "WebSearch",
    # Субагент: в SDK 0.2.121 тул зовётся Agent (не Task) — наследует bypassPermissions,
    # свой контекст без «будь безобиден». Режем оба имени; C4b разрешает явно через allow.
    "Agent",
    "Task",
]


# ── определение окружения ──────────────────────────────────────────────────────


def _resolve_cli() -> tuple[str | None, str]:
    """Вернуть путь к CLI, которым РЕАЛЬНО запустится SDK, и его версию.

    Критично: SDK берёт ВСТРОЕННЫЙ бинарник первым (_find_cli: «First, check for bundled
    CLI»), и только потом PATH. Гейт на `which('claude')` проверял бы не тот бинарник, а в
    отчёт уехала бы чужая версия — при том что суть C3 именно в фиксе на стороне CLI.
    """
    try:
        import claude_agent_sdk

        pkg = Path(claude_agent_sdk.__file__).parent
        bundled = pkg / "_bundled" / "claude"
        if bundled.exists():
            version = "unknown"
            try:
                from claude_agent_sdk._internal import _cli_version  # type: ignore

                version = getattr(_cli_version, "__cli_version__", "unknown")
            except Exception:  # noqa: BLE001
                pass
            return str(bundled), f"{version} (bundled)"
    except Exception:  # noqa: BLE001
        pass
    system = shutil.which("claude")
    return system, "system-PATH (version unread)"


def _auth_context() -> str | None:
    """Какой контур auth активен: 'subscription' | 'api' | None.

    Возвращает контур, а не булеву «есть/нет»: смысл факта зависит от контура (§8.1).
    total_cost_usd под подпиской может быть 0.0, под API — всегда >0 — путать нельзя.
    """
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return "subscription"
    cfg = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
    if (cfg / ".credentials.json").exists():
        return "subscription"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "api"
    return None


_CLI_PATH, _CLI_VERSION = _resolve_cli()
_AUTH = _auth_context()
_STRICT = os.environ.get("CANARY_STRICT") == "1"


def auth_context() -> str | None:
    """Публичный доступ к активному контуру auth ('subscription'|'api'|None) для канареек."""
    return _AUTH


def _gate_reason() -> str | None:
    if _CLI_PATH is None:
        return "не найден claude CLI (ни встроенный в SDK, ни в PATH)"
    if _AUTH is None:
        return "нет auth (ни CLAUDE_CODE_OAUTH_TOKEN, ни credentials, ни ANTHROPIC_API_KEY)"
    return None


def _skip_or_fail() -> Any:
    """Гейт живого SDK. В обычном режиме — skip; в CANARY_STRICT — падение.

    Зачем strict: на VPS прогон ОБЯЗАН состояться. Тихий skip → pytest exit 0 → `make canary`
    зелёный → критерий «канарейки прогнаны» закрыт нулём фактов. CANARY_STRICT=1 запрещает
    этот тихий успех: нет окружения — громкая ошибка, а не зелёный прочерк.
    """
    reason = _gate_reason()
    if reason is None:
        return pytest.mark.skipif(False, reason="")
    if _STRICT:
        return pytest.mark.skip(
            reason=f"CANARY_STRICT: {reason}"
        )  # см. pytest_collection_modifyitems
    return pytest.mark.skipif(
        True, reason=f"нужен живой SDK: {reason} — гоняй на VPS (make canary)"
    )


requires_live_sdk = _skip_or_fail()

# Канарейка про Linux-баг (#268) не должна «проходить» на macOS — платформа часть факта.
requires_linux = pytest.mark.skipif(
    sys.platform != "linux",
    reason=f"факт привязан к Linux (баг #268), текущая платформа {sys.platform}",
)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """В CANARY_STRICT падать, если весь прогон скипнулся — skip не должен читаться как успех."""
    if not _STRICT:
        return
    reason = _gate_reason()
    if reason is not None:
        pytest.exit(f"CANARY_STRICT и нет живого SDK: {reason}. Прогон не состоялся.", returncode=3)


# ── редакция секретов и печать фактов ──────────────────────────────────────────

_SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_-]+"),
    re.compile(r"\b\d{8,}:[A-Za-z0-9_-]{35}\b"),  # токен Telegram-бота
    re.compile(r"(?i)(oauth|token|api[_-]?key|secret)[\"'=:\s]+[A-Za-z0-9_\-.]{12,}"),
]


def _redact(text: str) -> str:
    for pat in _SECRET_PATTERNS:
        text = pat.sub("[REDACTED]", text)
    # Плюс прямые значения из окружения — если токен пилота утёк в вывод, вырезаем по значению
    for key in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN"):
        val = os.environ.get(key)
        if val and len(val) >= 8:
            text = text.replace(val, "[REDACTED]")
    return text


_HEADER_PRINTED = False


def _print_header_once() -> None:
    global _HEADER_PRINTED
    if _HEADER_PRINTED:
        return
    _HEADER_PRINTED = True
    try:
        import claude_agent_sdk

        sdk_ver = getattr(claude_agent_sdk, "__version__", "unknown")
    except Exception:  # noqa: BLE001
        sdk_ver = "unknown"
    print(
        "\n[КАНАРЕЙКИ · окружение] "
        f"platform={platform.platform()} | cli={_CLI_PATH} ({_CLI_VERSION}) | "
        f"sdk={sdk_ver} | auth_context={_AUTH}"
    )


def write_results_note(name: str, fact: str) -> None:
    """Печать факта в stdout прогона (pytest -s) — сырьё для docs/canaries/. Секреты вырезаны."""
    _print_header_once()
    print(f"\n[КАНАРЕЙКА {name}] ФАКТ: {_redact(fact)}")


# ── сборщик потока для канареек ─────────────────────────────────────────────────


@dataclass
class CanaryStream:
    """Всё, что канарейке нужно видеть в потоке — включая улики tool-вызовов."""

    full_text: str = ""
    tool_uses: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    session_id: str | None = None
    result: Any = None

    def used_tool(self, name: str) -> bool:
        return any(n == name for n, _ in self.tool_uses)


def _is_result(message: Any) -> bool:
    return type(message).__name__ == "ResultMessage" or hasattr(message, "total_cost_usd")


async def collect_stream(
    messages: AsyncIterator[Any], timeout: float = DEFAULT_TIMEOUT
) -> CanaryStream:
    """Собрать ВЕСЬ текст ассистента + ВСЕ tool-вызовы + session_id + ResultMessage.

    Отличие от ядрового сборщика (`engine/core/events.py` отдаёт человеку только финал и
    прячет tool_use — правильно для чата, слепо для канарейки).
    """
    out = CanaryStream()

    async def _drain() -> None:
        async for message in messages:
            sid = getattr(message, "session_id", None)
            if isinstance(sid, str) and sid:
                out.session_id = sid
            content = getattr(message, "content", None)
            if content is not None:
                for block in content:
                    text = getattr(block, "text", None)
                    if isinstance(text, str):
                        out.full_text += text
                    if type(block).__name__ == "ToolUseBlock":
                        out.tool_uses.append(
                            (getattr(block, "name", "?"), getattr(block, "input", {}) or {})
                        )
            if _is_result(message):
                out.result = message
                return

    await asyncio.wait_for(_drain(), timeout=timeout)
    return out


# ── опции ──────────────────────────────────────────────────────────────────────


def canary_options(brain: Path, *, allow: tuple[str, ...] = (), **overrides: Any) -> Any:
    """ClaudeAgentOptions для канарейки: bypassPermissions, но с отрезанным опасным тулингом.

    `allow` — тулы из DANGEROUS_TOOLS, которые именно этой канарейке нужны (напр. C4b — Task).
    Всё прочее опасное отрезано: защита живого VPS + провенанс маркера.
    Дефолтный setting_sources=['user','project'] — но C1 гоняет обе ветки (skills vs sources).
    """
    from claude_agent_sdk import ClaudeAgentOptions

    disallowed = [t for t in DANGEROUS_TOOLS if t not in allow]
    kwargs: dict[str, Any] = {
        "cwd": str(brain),
        "permission_mode": "bypassPermissions",
        "setting_sources": ["user", "project"],
        "disallowed_tools": disallowed,
        "max_turns": 6,
    }
    kwargs.update(overrides)
    return ClaudeAgentOptions(**kwargs)


# ── фикстуры мозгов ──────────────────────────────────────────────────────────────

_TERSE = "Отвечай максимально коротко, одной строкой, без пояснений.\n"


@pytest.fixture
def brain(tmp_path: Path) -> Path:
    """Минимальный валидный мозг: один CLAUDE.md (§4: обязательный минимум мозга)."""
    brain_dir = tmp_path / "brain"
    brain_dir.mkdir()
    (brain_dir / "CLAUDE.md").write_text(
        f"# Канареечный агент\n\nТы — тестовый агент. {_TERSE}", encoding="utf-8"
    )
    return brain_dir


@pytest.fixture
def brain_talky(tmp_path: Path) -> Path:
    """Мозг БЕЗ установки «отвечай коротко» — для C7, где нужна длинная генерация.

    В обычном brain «одной строкой» воюет с промптом «считай до 500»: модель слушается
    личности, отвечает за 2с, прерывать нечего — канарейка ложно-зелёная.
    """
    brain_dir = tmp_path / "brain-talky"
    brain_dir.mkdir()
    (brain_dir / "CLAUDE.md").write_text(
        "# Канареечный агент\n\nТы — тестовый агент. Выполняй инструкции пользователя буквально.\n",
        encoding="utf-8",
    )
    return brain_dir


@pytest.fixture
def brain_with_skill(brain: Path) -> Path:
    """Мозг со скиллом — под C1. Скилл в .claude/skills/ САМОГО МОЗГА, не в плагинах (#509).

    Маркер намеренно НЕ хранится в SKILL.md как готовая строка ответа: агент должен его
    ВЫЧИСЛИТЬ по инструкции скилла, иначе даже при отрезанном Read остаётся лазейка «увидел
    строку — повторил». Инструкция требует собрать маркер из частей.
    """
    skill_dir = brain / ".claude" / "skills" / "canary-ping"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: canary-ping\n"
        "description: Канареечный скилл. Используй когда просят сделать ping.\n"
        "---\n\n"
        "# canary-ping\n\n"
        "Когда просят ping, склей без пробелов и дефисов слово CANARY, слово PONG и код 7F3A,\n"
        "разделяя дефисами, и ответь ровно получившейся строкой, больше ничего.\n",
        encoding="utf-8",
    )
    return brain
