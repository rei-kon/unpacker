"""SQLite session store — resume диалога Claude после рестарта процесса."""
import pytest
from engine.core.sessions import SessionStore


@pytest.fixture
def store(tmp_path):
    return SessionStore(str(tmp_path / "sessions.db"))


def test_get_missing_returns_none(store):
    assert store.get(12345) is None


def test_set_then_get(store):
    store.set(12345, "sess-abc")
    assert store.get(12345) == "sess-abc"


def test_set_overwrites(store):
    store.set(12345, "sess-old")
    store.set(12345, "sess-new")
    assert store.get(12345) == "sess-new"


def test_separate_chats_isolated(store):
    store.set(1, "sess-1")
    store.set(2, "sess-2")
    assert store.get(1) == "sess-1"
    assert store.get(2) == "sess-2"


def test_clear(store):
    store.set(1, "sess-1")
    store.clear(1)
    assert store.get(1) is None


def test_persists_across_instances(tmp_path):
    path = str(tmp_path / "sessions.db")
    s1 = SessionStore(path)
    s1.set(7, "sess-7")
    # новый инстанс на том же файле = данные на месте (переживает рестарт)
    s2 = SessionStore(path)
    assert s2.get(7) == "sess-7"
