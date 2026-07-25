"""Заглушка Bot API для тестов гейтов deploy.sh (§7.3).

Гейт «токен валиден» обязан ходить в СЕТЬ (getMe) — иначе он не гейт, а декорация.
Чтобы тест при этом не зависел от интернета и живого токена, поднимаем локальный
HTTP-сервер и подсовываем его через `TG_API_BASE`. Проверяется настоящий curl-путь
внутри скрипта, включая парс ответа и редакцию токена в выводе.

Правило заглушки: токен «плохой», если в нём есть подстрока BAD (или это не getMe);
всё остальное — валидный бот. Так существующим тестам не надо менять свои токены.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Фикстура `api_base` живёт в engine/tests/conftest.py — здесь только сам сервер-дублёр
# (M9/S1: одна фикстура в одном месте, а не импорт-цепочка между тест-модулями).

# Токен, который заглушка ВСЕГДА отвергает (401) — для негативных тестов гейта.
BAD_TOKEN = "999999:BADtokenBAD"
# Порт, на котором заведомо никто не слушает — имитация «сеть недоступна».
DEAD_BASE = "http://127.0.0.1:9"


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 — имя задано BaseHTTPRequestHandler
        ok = self.path.endswith("/getMe") and "BAD" not in self.path
        if ok:
            payload: dict[str, object] = {
                "ok": True,
                "result": {"id": 123456, "is_bot": True, "username": "stub_bot"},
            }
            code = 200
        else:
            payload = {"ok": False, "error_code": 401, "description": "Unauthorized"}
            code = 401
        raw = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass  # тихий сервер: лог запросов мусорил бы в вывод pytest


@contextmanager
def tg_api_stub() -> Iterator[str]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()
