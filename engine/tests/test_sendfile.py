"""Отдача файлов маркером `[SEND_FILE:путь]` — §9 + песочница путей §8.2.

Механизм отдачи один (§9, §14): маркер в тексте ответа, никаких MCP-тулов. Отсюда и риск:
путь диктует МОДЕЛЬ, а модель читает недоверенные файлы и чужие мозги. Значит путь —
недоверенный ввод, и разрешены ровно два корня: папка-мозг проекта и `state/` инстанса.

Каждый «злой» кейс здесь — не гипотеза, а то, что реально уедет в чат, если песочницы нет:
`/etc/passwd`, `~/.claude/.credentials.json` (токен подписки!), `../../.env` инстанса
(токен бота), симлинк из мозга наружу.
"""

from __future__ import annotations

import pytest

from engine.core.sendfile import (
    MAX_SEND_BYTES,
    FileSandbox,
    SandboxError,
    blocked_message,
    extract_send_files,
)


@pytest.fixture
def world(tmp_path):
    """Мозг + state инстанса + «секреты» снаружи песочницы."""
    brain = tmp_path / "brains" / "office"
    (brain / "docs").mkdir(parents=True)
    (brain / "CLAUDE.md").write_text("# мозг\n", encoding="utf-8")
    (brain / "docs" / "kp.pdf").write_text("КП", encoding="utf-8")

    state = tmp_path / "agents" / "office" / "state"
    (state / "uploads" / "sid1").mkdir(parents=True)
    (state / "uploads" / "sid1" / "in.pdf").write_text("входящий", encoding="utf-8")

    secrets = tmp_path / "agents" / "office"
    (secrets / ".env").write_text("TELEGRAM_BOT_TOKEN=123:SECRET\n", encoding="utf-8")
    creds = tmp_path / "home" / ".claude"
    creds.mkdir(parents=True)
    (creds / ".credentials.json").write_text("{oauth}", encoding="utf-8")

    return {
        "brain": brain,
        "state": state,
        # base = папка-мозг: cwd агента там (§5.2), от него считаются относительные пути.
        # Явный аргумент вместо «первого корня в списке» — K5.
        "sandbox": FileSandbox([brain, state], base=brain),
        "env": secrets / ".env",
        "creds": creds / ".credentials.json",
    }


# ── извлечение маркеров из текста ────────────────────────────────────────────


def test_extracts_marker_and_strips_it():
    text, paths = extract_send_files("Готово, держи отчёт [SEND_FILE:docs/kp.pdf] — глянь.")
    assert paths == ["docs/kp.pdf"]
    assert "SEND_FILE" not in text
    assert "Готово, держи отчёт" in text and "— глянь." in text


def test_extracts_several_markers_in_order():
    text, paths = extract_send_files("[SEND_FILE:a.pdf] и ещё [SEND_FILE:/b/c.png]")
    assert paths == ["a.pdf", "/b/c.png"]
    assert "SEND_FILE" not in text


def test_text_without_markers_unchanged():
    src = "Обычный ответ без вложений."
    text, paths = extract_send_files(src)
    assert (text, paths) == (src, [])


def test_marker_with_spaces_is_trimmed():
    _, paths = extract_send_files("[SEND_FILE:  docs/kp.pdf  ]")
    assert paths == ["docs/kp.pdf"]


def test_empty_marker_is_ignored():
    text, paths = extract_send_files("пусто [SEND_FILE:] и [SEND_FILE:   ]")
    assert paths == []
    assert "SEND_FILE" not in text  # мусорный маркер всё равно не показываем человеку


def test_marker_cannot_span_lines():
    # перевод строки внутри маркера — это не маркер, а текст (иначе жадный матч съест абзац)
    text, paths = extract_send_files("[SEND_FILE:a\nb.pdf]")
    assert paths == []
    assert "SEND_FILE" in text


def test_text_of_only_marker_becomes_empty():
    text, paths = extract_send_files("[SEND_FILE:docs/kp.pdf]")
    assert paths == ["docs/kp.pdf"]
    assert text.strip() == ""


# ── песочница: разрешённое ───────────────────────────────────────────────────


def test_absolute_path_in_brain_allowed(world):
    got = world["sandbox"].resolve(str(world["brain"] / "docs" / "kp.pdf"))
    assert got.name == "kp.pdf"


def test_relative_path_resolved_against_brain(world):
    # cwd агента = папка-мозг (§5.2), поэтому «docs/kp.pdf» — это внутри мозга
    assert world["sandbox"].resolve("docs/kp.pdf").name == "kp.pdf"


def test_dot_slash_relative_allowed(world):
    assert world["sandbox"].resolve("./docs/kp.pdf").name == "kp.pdf"


def test_file_in_instance_state_allowed(world):
    # туда-обратно: принятый файл из uploads агент вправе отдать назад
    got = world["sandbox"].resolve(str(world["state"] / "uploads" / "sid1" / "in.pdf"))
    assert got.name == "in.pdf"


# ── песочница: злые кейсы (каждый — отдельная атака) ─────────────────────────


