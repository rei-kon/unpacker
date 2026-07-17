"""Конфиг из окружения (Pydantic). allowed_user_ids парсится из CSV."""
import pytest
from engine.core.config import Settings


def _base_env(**over):
    env = {
        "TELEGRAM_BOT_TOKEN": "123:abc",
        "ALLOWED_USER_IDS": "111,222",
    }
    env.update(over)
    return env


def test_loads_from_env(monkeypatch):
    for k, v in _base_env().items():
        monkeypatch.setenv(k, v)
    s = Settings()
    assert s.telegram_bot_token == "123:abc"
    assert s.allowed_user_ids == [111, 222]


def test_default_office_cwd(monkeypatch):
    for k, v in _base_env().items():
        monkeypatch.setenv(k, v)
    s = Settings()
    assert s.office_cwd == "/root/ai-office-v2"


def test_allowed_ids_single(monkeypatch):
    for k, v in _base_env(ALLOWED_USER_IDS="555").items():
        monkeypatch.setenv(k, v)
    s = Settings()
    assert s.allowed_user_ids == [555]


def test_allowed_ids_with_spaces(monkeypatch):
    for k, v in _base_env(ALLOWED_USER_IDS=" 111 , 222 ").items():
        monkeypatch.setenv(k, v)
    s = Settings()
    assert s.allowed_user_ids == [111, 222]


def test_missing_token_raises(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("ALLOWED_USER_IDS", "111")
    with pytest.raises(Exception):
        Settings()


def test_garbage_allowed_ids_raises(monkeypatch):
    # мусор в ALLOWED_USER_IDS = отказ старта с понятной ошибкой, а не молча
    for k, v in _base_env(ALLOWED_USER_IDS="111,foo").items():
        monkeypatch.setenv(k, v)
    with pytest.raises(Exception):
        Settings()


def test_default_response_timeout(monkeypatch):
    for k, v in _base_env().items():
        monkeypatch.setenv(k, v)
    s = Settings()
    assert s.response_timeout == 180.0


def test_system_prompt_append_default_none(monkeypatch):
    for k, v in _base_env().items():
        monkeypatch.setenv(k, v)
    s = Settings()
    assert s.system_prompt_append is None


def test_system_prompt_append_from_env(monkeypatch):
    for k, v in _base_env(SYSTEM_PROMPT_APPEND="Действуй как Карусельщик.").items():
        monkeypatch.setenv(k, v)
    s = Settings()
    assert s.system_prompt_append == "Действуй как Карусельщик."


def test_response_timeout_override(monkeypatch):
    for k, v in _base_env(RESPONSE_TIMEOUT="900").items():
        monkeypatch.setenv(k, v)
    s = Settings()
    assert s.response_timeout == 900.0
