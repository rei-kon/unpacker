"""SQLite-хранилище session_id на chat_id — resume диалога после рестарта процесса.

resume в Agent SDK завязан на совпадение cwd: при несовпадении SDK молча создаёт новую
сессию (issue #555), поэтому cwd задаёт вызывающий и держит его неизменным.
Канарейка на это поведение — canaries/test_c2_resume_cwd.py.
"""

from __future__ import annotations

import sqlite3


class SessionStore:
    def __init__(self, path: str):
        self._path = path
        # check_same_thread=False — aiogram гоняет хендлеры в одном loop, но на разных задачах
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS sessions ("
            "  chat_id INTEGER PRIMARY KEY,"
            "  session_id TEXT NOT NULL"
            ")"
        )
        self._conn.commit()

    def get(self, chat_id: int) -> str | None:
        row = self._conn.execute(
            "SELECT session_id FROM sessions WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        return row[0] if row else None

    def set(self, chat_id: int, session_id: str) -> None:
        self._conn.execute(
            "INSERT INTO sessions (chat_id, session_id) VALUES (?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET session_id = excluded.session_id",
            (chat_id, session_id),
        )
        self._conn.commit()

    def clear(self, chat_id: int) -> None:
        self._conn.execute("DELETE FROM sessions WHERE chat_id = ?", (chat_id,))
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
