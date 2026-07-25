"""Сборка движка из Settings — собирается без падения (smoke), опции строятся верно."""

import pytest

from engine.core.config import Settings
from engine.core.sendfile import FileSandbox, SandboxError
from engine.runtime import _options_builder, build_bot


def _settings(tmp_path, **over):
    env = {
        "telegram_bot_token": "123:abc",
        "allowed_user_ids": [111, 222],
        "db_path": str(tmp_path / "state.db"),
        "health_path": str(tmp_path / "health.json"),
    }
    env.update(over)
    return Settings(_env_file=None, **env)  # type: ignore[arg-type]


def test_build_bot_assembles(tmp_path):
    settings = _settings(tmp_path)
    bot = build_bot(settings)
    assert bot is not None
    assert bot.bot is not None  # общий Bot для polling и алертов


def test_owner_is_first_allowed(tmp_path):
    settings = _settings(tmp_path)
    assert settings.owner_user_id == 111


def test_build_bot_rejects_empty_allowlist(tmp_path):
    """Пустой allow-list → громкий отказ на старте, не зомби-бот (находка ревью D)."""
    import pytest

    settings = _settings(tmp_path, allowed_user_ids=[])
    with pytest.raises(ValueError, match="ALLOWED_USER_IDS"):
        build_bot(settings)


def test_options_builder_internal_contour(tmp_path):
    """Внутренний контур: bypassPermissions + setting_sources (C1: скиллы мозга на Linux)."""
    settings = _settings(tmp_path)
    build = _options_builder(settings)
    opts = build(cwd="/brains/office", resume="claude-1", model="sonnet")
    assert opts.cwd == "/brains/office"
    assert opts.permission_mode == "bypassPermissions"
    assert opts.setting_sources == ["user", "project"]
    assert opts.resume == "claude-1"
    assert opts.model == "sonnet"


def test_options_builder_omits_empty(tmp_path):
    settings = _settings(tmp_path)
    build = _options_builder(settings)
    opts = build(cwd="/b", resume=None, model=None)
    assert opts.resume is None


# ── проводка косметики Фазы 1b (§6.1) ────────────────────────────────────────


def test_cosmetics_wired_on_by_default(tmp_path):
    bot = build_bot(_settings(tmp_path))
    assert bot._buttons is not None
    assert bot._intake is not None
    assert bot._send_file is not None, "без state/ проверка SEND_FILE не знает второй корень"


def test_flags_switch_cosmetics_off(tmp_path):
    """K10: одна конвенция — выключенная косметика не существует как объект.

    Три разных механизма (флаг внутри реестра / None / bool) были ровно тем швом, на
    котором жило «включено, но недонастроено».
    """
    bot = build_bot(
        _settings(tmp_path, buttons_enabled=False, uploads_enabled=False, send_file_enabled=False)
    )
    assert bot._buttons is None, "выключенные кнопки не должны существовать как объект"
    assert bot._intake is None, "выключенный приём файлов не должен существовать как объект"
    assert bot._send_file is None, "выключенная отдача файлов не должна существовать как объект"


def test_uploads_root_follows_config(tmp_path):
    bot = build_bot(_settings(tmp_path, uploads_dir=str(tmp_path / "custom" / "up")))
    assert bot._intake is not None
    assert bot._intake._uploads.root == tmp_path / "custom" / "up"


# ── K1/M-07/SEC-5/C17: корень песочницы не выводится из db_path ───────────────


def test_state_root_is_state_dir_not_instance_dir(tmp_path, monkeypatch):
    """ДЕФОЛТНЫЙ конфиг: `DB_PATH=state.db` лежит в каталоге инстанса, но корнем
    песочницы становится `state/`, а не сам каталог инстанса с `.env`."""
    monkeypatch.chdir(tmp_path)
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, telegram_bot_token="123:abc", allowed_user_ids=[111]
    )
    bot = build_bot(settings)
    assert bot._send_file is not None
    assert bot._send_file.state_root == (tmp_path / "state").resolve()


def test_default_config_does_not_expose_instance_env(tmp_path, monkeypatch):
    """Главный критерий K1: при дефолтном конфиге `.env` инстанса через песочницу не проходит."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=123:SECRET\n", encoding="utf-8")
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, telegram_bot_token="123:abc", allowed_user_ids=[111]
    )
    bot = build_bot(settings)
    assert bot._send_file is not None
    sandbox = FileSandbox([bot._send_file.state_root])
    with pytest.raises(SandboxError):
        sandbox.resolve(str(tmp_path / ".env"))


def test_state_root_follows_state_dir_setting(tmp_path):
    """M6: проверяем ЗНАЧЕНИЕ корня, а не «оно не None»."""
    bot = build_bot(_settings(tmp_path, state_dir=str(tmp_path / "явный" / "state")))
    assert bot._send_file is not None
    assert bot._send_file.state_root == (tmp_path / "явный" / "state").resolve()
