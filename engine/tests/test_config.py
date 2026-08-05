"""Конфиг из окружения (Pydantic). allowed_user_ids парсится из CSV.

_env_file=None во всех вызовах: тесты меряют ОКРУЖЕНИЕ, а не .env в текущем каталоге.
Иначе локальный .env разработчика (он в .gitignore) подмешивался бы и делал тест флаки —
зелёным в CI, красным локально.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from engine.core.config import Settings


def _base_env(**over):
    env = {
        "TELEGRAM_BOT_TOKEN": "123:abc",
        "ALLOWED_USER_IDS": "111,222",
    }
    env.update(over)
    return env


def _settings() -> Settings:
    # _env_file — рантайм-параметр BaseSettings.__init__ (pydantic-settings), mypy его не видит
    return Settings(_env_file=None)  # type: ignore[call-arg]


def test_loads_from_env(monkeypatch):
    for k, v in _base_env().items():
        monkeypatch.setenv(k, v)
    s = _settings()
    assert s.telegram_bot_token == "123:abc"
    assert s.allowed_user_ids == [111, 222]


def test_allowed_ids_single(monkeypatch):
    for k, v in _base_env(ALLOWED_USER_IDS="555").items():
        monkeypatch.setenv(k, v)
    s = _settings()
    assert s.allowed_user_ids == [555]


def test_allowed_ids_with_spaces(monkeypatch):
    for k, v in _base_env(ALLOWED_USER_IDS=" 111 , 222 ").items():
        monkeypatch.setenv(k, v)
    s = _settings()
    assert s.allowed_user_ids == [111, 222]


def test_missing_token_raises(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("ALLOWED_USER_IDS", "111")
    with pytest.raises(ValidationError):
        _settings()


def test_garbage_allowed_ids_raises(monkeypatch):
    # мусор в ALLOWED_USER_IDS = отказ старта с понятной ошибкой, а не молча
    for k, v in _base_env(ALLOWED_USER_IDS="111,foo").items():
        monkeypatch.setenv(k, v)
    with pytest.raises(ValidationError):
        _settings()


def test_default_response_timeout(monkeypatch):
    for k, v in _base_env().items():
        monkeypatch.setenv(k, v)
    s = _settings()
    assert s.response_timeout == 180.0


def test_system_prompt_append_default_none(monkeypatch):
    for k, v in _base_env().items():
        monkeypatch.setenv(k, v)
    s = _settings()
    assert s.system_prompt_append is None


def test_system_prompt_append_from_env(monkeypatch):
    for k, v in _base_env(SYSTEM_PROMPT_APPEND="Действуй как Карусельщик.").items():
        monkeypatch.setenv(k, v)
    s = _settings()
    assert s.system_prompt_append == "Действуй как Карусельщик."


def test_response_timeout_override(monkeypatch):
    for k, v in _base_env(RESPONSE_TIMEOUT="900").items():
        monkeypatch.setenv(k, v)
    s = _settings()
    assert s.response_timeout == 900.0


# ── флаги косметики Фазы 1b (§6.1: включено по умолчанию, выключается флагом) ──


def test_cosmetics_enabled_by_default(monkeypatch):
    for k, v in _base_env().items():
        monkeypatch.setenv(k, v)
    s = _settings()
    assert s.buttons_enabled is True
    assert s.uploads_enabled is True
    assert s.send_file_enabled is True


def test_cosmetics_paths_match_deploy_layout(monkeypatch):
    # deploy.sh кладёт buttons.yaml в корень инстанса и создаёт state/uploads
    for k, v in _base_env().items():
        monkeypatch.setenv(k, v)
    s = _settings()
    assert s.buttons_path == "buttons.yaml"
    assert s.uploads_path == Path("state/uploads")


# ── STATE_DIR: явный корень состояния (K1/M-07/SEC-5/C17) ────────────────────


def test_state_dir_defaults_to_state_subdir(monkeypatch):
    """Корень состояния объявлен ЯВНО, а не выводится из db_path.

    Раньше корень песочницы считался как parent(db_path); при дефолтном
    `DB_PATH=state.db` им становился каталог инстанса — вместе с `.env`, в котором
    лежат токен бота и токен подписки.
    """
    for k, v in _base_env().items():
        monkeypatch.setenv(k, v)
    assert _settings().state_dir == "state"


def test_uploads_path_derives_from_state_dir(monkeypatch):
    """UPLOADS_DIR не задан → uploads считаются от STATE_DIR (одна вселенная путей)."""
    for k, v in _base_env(STATE_DIR="/srv/inst/state").items():
        monkeypatch.setenv(k, v)
    assert _settings().uploads_path == Path("/srv/inst/state/uploads")


def test_uploads_dir_still_overrides_state_dir(monkeypatch):
    """UPLOADS_DIR остаётся рабочей ручкой (контракт .env не ломаем)."""
    for k, v in _base_env(STATE_DIR="/srv/inst/state", UPLOADS_DIR="/mnt/big/up").items():
        monkeypatch.setenv(k, v)
    assert _settings().uploads_path == Path("/mnt/big/up")


def test_state_dir_does_not_follow_db_path(monkeypatch):
    """Ключевая развязка K1: сдвинули БД в каталог инстанса — корень состояния не поехал."""
    for k, v in _base_env(DB_PATH="state.db").items():
        monkeypatch.setenv(k, v)
    assert _settings().state_dir == "state"


def test_buttons_can_be_switched_off(monkeypatch):
    for k, v in _base_env(BUTTONS_ENABLED="false").items():
        monkeypatch.setenv(k, v)
    assert _settings().buttons_enabled is False


def test_uploads_can_be_switched_off(monkeypatch):
    for k, v in _base_env(UPLOADS_ENABLED="0").items():
        monkeypatch.setenv(k, v)
    assert _settings().uploads_enabled is False


def test_send_file_can_be_switched_off(monkeypatch):
    for k, v in _base_env(SEND_FILE_ENABLED="no").items():
        monkeypatch.setenv(k, v)
    assert _settings().send_file_enabled is False


def test_upload_limit_defaults_to_bot_api_cap(monkeypatch):
    from engine.core.uploads import MAX_UPLOAD_BYTES

    for k, v in _base_env().items():
        monkeypatch.setenv(k, v)
    assert _settings().max_upload_bytes == MAX_UPLOAD_BYTES


def test_upload_limit_cannot_exceed_bot_api_cap(monkeypatch):
    """Поднять лимит выше предела Bot API нельзя — это была бы ложь пользователю:
    движок обещает принять 100 МБ, а Telegram всё равно не отдаст файл."""
    for k, v in _base_env(MAX_UPLOAD_BYTES=str(200 * 1024 * 1024)).items():
        monkeypatch.setenv(k, v)
    with pytest.raises(ValidationError):
        _settings()


# ── A4: ручки черновика «печатает…» ──────────────────────────────────────────


def test_streaming_defaults_are_sane(monkeypatch):
    for k, v in _base_env().items():
        monkeypatch.setenv(k, v)
    s = _settings()
    assert s.stream_enabled is True
    assert 0 < s.stream_interval <= 3.0, "интервал впритык к потолку Telegram — не дефолт"
    assert s.stream_max_units < 4096, "окно черновика обязано быть ниже лимита сообщения"


def test_streaming_knobs_read_from_env(monkeypatch):
    env = _base_env(STREAM_ENABLED="false", STREAM_INTERVAL="5", STREAM_MAX_UNITS="1200")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    s = _settings()
    assert s.stream_enabled is False
    assert s.stream_interval == 5.0
    assert s.stream_max_units == 1200


# ── FA7: ручки черновика с границами (ученик правит .env руками) ─────────────


def test_streaming_defaults_match_the_engine_defaults(monkeypatch):
    """Дефолт .env и дефолт DraftTuning — одно число, а не два похожих.

    Разъедутся — движок начнёт вести себя по-разному в зависимости от того, задал ли
    ученик ручку вообще, и объяснить это ему будет нечем.
    """
    from engine.adapters.telegram.draft import DraftTuning

    for k, v in _base_env().items():
        monkeypatch.setenv(k, v)
    s = _settings()
    tuning = DraftTuning()
    assert s.stream_interval == tuning.interval
    assert s.stream_max_units == tuning.max_units


def test_zero_interval_is_refused(monkeypatch):
    """0 — не «быстро», а горячий цикл без сна: 100% CPU на инстансе ученика."""
    for k, v in _base_env(STREAM_INTERVAL="0").items():
        monkeypatch.setenv(k, v)
    with pytest.raises(ValidationError):
        _settings()


def test_negative_interval_is_refused(monkeypatch):
    for k, v in _base_env(STREAM_INTERVAL="-1").items():
        monkeypatch.setenv(k, v)
    with pytest.raises(ValidationError):
        _settings()


def test_window_wider_than_a_message_is_refused(monkeypatch):
    """Окно шире лимита Telegram = 400 MESSAGE_TOO_LONG на КАЖДОМ тике черновика."""
    for k, v in _base_env(STREAM_MAX_UNITS="4200").items():
        monkeypatch.setenv(k, v)
    with pytest.raises(ValidationError):
        _settings()


def test_window_narrower_than_the_counter_is_refused(monkeypatch):
    """Окно меньше служебной строки-счётчика — человек видит пустоту вместо текста."""
    for k, v in _base_env(STREAM_MAX_UNITS="50").items():
        monkeypatch.setenv(k, v)
    with pytest.raises(ValidationError):
        _settings()
