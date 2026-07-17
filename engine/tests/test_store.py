"""Слой данных §5.2 — projects / sessions / bindings / usage.

Модель из трёх сущностей (не «топик = сессия = проект»):
  • project = папка-мозг;
  • session = одна непрерывная история диалога (id — UUIDv4, хранит claude_session_id);
  • binding = окно поверхности (топик ТГ / чат веб) в сессию — разные окна в одну сессию.

Заменяет транзитный chat_id→session_id из пилота. Дисциплина: WAL, busy_timeout,
единая writer-точка (все записи через один Store).
"""

import re
import sqlite3

import pytest

from engine.core.models import Project, Session
from engine.core.store import Store

UUID4_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "state.db"))
    yield s
    s.close()


# ── дисциплина SQLite ────────────────────────────────────────────────────────


def test_wal_enabled(store):
    mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_foreign_keys_on(store):
    assert store._conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_busy_timeout_set(store):
    assert store._conn.execute("PRAGMA busy_timeout").fetchone()[0] > 0


def test_schema_version_set(store):
    # якорь миграций (DM-1): update.sh определяет версию БД по нему
    assert store._conn.execute("PRAGMA user_version").fetchone()[0] == 1


# ── projects ─────────────────────────────────────────────────────────────────


def test_create_and_get_project(store):
    store.projects.create(slug="office", name="Офис", brain_path="/brains/office")
    p = store.projects.get("office")
    assert isinstance(p, Project)
    assert p.slug == "office"
    assert p.brain_path == "/brains/office"
    assert p.status == "active"


def test_get_missing_project_returns_none(store):
    assert store.projects.get("nope") is None


def test_project_slug_validation(store):
    for bad in ("Office", "of fice", "of/ice", "офис", ""):
        with pytest.raises(ValueError):
            store.projects.create(slug=bad, name="x", brain_path="/b")


def test_project_slug_unique(store):
    store.projects.create(slug="office", name="Офис", brain_path="/b1")
    with pytest.raises(sqlite3.IntegrityError):
        store.projects.create(slug="office", name="Другой", brain_path="/b2")


def test_list_and_disable_projects(store):
    store.projects.create(slug="a", name="A", brain_path="/a")
    store.projects.create(slug="b", name="B", brain_path="/b")
    assert {p.slug for p in store.projects.list_active()} == {"a", "b"}
    store.projects.disable("a")
    assert {p.slug for p in store.projects.list_active()} == {"b"}


# ── sessions ─────────────────────────────────────────────────────────────────


@pytest.fixture
def project(store):
    store.projects.create(
        slug="office", name="Офис", brain_path="/brains/office", default_model="sonnet"
    )
    return "office"


def test_create_session_uuid4(store, project):
    s = store.sessions.create(owner_user_id=111, project_slug=project)
    assert isinstance(s, Session)
    assert UUID4_RE.match(s.id), f"session.id не UUIDv4: {s.id}"
    assert s.status == "active"
    assert s.claude_session_id is None
    assert s.model == "sonnet"  # унаследован из project.default_model


def test_session_requires_existing_project(store):
    with pytest.raises(sqlite3.IntegrityError):
        store.sessions.create(owner_user_id=111, project_slug="ghost")


def test_update_claude_session_id_rewrites(store, project):
    """Урок канарейки C2a: claude_session_id ДОЛЖЕН переписываться из каждого ответа,
    иначе 2-й рестарт поднимает ветку истории по состоянию на 1-й рестарт."""
    s = store.sessions.create(owner_user_id=111, project_slug=project)
    store.sessions.set_claude_session_id(s.id, "claude-run-1")
    assert store.sessions.get(s.id).claude_session_id == "claude-run-1"
    store.sessions.set_claude_session_id(s.id, "claude-run-2")
    assert store.sessions.get(s.id).claude_session_id == "claude-run-2"


def test_session_status_transition_to_stopped(store, project):
    s = store.sessions.create(owner_user_id=111, project_slug=project)
    store.sessions.set_status(s.id, "stopped")
    assert store.sessions.get(s.id).status == "stopped"


def test_set_status_rejects_junk(store, project):
    s = store.sessions.create(owner_user_id=111, project_slug=project)
    with pytest.raises(ValueError):
        store.sessions.set_status(s.id, "banana")


def test_set_status_closed_only_via_close(store, project):
    """set_status('closed') запрещён — closed ставится только close() (он же отвязывает окна)."""
    s = store.sessions.create(owner_user_id=111, project_slug=project)
    with pytest.raises(ValueError):
        store.sessions.set_status(s.id, "closed")


def test_closed_session_not_resurrectable(store, project):
    s = store.sessions.create(owner_user_id=111, project_slug=project)
    store.sessions.close(s.id)
    with pytest.raises(ValueError):
        store.sessions.set_status(s.id, "active")


def test_session_verbose_default_zero(store, project):
    s = store.sessions.create(owner_user_id=111, project_slug=project)
    assert s.verbose == 0


def test_set_verbose_range(store, project):
    s = store.sessions.create(owner_user_id=111, project_slug=project)
    store.sessions.set_verbose(s.id, 3)
    assert store.sessions.get(s.id).verbose == 3
    for bad in (-1, 4, 99):
        with pytest.raises(ValueError):
            store.sessions.set_verbose(s.id, bad)


def test_create_session_on_disabled_project_rejected(store):
    store.projects.create(slug="office", name="Офис", brain_path="/b")
    store.projects.disable("office")
    with pytest.raises(sqlite3.IntegrityError):
        store.sessions.create(owner_user_id=111, project_slug="office")


