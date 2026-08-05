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


# ── M-04: контракт [SEND_FILE:] доходит до модели ─────────────────────────────


def _append(opts) -> str:
    sp = opts.system_prompt
    return sp.get("append", "") if isinstance(sp, dict) else ""


def test_system_prompt_teaches_send_file_marker(tmp_path):
    """Движок вырезает маркер и отправляет файл — но агент про маркер узнать не мог:
    SYSTEM_PROMPT_APPEND пуст, в шаблонах мозга маркера нет, а примеры обещают
    «пришлю файлом». Значит описание маркера дописывает сам движок."""
    opts = _options_builder(_settings(tmp_path))(cwd="/b", resume=None, model=None)
    append = _append(opts)
    assert "[SEND_FILE:" in append
    assert "claude_code" == (opts.system_prompt or {}).get("preset")


def test_send_file_instructions_not_promised_when_feature_off(tmp_path):
    """SEND_FILE_ENABLED=false → маркер не описываем: обещать неработающее нельзя."""
    opts = _options_builder(_settings(tmp_path, send_file_enabled=False))(
        cwd="/b", resume=None, model=None
    )
    assert "[SEND_FILE:" not in _append(opts)


def test_owner_persona_append_survives_marker_docs(tmp_path):
    """SYSTEM_PROMPT_APPEND владельца не должен быть вытеснен описанием маркера."""
    opts = _options_builder(_settings(tmp_path, system_prompt_append="Ты Карусельщик."))(
        cwd="/b", resume=None, model=None
    )
    append = _append(opts)
    assert "Ты Карусельщик." in append and "[SEND_FILE:" in append


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


# ── A4: черновик настраивается из .env, а не числами в bot.py ────────────────


def test_streaming_is_wired_on_by_default(tmp_path):
    bot = build_bot(_settings(tmp_path))
    assert bot._draft is not None, "черновик «печатает…» включён по умолчанию"


def test_streaming_interval_comes_from_settings(tmp_path):
    """Ученик, у которого бот стоит в живой группе, обязан уметь притушить черновик."""
    bot = build_bot(_settings(tmp_path, stream_interval=4.0, stream_max_units=1000))
    assert bot._draft is not None
    assert bot._draft.interval == 4.0
    assert bot._draft.max_units == 1000


def test_streaming_can_be_switched_off_completely(tmp_path):
    """K10: выключенная косметика не существует как объект — черновика просто нет."""
    bot = build_bot(_settings(tmp_path, stream_enabled=False))
    assert bot._draft is None


def test_health_and_alert_reach_the_adapter(tmp_path):
    """A10: падение мимо ask обязано быть видно снаружи так же, как падение генерации."""
    bot = build_bot(_settings(tmp_path))
    assert bot._health is not None, "errors-handler без health-маркера ничего не пометит"
    assert bot._on_alert is not None, "владелец должен узнавать о сбоях адаптера"