def test_blocks_absolute_outside(world):
    with pytest.raises(SandboxError) as exc:
        world["sandbox"].resolve("/etc/passwd")
    assert exc.value.reason == "outside"


def test_blocks_dotdot_escape_to_instance_env(world):
    """`../.env` из мозга — попытка выдать токен бота в чат."""
    with pytest.raises(SandboxError) as exc:
        world["sandbox"].resolve("../../agents/office/.env")
    assert exc.value.reason == "outside"


def test_blocks_absolute_env_of_instance(world):
    with pytest.raises(SandboxError) as exc:
        world["sandbox"].resolve(str(world["env"]))
    assert exc.value.reason == "outside"


def test_blocks_claude_credentials(world):
    """~/.claude/.credentials.json — токен подписки; и через ~, и абсолютным путём."""
    with pytest.raises(SandboxError):
        world["sandbox"].resolve("~/.claude/.credentials.json")
    with pytest.raises(SandboxError) as exc:
        world["sandbox"].resolve(str(world["creds"]))
    assert exc.value.reason == "outside"


def test_blocks_tilde_paths_explicitly(world):
    with pytest.raises(SandboxError) as exc:
        world["sandbox"].resolve("~/anything.txt")
    assert exc.value.reason == "home"


def test_blocks_symlink_pointing_outside(world):
    """Симлинк ВНУТРИ мозга на файл снаружи: имя выглядит легально, цель — нет."""
    link = world["brain"] / "docs" / "innocent.pdf"
    link.symlink_to(world["env"])
    with pytest.raises(SandboxError) as exc:
        world["sandbox"].resolve(str(link))
    assert exc.value.reason == "outside"


def test_blocks_symlinked_directory_escape(world):
    """Симлинк-каталог: `brain/out/` → наружу, дальше обычный относительный путь."""
    (world["brain"] / "out").symlink_to(world["env"].parent)
    with pytest.raises(SandboxError):
        world["sandbox"].resolve("out/.env")


def test_blocks_directory_itself(world):
    with pytest.raises(SandboxError) as exc:
        world["sandbox"].resolve(str(world["brain"] / "docs"))
    assert exc.value.reason == "not_file"


def test_blocks_missing_file(world):
    with pytest.raises(SandboxError) as exc:
        world["sandbox"].resolve("docs/нет-такого.pdf")
    assert exc.value.reason == "missing"


def test_blocks_nul_byte(world):
    with pytest.raises(SandboxError):
        world["sandbox"].resolve("docs/kp.pdf\x00.png")


def test_blocks_empty_path(world):
    with pytest.raises(SandboxError) as exc:
        world["sandbox"].resolve("   ")
    assert exc.value.reason == "empty"


def test_blocks_too_big_file(world):
    big = world["brain"] / "huge.bin"
    big.write_bytes(b"0")
    # не пишем 50 МБ на диск — подменяем лимит, поведение то же
    tight = FileSandbox([world["brain"]], max_bytes=0)
    with pytest.raises(SandboxError) as exc:
        tight.resolve(str(big))
    assert exc.value.reason == "too_big"


def test_default_limit_is_telegram_document_cap():
    assert MAX_SEND_BYTES == 50 * 1024 * 1024


def test_symlinked_root_still_works(tmp_path):
    """Корень сам может быть симлинком (на macOS /tmp → /private/tmp) — это не «наружу»."""
    real = tmp_path / "real"
    real.mkdir()
    (real / "f.txt").write_text("x", encoding="utf-8")
    link_root = tmp_path / "link"
    link_root.symlink_to(real)
    assert FileSandbox([link_root]).resolve(str(link_root / "f.txt")).name == "f.txt"


def test_sandbox_without_roots_blocks_everything(world):
    # выключенная фича / нет проекта → ничего не отдаём (fail-closed)
    with pytest.raises(SandboxError):
        FileSandbox([]).resolve(str(world["brain"] / "docs" / "kp.pdf"))


# ── deny-list по ИМЕНИ: работает независимо от корней (K1/SEC-5) ─────────────
#
# Корень песочницы задаётся конфигом, а конфиг ученик правит руками. Поэтому одной
# проверки «внутри корня» мало: секретные имена запрещены всегда, даже если файл лежит
# внутри разрешённого каталога (`DB_PATH=/opt/x/state.db` → корнем станет /opt/x).


@pytest.mark.parametrize(
    "name",
    [
        ".env",
        ".env.local",
        ".env.production",
        "google.credentials.json",
        "id_rsa",
        "id_ed25519.pub",
        "server.pem",
    ],
)
def test_denylisted_name_blocked_even_inside_root(world, name):
    victim = world["brain"] / name
    victim.write_text("SECRET", encoding="utf-8")
    with pytest.raises(SandboxError) as exc:
        world["sandbox"].resolve(str(victim))
    assert exc.value.reason == "denied"


def test_denylist_blocks_git_config(world):
    """`.git/config` несёт токен приватного репо в remote URL."""
    (world["brain"] / ".git").mkdir()
    (world["brain"] / ".git" / "config").write_text("[remote]\n", encoding="utf-8")
    with pytest.raises(SandboxError) as exc:
        world["sandbox"].resolve(".git/config")
    assert exc.value.reason == "denied"


