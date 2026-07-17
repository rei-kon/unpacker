"""Health-маркер §5.4 — файл-сигнал состояния движка (паттерн agent_health боевого движка).

При auth-фейле (протух OAuth — риск #13) движок пишет degraded в health.json; agentctl doctor
и алерт владельцу читают его. Тихо умерший бот — фатальный первый опыт ученика.
"""

import json

from engine.core.health import HealthMarker


def test_ok_writes_status(tmp_path):
    h = HealthMarker(str(tmp_path / "health.json"))
    h.ok()
    data = json.loads((tmp_path / "health.json").read_text())
    assert data["status"] == "ok"
    assert data["reason"] is None
    assert "ts" in data


def test_degraded_writes_reason(tmp_path):
    h = HealthMarker(str(tmp_path / "health.json"))
    h.degraded("auth: 401")
    data = json.loads((tmp_path / "health.json").read_text())
    assert data["status"] == "degraded"
    assert data["reason"] == "auth: 401"


def test_read_returns_none_when_absent(tmp_path):
    h = HealthMarker(str(tmp_path / "health.json"))
    assert h.read() is None


def test_read_roundtrip(tmp_path):
    h = HealthMarker(str(tmp_path / "health.json"))
    h.degraded("boom")
    assert h.read()["status"] == "degraded"
    h.ok()
    assert h.read()["status"] == "ok"


def test_read_corrupt_returns_none(tmp_path):
    p = tmp_path / "health.json"
    p.write_text("{ not json")
    h = HealthMarker(str(p))
    assert h.read() is None  # битый файл не роняет читателя


def test_change_detection_dedups(tmp_path):
    """Повторный тот же статус — не переход, иначе алерт флудил бы на каждом сообщении."""
    h = HealthMarker(str(tmp_path / "health.json"))
    assert h.degraded("401") is True  # первый переход в degraded
    assert h.degraded("401") is False  # тот же — не переход, файл не переписан
    assert h.ok() is True  # переход обратно
    assert h.ok() is False
