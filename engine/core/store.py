"""Слой данных §5.2 — единая writer-точка поверх SQLite.

Одна `Store` держит одно соединение и один RLock. Все операции (чтение и запись) идут через
этот lock — «единая writer-точка» §5.2 как ФАКТ, а не намерение: без него check_same_thread
=False позволил бы двум потокам переплести commit на общем соединении (ревью корректности).

Модель — три сущности: project (папка-мозг) · session (история, id=UUIDv4) · binding
(окно поверхности в сессию). resume завязан на cwd = projects.brain_path (канарейка C2b:
чужой cwd рушится с ProcessError, поэтому cwd берём только отсюда).

Инварианты, которые держит слой (ревью Фазы 1a):
- `resolve()` отдаёт окно ТОЛЬКО активной сессии — closed/stopped не резюмятся молча;
- перевязка окна на новую сессию закрывает осиротевшую старую (иначе пул Среза B держит
  брошенные клиенты тёплыми);
- переходы статуса валидируются (нет воскрешения closed→active, нет мусорных статусов);
- `claude_session_id` переписывается из каждого ответа (урок C2a).

НЕ решает в слое данных (задокументировано для Среза B):
- гонку `claude_session_id` между двумя окнами одной сессии — её закрывает per-session lock
  движка (§5.1): одна сессия обрабатывается по одному сообщению за раз;
- проверку owner при resolve (групповые чаты, usage per-user) — групповой контур в фазе 2.
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import UTC, datetime

# паттерн slug — один на движок (K17): раньше он был выписан и здесь, и в brain.py
from engine.core.models import SLUG_PATTERN, SLUG_RE, Binding, Project, Session, Surface

_BUSY_TIMEOUT_MS = 5000
_SCHEMA_VERSION = 1
_SESSION_STATUSES = ("active", "stopped", "closed")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _new_uuid() -> str:
    return str(uuid.uuid4())


_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    slug          TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    brain_path    TEXT NOT NULL,
    default_model TEXT,
    status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled'))
);
CREATE TABLE IF NOT EXISTS sessions (
    id                TEXT PRIMARY KEY,
    owner_user_id     INTEGER NOT NULL,
    project_slug      TEXT NOT NULL REFERENCES projects(slug),
    claude_session_id TEXT,
    status            TEXT NOT NULL DEFAULT 'active'
                      CHECK (status IN ('active','stopped','closed')),
    model             TEXT,
    verbose           INTEGER NOT NULL DEFAULT 0 CHECK (verbose BETWEEN 0 AND 3),
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bindings (
    surface     TEXT NOT NULL CHECK (surface IN ('tg','web')),
    surface_key TEXT NOT NULL,
    session_id  TEXT NOT NULL REFERENCES sessions(id),
    PRIMARY KEY (surface, surface_key)
);
CREATE TABLE IF NOT EXISTS usage (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT NOT NULL REFERENCES sessions(id),
    ts            TEXT NOT NULL,
    model         TEXT,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_usage_session ON usage(session_id);
"""


class Store:
    def __init__(self, path: str):
        # check_same_thread=False — aiogram гоняет хендлеры в одном loop, но на разных задачах;
        # реальную сериализацию даёт _lock ниже, а не это (см. модуль-docstring).
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

        self.projects = ProjectStore(self._conn, self._lock)
        self.sessions = SessionStore(self._conn, self._lock)
        self.bindings = BindingStore(self._conn, self._lock)
        self.usage = UsageStore(self._conn, self._lock)

    def _migrate(self) -> None:
        """Диспетчер миграций по PRAGMA user_version (DM-1).

        Сейчас тело почти пустое: схема — версия 1. Когда в фазе 2 понадобится ALTER, шаги
        кладутся сюда под `if version < N`, и update.sh получает детектируемую, обратимую точку
        версии. Без этого якоря `CREATE TABLE IF NOT EXISTS` не эволюционирует заполненную БД.
        """
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        # if version < 2: <ALTER ...>; version = 2
        if version < _SCHEMA_VERSION:
            self._conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class _SubStore:
    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock):
        self._conn = conn
        self._lock = lock