# ── bindings ─────────────────────────────────────────────────────────────────


def test_bind_and_resolve(store, project):
    s = store.sessions.create(owner_user_id=111, project_slug=project)
    store.bindings.bind("tg", "chat42:thread7", s.id)
    assert store.bindings.resolve("tg", "chat42:thread7") == s.id


def test_resolve_missing_binding_returns_none(store):
    assert store.bindings.resolve("tg", "nope") is None


def test_two_windows_one_session(store, project):
    """Веб + ТГ в одну сессию — «начал в вебе, продолжил в ТГ» (§5.2)."""
    s = store.sessions.create(owner_user_id=111, project_slug=project)
    store.bindings.bind("tg", "chat42:thread7", s.id)
    store.bindings.bind("web", "conv-abc", s.id)
    assert store.bindings.resolve("tg", "chat42:thread7") == s.id
    assert store.bindings.resolve("web", "conv-abc") == s.id


def test_rebind_surface_key_to_new_session(store, project):
    s1 = store.sessions.create(owner_user_id=111, project_slug=project)
    s2 = store.sessions.create(owner_user_id=111, project_slug=project)
    store.bindings.bind("tg", "chat42:thread7", s1.id)
    store.bindings.bind("tg", "chat42:thread7", s2.id)  # то же окно → новая сессия
    assert store.bindings.resolve("tg", "chat42:thread7") == s2.id
    # ровно одна строка биндинга (ON CONFLICT перезаписал, не задублировал)
    n = store._conn.execute(
        "SELECT COUNT(*) FROM bindings WHERE surface='tg' AND surface_key='chat42:thread7'"
    ).fetchone()[0]
    assert n == 1
    # осиротевшая s1 (без окон) закрыта в stopped — иначе пул держал бы её тёплой
    assert store.sessions.get(s1.id).status == "stopped"


def test_rebind_keeps_session_with_other_window(store, project):
    """Сессия с ДВУМЯ окнами: перевязка одного окна не осиротит её — второе ещё смотрит."""
    s1 = store.sessions.create(owner_user_id=111, project_slug=project)
    s2 = store.sessions.create(owner_user_id=111, project_slug=project)
    store.bindings.bind("tg", "chatX", s1.id)
    store.bindings.bind("web", "convY", s1.id)  # у s1 два окна
    store.bindings.bind("tg", "chatX", s2.id)  # перевязали только tg
    assert store.sessions.get(s1.id).status == "active"  # web-окно держит s1 живой


def test_resolve_ignores_non_active_session(store, project):
    """Окно закрытой/остановленной сессии не резолвится — не резюмим её молча."""
    s = store.sessions.create(owner_user_id=111, project_slug=project)
    store.bindings.bind("tg", "chatZ", s.id)
    store.sessions.set_status(s.id, "stopped")
    assert store.bindings.resolve("tg", "chatZ") is None


def test_bind_to_non_active_session_rejected(store, project):
    s = store.sessions.create(owner_user_id=111, project_slug=project)
    store.sessions.set_status(s.id, "stopped")
    with pytest.raises(ValueError):
        store.bindings.bind("tg", "chatW", s.id)


def test_bind_after_close_rejected(store, project):
    """in-flight хендлер не может переприкрепить окно к закрытой сессии."""
    s = store.sessions.create(owner_user_id=111, project_slug=project)
    store.sessions.close(s.id)
    with pytest.raises(ValueError):
        store.bindings.bind("web", "conv-x", s.id)


def test_bind_to_missing_session_rejected(store, project):
    with pytest.raises(sqlite3.IntegrityError):
        store.bindings.bind("tg", "chatQ", "no-such-session")


# ── usage ────────────────────────────────────────────────────────────────────


def test_usage_fk_enforced(store, project):
    """foreign_keys=ON реально форсится: usage на несуществующую сессию → IntegrityError."""
    with pytest.raises(sqlite3.IntegrityError):
        store.usage.add(
            session_id="ghost", model="x", input_tokens=1, output_tokens=1, cost_usd=0.0
        )


def test_usage_append_and_sum(store, project):
    s = store.sessions.create(owner_user_id=111, project_slug=project)
    store.usage.add(
        session_id=s.id, model="sonnet", input_tokens=100, output_tokens=20, cost_usd=0.01
    )
    store.usage.add(
        session_id=s.id, model="haiku", input_tokens=50, output_tokens=10, cost_usd=0.002
    )
    total = store.usage.session_cost(s.id)
    assert abs(total - 0.012) < 1e-9


# ── close + персистентность ───────────────────────────────────────────────────


def test_close_session_unbinds(store, project):
    s = store.sessions.create(owner_user_id=111, project_slug=project)
    store.bindings.bind("tg", "chat42:thread7", s.id)
    store.sessions.close(s.id)
    assert store.sessions.get(s.id).status == "closed"
    # биндинг отвязан — реанимация только новой сессией
    assert store.bindings.resolve("tg", "chat42:thread7") is None


def test_persists_across_instances(tmp_path):
    path = str(tmp_path / "state.db")
    s1 = Store(path)
    s1.projects.create(slug="office", name="Офис", brain_path="/b")
    sess = s1.sessions.create(owner_user_id=7, project_slug="office")
    s1.sessions.set_claude_session_id(sess.id, "claude-xyz")
    s1.close()

    s2 = Store(path)  # новый инстанс на том же файле = переживает рестарт
    assert s2.sessions.get(sess.id).claude_session_id == "claude-xyz"
    assert s2.projects.get("office").brain_path == "/b"
    s2.close()
