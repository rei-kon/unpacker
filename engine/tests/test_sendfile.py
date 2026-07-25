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
        "sandbox": FileSandbox([brain, state]),
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
