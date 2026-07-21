"""TDD для engine/seed.py — идемпотентный сид дефолтного проекта→мозг.

deploy.sh зовёт `python -m engine.seed` перед стартом бота: без строки в projects
роутер падает NoProjectError на первом сообщении (см. store.SessionStore.create).
Сид обязан быть идемпотентным — повторный деплой не должен дублировать/ронять.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys

from engine.core.store import Store
from engine.seed import ensure_project


def test_ensure_project_creates_when_absent(tmp_path):
    db = str(tmp_path / "state.db")
    store = Store(db)
    result = ensure_project(
        store, slug="test-sdk", name="Тест", brain_path="/home/canary/brains/test-sdk"
    )
    assert result == "created"
    proj = store.projects.get("test-sdk")
    assert proj is not None
    assert proj.brain_path == "/home/canary/brains/test-sdk"
    assert proj.status == "active"
    store.close()


def test_ensure_project_idempotent(tmp_path):
    db = str(tmp_path / "state.db")
    store = Store(db)
    ensure_project(store, slug="test-sdk", name="Тест", brain_path="/b")
    # второй прогон — не дублирует, не падает, сообщает "exists"
    result = ensure_project(store, slug="test-sdk", name="Тест", brain_path="/b")
    assert result == "exists"
    # ровно одна строка проекта
    n = store.projects.list_active()
    assert len([p for p in n if p.slug == "test-sdk"]) == 1
    store.close()


def test_ensure_project_rejects_bad_slug(tmp_path):
    db = str(tmp_path / "state.db")
    store = Store(db)
    try:
        ensure_project(store, slug="Bad Slug!", name="x", brain_path="/b")
        raised = False
    except ValueError:
        raised = True
    assert raised, "плохой slug должен отвергаться (store._SLUG_RE)"
    store.close()


def test_cli_main_creates_and_is_idempotent(tmp_path):
    db = str(tmp_path / "state.db")
    cmd = [
        sys.executable,
        "-m",
        "engine.seed",
        "--db",
        db,
        "--slug",
        "test-sdk",
        "--name",
        "Тест",
        "--brain",
        "/home/canary/brains/test-sdk",
    ]
    r1 = subprocess.run(cmd, capture_output=True, text=True)
    assert r1.returncode == 0, r1.stderr
    assert "created" in r1.stdout
    # проект реально в БД
    c = sqlite3.connect(db)
    assert c.execute("SELECT count(*) FROM projects WHERE slug='test-sdk'").fetchone()[0] == 1
    c.close()
    # повторный запуск — exit 0, exists, без дубля
    r2 = subprocess.run(cmd, capture_output=True, text=True)
    assert r2.returncode == 0, r2.stderr
    assert "exists" in r2.stdout
    c = sqlite3.connect(db)
    assert c.execute("SELECT count(*) FROM projects WHERE slug='test-sdk'").fetchone()[0] == 1
    c.close()


def test_ensure_project_divergent_brain(tmp_path):
    # передеплой того же slug с ДРУГИМ brain_path → 'exists-divergent' (не молчаливое игнорирование)
    db = str(tmp_path / "state.db")
    store = Store(db)
    ensure_project(store, slug="test-sdk", name="Тест", brain_path="/home/canary/brains/a")
    result = ensure_project(store, slug="test-sdk", name="Тест", brain_path="/home/canary/brains/b")
    assert result == "exists-divergent"
    # brain_path НЕ сдвинут (store INSERT-only, cwd резюма не двигаем молча)
    assert store.projects.get("test-sdk").brain_path == "/home/canary/brains/a"
    store.close()
