"""Health-маркер §5.4 — файл-сигнал состояния движка (паттерн agent_health боевого движка).

Пишется атомарно (temp + rename), чтобы читатель (agentctl doctor, алерт) не поймал
полузаписанный JSON. При auth-фейле движок ставит degraded — часть Фазы 1 (риск #13): тихо
умерший бот = фатальный первый опыт ученика, поэтому 401 виден снаружи, а не в логах.

Запись идемпотентна по смыслу: `ok()`/`degraded()` пишут файл ТОЛЬКО при смене статуса и
возвращают True на переходе. AgentCore алертит владельцу лишь на переходе ok→degraded —
иначе протухший токен слал бы сотни одинаковых алертов (по одному на сообщение).
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class HealthMarker:
    def __init__(self, path: str):
        self._path = Path(path)

    def ok(self) -> bool:
        """Пометить здоровым. True — если это переход (был не-ok)."""
        return self._write_if_changed("ok", None)

    def degraded(self, reason: str) -> bool:
        """Пометить деградировавшим. True — если это переход (был не тот degraded)."""
        return self._write_if_changed("degraded", reason)

    def read(self) -> dict[str, Any] | None:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def _write_if_changed(self, status: str, reason: str | None) -> bool:
        current = self.read()
        if current and current.get("status") == status and current.get("reason") == reason:
            return False  # статус не изменился — не пишем файл и не считаем переходом
        self._write(status, reason)
        return True

    def _write(self, status: str, reason: str | None) -> None:
        payload = {"status": status, "reason": reason, "ts": datetime.now(UTC).isoformat()}
        # tmp уникален по pid — два процесса движка не перезапишут чужой tmp и не словят
        # FileNotFoundError на replace (ревью: фиксированное имя гонится между процессами)
        tmp = self._path.with_suffix(f"{self._path.suffix}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self._path)  # атомарная замена — читатель не увидит полузапись