class ProjectStore(_SubStore):
    def create(
        self, *, slug: str, name: str, brain_path: str, default_model: str | None = None
    ) -> Project:
        if not SLUG_RE.match(slug):
            raise ValueError(f"slug должен быть {SLUG_PATTERN}, получено: {slug!r}")
        with self._lock:
            self._conn.execute(
                "INSERT INTO projects (slug, name, brain_path, default_model, status) "
                "VALUES (?, ?, ?, ?, 'active')",
                (slug, name, brain_path, default_model),
            )
            self._conn.commit()
        return self.get(slug)  # type: ignore[return-value]

    def get(self, slug: str) -> Project | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM projects WHERE slug = ?", (slug,)).fetchone()
        return _project(row) if row else None

    def list_active(self) -> list[Project]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM projects WHERE status = 'active' ORDER BY slug"
            ).fetchall()
        return [_project(r) for r in rows]

    def disable(self, slug: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE projects SET status = 'disabled' WHERE slug = ?", (slug,))
            self._conn.commit()


class SessionStore(_SubStore):
    # whitelist: column приходит ТОЛЬКО из set_*-методов ниже, но фиксируем явно, чтобы будущий
    # set_field(column_from_input) не открыл SQL-инъекцию в f-string _set_locked.
    _ALLOWED_COLUMNS = frozenset({"claude_session_id", "status", "model", "verbose"})

    def create(self, *, owner_user_id: int, project_slug: str) -> Session:
        with self._lock:
            project = self._conn.execute(
                "SELECT default_model FROM projects WHERE slug = ? AND status = 'active'",
                (project_slug,),
            ).fetchone()
            if project is None:
                # нет проекта ИЛИ он disabled — новую сессию не заводим
                raise sqlite3.IntegrityError(f"нет активного проекта: {project_slug!r}")
            sid = _new_uuid()
            now = _now()
            self._conn.execute(
                "INSERT INTO sessions "
                "(id, owner_user_id, project_slug, claude_session_id, status, model, verbose, "
                " created_at, updated_at) "
                "VALUES (?, ?, ?, NULL, 'active', ?, 0, ?, ?)",
                (sid, owner_user_id, project_slug, project["default_model"], now, now),
            )
            self._conn.commit()
        return self.get(sid)  # type: ignore[return-value]

    def get(self, session_id: str) -> Session | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return _session(row) if row else None

    def set_claude_session_id(self, session_id: str, claude_session_id: str) -> None:
        # Переписываем из каждого ответа (урок C2a) — иначе 2-й рестарт теряет историю.
        # Сериализацию двух окон одной сессии обеспечивает per-session lock движка (§5.1).
        self._touch(session_id, "claude_session_id", claude_session_id)

    def clear_claude_session_id(self, session_id: str) -> None:
        """Забыть handle сессии CLI: зовётся, когда прошлый оказался мёртв и новый не приехал —
        держать битый id значит гарантированно упереться в него следующим сообщением."""
        self._touch(session_id, "claude_session_id", None)

    def set_status(self, session_id: str, status: str) -> None:
        if status not in _SESSION_STATUSES:
            raise ValueError(f"недопустимый статус: {status!r}")
        if status == "closed":
            # закрытие только через close() — оно ещё и отвязывает окна (инвариант §5.2)
            raise ValueError("status='closed' ставится только через close()")
        with self._lock:
            current = self._conn.execute(
                "SELECT status FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if current and current["status"] == "closed":
                # closed — терминальный: воскрешение запрещено (реанимация — новой сессией)
                raise ValueError("closed-сессия не воскрешается — заведи новую сессию")
            self._set_locked(session_id, "status", status)

    def set_model(self, session_id: str, model: str) -> None:
        self._touch(session_id, "model", model)

    def set_verbose(self, session_id: str, verbose: int) -> None:
        if not 0 <= verbose <= 3:
            raise ValueError(f"verbose вне диапазона 0..3: {verbose}")
        self._touch(session_id, "verbose", verbose)

    def close(self, session_id: str) -> None:
        # closed + отвязать все окна одной транзакцией: реанимация только новой сессией (§5.2)
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET status = 'closed', updated_at = ? WHERE id = ?",
                (_now(), session_id),
            )
            self._conn.execute("DELETE FROM bindings WHERE session_id = ?", (session_id,))
            self._conn.commit()

    def _touch(self, session_id: str, column: str, value: object) -> None:
        with self._lock:
            self._set_locked(session_id, column, value)

    def _set_locked(self, session_id: str, column: str, value: object) -> None:
        if column not in self._ALLOWED_COLUMNS:
            raise ValueError(f"колонка вне whitelist: {column!r}")
        self._conn.execute(
            f"UPDATE sessions SET {column} = ?, updated_at = ? WHERE id = ?",
            (value, _now(), session_id),
        )
        self._conn.commit()


class BindingStore(_SubStore):
    def bind(self, surface: Surface, surface_key: str, session_id: str) -> None:
        """Привязать окно к сессии. Целевая сессия обязана быть active.

        При перевязке окна на новую сессию старая, если осталась без единого окна, помечается
        stopped — иначе она «сирота»: пул Среза B держал бы её тёплой, а ни одно окно её не
        адресует (находка adversarial-ревью).
        """
        with self._lock:
            target = self._conn.execute(
                "SELECT status FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if target is None:
                raise sqlite3.IntegrityError(f"нет сессии: {session_id!r}")
            if target["status"] != "active":
                raise ValueError(
                    f"нельзя привязать окно к сессии в статусе {target['status']!r} — только active"
                )
            old = self._conn.execute(
                "SELECT session_id FROM bindings WHERE surface = ? AND surface_key = ?",
                (surface, surface_key),
            ).fetchone()
            old_session_id = old["session_id"] if old else None

            self._conn.execute(
                "INSERT INTO bindings (surface, surface_key, session_id) VALUES (?, ?, ?) "
                "ON CONFLICT(surface, surface_key) DO UPDATE SET session_id = excluded.session_id",
                (surface, surface_key, session_id),
            )
            if old_session_id and old_session_id != session_id:
                self._stop_if_orphaned(old_session_id)
            self._conn.commit()

    def _stop_if_orphaned(self, session_id: str) -> None:
        remaining = self._conn.execute(
            "SELECT COUNT(*) AS n FROM bindings WHERE session_id = ?", (session_id,)
        ).fetchone()["n"]
        if remaining == 0:
            self._conn.execute(
                "UPDATE sessions SET status = 'stopped', updated_at = ? "
                "WHERE id = ? AND status = 'active'",
                (_now(), session_id),
            )

    def resolve(self, surface: Surface, surface_key: str) -> str | None:
        """Вернуть session_id окна — ТОЛЬКО если сессия active.

        Фильтр по статусу закрывает «resume закрытой/остановленной сессии»: окно, чья сессия
        closed/stopped, не резюмится молча (ревью корректности + adversarial).
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT b.session_id FROM bindings b "
                "JOIN sessions s ON s.id = b.session_id "
                "WHERE b.surface = ? AND b.surface_key = ? AND s.status = 'active'",
                (surface, surface_key),
            ).fetchone()
        return row["session_id"] if row else None

    def get(self, surface: Surface, surface_key: str) -> Binding | None:
        sid = self.resolve(surface, surface_key)
        return Binding(surface=surface, surface_key=surface_key, session_id=sid) if sid else None


class UsageStore(_SubStore):
    def add(
        self,
        *,
        session_id: str,
        model: str | None,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO usage (session_id, ts, model, input_tokens, output_tokens, cost_usd) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, _now(), model, input_tokens, output_tokens, cost_usd),
            )
            self._conn.commit()

    def session_cost(self, session_id: str) -> float:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM usage WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return float(row["total"])


def _project(row: sqlite3.Row) -> Project:
    return Project(
        slug=row["slug"],
        name=row["name"],
        brain_path=row["brain_path"],
        default_model=row["default_model"],
        status=row["status"],
    )


def _session(row: sqlite3.Row) -> Session:
    return Session(
        id=row["id"],
        owner_user_id=row["owner_user_id"],
        project_slug=row["project_slug"],
        claude_session_id=row["claude_session_id"],
        status=row["status"],
        model=row["model"],
        verbose=row["verbose"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
