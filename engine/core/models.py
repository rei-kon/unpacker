"""Типы слоя данных §5.2 — простые dataclass, не ORM.

Три сущности модели: project (папка-мозг), session (история диалога), binding (окно
поверхности в сессию). Store читает строки SQLite и отдаёт эти объекты наружу.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ProjectStatus = Literal["active", "disabled"]
SessionStatus = Literal["active", "stopped", "closed"]
Surface = Literal["tg", "web"]


@dataclass(frozen=True)
class Project:
    """Папка-мозг. cwd движка = brain_path (§5.2: cwd жёстко отсюда, не гадаем)."""

    slug: str
    name: str
    brain_path: str
    default_model: str | None
    status: ProjectStatus


@dataclass(frozen=True)
class Session:
    """Непрерывная история диалога с проектом.

    id — UUIDv4 (непредсказуемый, §8.2). claude_session_id хранит хэндл SDK для resume и
    ПЕРЕПИСЫВАЕТСЯ из каждого ответа (урок канарейки C2a).
    """

    id: str
    owner_user_id: int
    project_slug: str
    claude_session_id: str | None
    status: SessionStatus
    model: str | None
    verbose: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Binding:
    """Окно поверхности в сессию: топик ТГ / чат веб. Разные окна могут смотреть в одну сессию."""

    surface: Surface
    surface_key: str
    session_id: str
