"""Общая обвязка канареек.

Канарейка — не юнит-тест. Она проверяет ЧУЖОЕ поведение (Claude Agent SDK), а не наш код,
и потому требует живого окружения: `claude` CLI, токен подписки, Linux. В CI не гоняется.

Правило (PLAN.md §12): красная канарейка НЕ чинится подгонкой ассерта. Она означает,
что допущение плана ложно — фиксируем факт в docs/canaries/ и правим план.
"""
from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

# Канарейки жгут живую подписку и ждут реальных ответов модели — секунды, иногда минуты.
DEFAULT_TIMEOUT = 180.0


def _has_claude_cli() -> bool:
    return shutil.which("claude") is not None


def _has_auth() -> bool:
    """Токен подписки: либо в окружении, либо в ~/.claude/.credentials.json от `claude setup-token`."""
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return True
    return (Path.home() / ".claude" / ".credentials.json").exists()


requires_live_sdk = pytest.mark.skipif(
    not (_has_claude_cli() and _has_auth()),
    reason="нужен живой claude CLI + токен подписки — гоняй на VPS (make canary), не на маке",
)


@pytest.fixture
def brain(tmp_path: Path) -> Path:
    """Минимальный валидный мозг: одна папка с CLAUDE.md.

    §4: обязательный минимум мозга — один CLAUDE.md. Личность подхватывается через cwd.
    """
    brain_dir = tmp_path / "brain"
    brain_dir.mkdir()
    (brain_dir / "CLAUDE.md").write_text(
        "# Канареечный агент\n\n"
        "Ты — тестовый агент. Отвечай максимально коротко, одной строкой, без пояснений.\n",
        encoding="utf-8",
    )
    return brain_dir


@pytest.fixture
def brain_with_skill(brain: Path) -> Path:
    """Мозг со скиллом — под C1 (видны ли скиллы мозга на Linux, риск #268).

    Скилл кладём в .claude/skills/ САМОГО МОЗГА, а не в плагины: плагинные скиллы
    ловят отдельный баг (#509), мы такой путь не используем by design (§13).
    """
    skill_dir = brain / ".claude" / "skills" / "canary-ping"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: canary-ping\n"
        "description: Канареечный скилл. Используй когда просят сделать ping.\n"
        "---\n\n"
        "# canary-ping\n\n"
        "Когда тебя просят сделать ping — ответь ровно одним словом: CANARY-PONG-7F3A\n",
        encoding="utf-8",
    )
    return brain


def write_results_note(name: str, fact: str) -> None:
    """Печать факта в stdout прогона (pytest -s) — сырьё для docs/canaries/."""
    print(f"\n[КАНАРЕЙКА {name}] ФАКТ: {fact}")
