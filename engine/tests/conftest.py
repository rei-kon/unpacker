"""Общие фикстуры и хелперы тестов движка (M9/S1).

Раньше session-фикстура `isolated_runtime` жила в `test_deploy_scripts.py` и растаскивалась
по другим модулям через `from ... import isolated_runtime as isolated_runtime`, а `run_deploy`
существовал в трёх копиях с разными подписями. Любая правка окружения деплоя расходилась
между копиями молча. Здесь — один источник: фикстуры видит pytest сам (conftest), хелперы
импортируются явно (`from engine.tests.conftest import run_deploy`).

Дисциплина: тесты НИЧЕГО не мутируют на хосте. Всё, что деплой-скрипты пишут в систему
(юниты, drop-in, sudoers), перенаправляется env-переключателями в tmp — иначе на Linux-CI
с passwordless sudo тесты ставили бы юниты в /etc/systemd/system (H6).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from engine.tests._tgapi_stub import tg_api_stub

REPO = Path(__file__).resolve().parents[2]
DEPLOY = REPO / "deploy" / "deploy.sh"
AGENTCTL = REPO / "deploy" / "agentctl.sh"
INSTALL_SUDOERS = REPO / "deploy" / "install-sudoers.sh"
ME = os.environ.get("USER", "")
GOOD_TOKEN = "123456:AAbbCC-dd_ee"

UV = (
    os.environ.get("UV_BIN")
    or subprocess.run(
        ["bash", "-lc", "command -v uv"], capture_output=True, text=True
    ).stdout.strip()
)


# ── фикстуры ────────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def api_base() -> Iterator[str]:
    """URL заглушки Bot API — подставляется в TG_API_BASE (гейт getMe ходит настоящим curl)."""
    with tg_api_stub() as base:
        yield base


@pytest.fixture(scope="session")
def isolated_runtime(tmp_path_factory) -> str:
    """Изолированная копия движка как RUNTIME.

    deploy.sh зовёт `uv run --directory $RUNTIME` (мост кнопок, сид). Против рабочего репо
    это трогало бы общий `.venv` (ruff/mypy/pytest). Копируем код в tmp с собственным venv —
    тесты не трогают рабочее окружение.
    """
    if not UV:
        pytest.skip("нужен uv")
    dst = tmp_path_factory.mktemp("runtime") / "unpacker"
    shutil.copytree(
        REPO,
        dst,
        ignore=shutil.ignore_patterns(
            ".venv",
            ".git",
            "*_cache",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
            "__pycache__",
        ),
    )
    subprocess.run([UV, "sync", "--project", str(dst)], check=True, capture_output=True)
    return str(dst)


# ── запуск скриптов чёрным ящиком ───────────────────────────────────────────


def _env(env_extra: dict[str, str] | None) -> dict[str, str]:
    env = {**os.environ}
    if env_extra:
        env.update(env_extra)
    return env


def run_deploy(
    *args: str, env_extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(DEPLOY), *args], capture_output=True, text=True, env=_env(env_extra)
    )


def run_agentctl(
    *args: str, env_extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(AGENTCTL), *args], capture_output=True, text=True, env=_env(env_extra)
    )


def run_installer(
    *args: str, env_extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(INSTALL_SUDOERS), *args], capture_output=True, text=True, env=_env(env_extra)
    )


def out_of(r: subprocess.CompletedProcess[str]) -> str:
    """stdout+stderr в нижнем регистре — для ассертов на текст отказа."""
    return (r.stdout + r.stderr).lower()


# ── фикстуры-данные: мозг, окружение деплоя, стабы системных команд ─────────


def make_brain(tmp_path: Path, name: str = "brainsrc") -> Path:
    """Минимальная папка-мозг: один CLAUDE.md (§4 — минимум личности)."""
    b = tmp_path / name
    b.mkdir(exist_ok=True)
    (b / "CLAUDE.md").write_text("# Тест-мозг\nТы тест.\n", encoding="utf-8")
    return b


def deploy_env(
    tmp_path: Path, api: str, runtime: str | None = None, **extra: str
) -> dict[str, str]:
    """Окружение деплоя, полностью замкнутое в tmp.

    TG_UNIT_DIR/TG_DROPIN_DIR/TG_SYSTEMCTL нужны, чтобы на Linux (где systemctl есть)
    тест не ставил юнит в /etc/systemd/system и не звал `enable --now` (H6).
    """
    e = {
        "TG_RUN_USER": ME,
        "TG_AGENTS_BASE": str(tmp_path / "agents"),
        "TG_BRAINS_BASE": str(tmp_path / "brains"),
        "TG_RUNTIME": runtime or str(REPO),
        "TG_API_BASE": api,
        "TG_UNIT_DIR": str(tmp_path / "systemd"),
        "TG_DROPIN_DIR": str(tmp_path / "systemd"),
        "UNPACKER_SUDOERS_DIR": str(tmp_path / "sudoers.d"),
        # engine.conf хоста в тестах не читаем: у машины разработчика он может существовать
        "UNPACKER_ENGINE_CONF": str(tmp_path / "no-such-engine.conf"),
    }
    if UV:
        e["TG_UV_BIN"] = UV
    e.update(extra)
    return e


def deploy_args(tmp_path: Path, name: str, *rest: str, brain: Path | None = None) -> list[str]:
    """Аргументы деплоя с валидными дефолтами; `rest` дописывает/переопределяет флаги."""
    return [
        "--surface",
        "tg",
        "--name",
        name,
        "--token",
        GOOD_TOKEN,
        "--users",
        "111",
        "--brain",
        str(brain if brain is not None else make_brain(tmp_path)),
        "--project-slug",
        name,
        *rest,
    ]


def stub_bin(tmp_path: Path, name: str, body: str) -> Path:
    """Исполняемая заглушка в tmp/bin. Возвращает путь до неё (не в PATH — путь передаём явно)."""
    d = tmp_path / "bin"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_text(body, encoding="utf-8")
    p.chmod(0o755)
    return p


SYSTEMCTL_STUB = """#!/usr/bin/env bash
# Заглушка systemctl: пишет argv в $SYSTEMCTL_LOG и всегда успешна.
printf '%s\\n' "$*" >> "${SYSTEMCTL_LOG:-/dev/null}"
exit 0
"""

CLAUDE_STUB = """#!/usr/bin/env bash
echo "1.0.0 (Claude Code)"
"""


@contextmanager
def module_hidden(path: Path) -> Iterator[None]:
    """Временно спрятать файл модуля (сценарий «в движке ученика этой части ещё нет»).

    Session-runtime общий, поэтому восстановление обязательно и идёт через finally.
    """
    hidden = path.with_suffix(path.suffix + ".hidden")
    path.rename(hidden)
    try:
        yield
    finally:
        hidden.rename(path)