def test_denylist_survives_root_equal_to_instance_dir(world):
    """Худший случай K1: корнем стал каталог инстанса. `.env` всё равно не уходит."""
    instance_dir = world["env"].parent
    wide = FileSandbox([instance_dir], base=instance_dir)
    with pytest.raises(SandboxError) as exc:
        wide.resolve(".env")
    assert exc.value.reason == "denied"


def test_denylist_catches_symlink_to_secret_name(world):
    """Симлинк с невинным именем на `.env` — deny-list смотрит на РАЗРЕШЁННОЕ имя."""
    (world["brain"] / "readme.txt").symlink_to(world["env"])
    wide = FileSandbox([world["brain"], world["env"].parent], base=world["brain"])
    with pytest.raises(SandboxError) as exc:
        wide.resolve("readme.txt")
    assert exc.value.reason == "denied"


def test_denylist_does_not_block_normal_names(world):
    """Ремень не должен мешать работе: обычный файл проходит."""
    assert world["sandbox"].resolve("docs/kp.pdf").name == "kp.pdf"


def test_denylist_message_is_human_readable(world):
    victim = world["brain"] / ".env"
    victim.write_text("x", encoding="utf-8")
    try:
        world["sandbox"].resolve(".env")
    except SandboxError as exc:
        msg = blocked_message(".env", exc)
    assert ".env" in msg and "Traceback" not in msg


# ── явный base для относительных путей (K5) ──────────────────────────────────


def test_relative_path_uses_explicit_base(world):
    """Относительный путь считается от ЯВНОГО base, а не от «первого корня»."""
    sandbox = FileSandbox([world["state"], world["brain"]], base=world["brain"])
    assert sandbox.resolve("docs/kp.pdf").name == "kp.pdf"


def test_without_base_relative_path_is_rejected(world):
    """Нет base → относительный путь некуда считать: честный отказ вместо угадывания."""
    sandbox = FileSandbox([world["brain"]], base=None)
    with pytest.raises(SandboxError) as exc:
        sandbox.resolve("docs/kp.pdf")
    assert exc.value.reason == "bad_path"


def test_unresolvable_root_is_logged(tmp_path, caplog):
    """K13: корень, который не резолвится, не должен пропадать молча."""
    import logging

    with caplog.at_level(logging.WARNING, logger="unpacker.engine"):
        FileSandbox([tmp_path / "нет-такого\x00"])
    assert caplog.records, "выпавший корень песочницы обязан оставить след в логе"


# ── hardlink и TOCTOU (пробел покрытия S3) ───────────────────────────────────


def test_hardlink_inside_root_is_allowed_and_named_honestly(world):
    """Hardlink на секрет НЕ ловится resolve() — ловится deny-list по имени цели.

    Честная фиксация границы: hardlink с невинным именем внутри корня песочницу пройдёт
    (у жёсткой ссылки нет «цели», resolve её не раскроет). Именно поэтому §8.2 называет
    это guardrail'ом против ошибки модели, а не sandbox'ом против агента с Bash: реальная
    защита — ToolPolicy Фазы 4 (SEC-9).
    """
    link = world["brain"] / "innocent.txt"
    link.hardlink_to(world["env"])
    got = world["sandbox"].resolve("innocent.txt")
    assert got.name == "innocent.txt"


def test_denylisted_hardlink_name_still_blocked(world):
    """А вот hardlink, названный `.env`, deny-list остановит."""
    link = world["brain"] / ".env"
    link.hardlink_to(world["env"])
    with pytest.raises(SandboxError) as exc:
        world["sandbox"].resolve(".env")
    assert exc.value.reason == "denied"


def test_toctou_swap_after_resolve_is_out_of_scope(world):
    """TOCTOU: между resolve() и отправкой файл можно подменить.

    Тест закрепляет ГРАНИЦУ, а не защиту: путь уже проверен, подмена содержимого по тому
    же пути ничем не ловится. Это записано в docstring модуля как известный предел (SEC-9).
    """
    victim = world["brain"] / "report.txt"
    victim.write_text("безобидно", encoding="utf-8")
    checked = world["sandbox"].resolve("report.txt")
    victim.write_text("SECRET", encoding="utf-8")
    assert checked.read_text(encoding="utf-8") == "SECRET"


# ── сообщение человеку: понятная строка, не краш ─────────────────────────────


def test_blocked_message_is_human_readable(world):
    try:
        world["sandbox"].resolve("/etc/passwd")
    except SandboxError as exc:
        msg = blocked_message("/etc/passwd", exc)
    assert "/etc/passwd" in msg
    assert msg.strip() and "Traceback" not in msg


def test_missing_file_message_says_missing(world):
    try:
        world["sandbox"].resolve("docs/ghost.pdf")
    except SandboxError as exc:
        msg = blocked_message("docs/ghost.pdf", exc)
    assert "не наш" not in msg.lower()  # не путаем «нет файла» с «запрещено»
    assert "нет" in msg.lower() or "не найд" in msg.lower()
