"""Негативные тесты установщика: пути к root, которые не должны существовать (SEC-1, Р1).

update.sh стоит в sudoers-whitelist'е Распаковщика (NOPASSWD, от root). Значит любой аргумент
и любой файл, который скрипт сорсит, — это граница доверия: агент с bypassPermissions может
и то и другое подсунуть. Здесь проверяется, что подсунуть НЕЛЬЗЯ:
  * нет способа увести update.sh на чужой каталог движка (не было бы — `source` от root
    исполнил бы подложенный `deploy/_common.sh`; PoC ревью работал);
  * писуемый группой/миром `_common.sh` не сорсится вообще (второй слой той же защиты).

Тесты чёрного ящика: подкладываем полезную нагрузку, которая создаёт файл-маркер, и требуем,
чтобы маркера не появилось.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
UPDATE = REPO / "update.sh"

# Полезная нагрузка «злого» _common.sh: создаёт маркер и притворяется настоящим _common.sh
# (объявляет обе функции), чтобы дальше скрипт не упал раньше, чем мы это заметим.
_EVIL_COMMON = """#!/usr/bin/env bash
touch "{marker}"
resolve_run_identity() {{ RUN_USER=root; RUN_HOME=/root
  AGENTS_BASE=/root/agents; BRAINS_BASE=/root/brains; }}
resolve_uv_bin() {{ UV_BIN=/bin/true; }}
"""


def _evil_engine(tmp_path: Path, marker: Path) -> Path:
    """Каталог, который агент мог бы создать сам: git-репо с подложенным _common.sh."""
    evil = tmp_path / "evil-engine"
    (evil / "deploy").mkdir(parents=True)
    (evil / "deploy" / "_common.sh").write_text(_EVIL_COMMON.format(marker=marker))
    subprocess.run(["git", "init", "-q", str(evil)], check=True, capture_output=True)
    return evil


def _run(*args: str, env_extra: dict[str, str] | None = None, script: Path = UPDATE):
    env = {**os.environ}
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(script), *args], capture_output=True, text=True, env=env, timeout=180
    )


def test_update_has_no_engine_dir_flag_at_all():
    """--engine-dir убран: каталог движка — там, где лежит сам update.sh, и точка.

    Пока флаг существовал, sudoers-правило «update.sh с любыми аргументами» означало
    «source любого файла от root». Флага нет — вектора нет.
    """
    # Комментарии считать нельзя: в шапке флаг упомянут как «почему его нет».
    code = [ln for ln in UPDATE.read_text().splitlines() if not ln.lstrip().startswith("#")]
    assert not [ln for ln in code if "--engine-dir" in ln], (
        "--engine-dir в update.sh — это прямой путь к root (SEC-1): скрипт сорсит "
        "$ENGINE_DIR/deploy/_common.sh ещё до проверок"
    )


def test_update_refuses_engine_dir_flag_and_does_not_source_payload(tmp_path):
    """PoC SEC-1: `update.sh --engine-dir <мой каталог>` не исполняет подложенный код."""
    marker = tmp_path / "PWNED"
    evil = _evil_engine(tmp_path, marker)
    r = _run("--engine-dir", str(evil))
    assert not marker.exists(), (
        "подложенный deploy/_common.sh ИСПОЛНИЛСЯ — это root-RCE через sudoers-whitelist"
    )
    assert r.returncode != 0, "неизвестный флаг обязан быть отказом, а не тихим продолжением"


def test_update_ignores_engine_dir_from_environment(tmp_path):
    """Тот же вектор через окружение: TG_RUNTIME/UNPACKER_ENGINE_DIR тоже не должны уводить.

    `sudo` чистит окружение (env_reset), но полагаться на конфиг sudo как на единственную
    защиту нельзя: update.sh зовут и напрямую.
    """
    marker = tmp_path / "PWNED-ENV"
    evil = _evil_engine(tmp_path, marker)
    for var in ("TG_RUNTIME", "UNPACKER_ENGINE_DIR", "ENGINE_DIR"):
        _run("--dry-run", env_extra={var: str(evil)})
        assert not marker.exists(), f"{var} уводит update.sh на чужой каталог движка (SEC-1)"


def test_install_parses_saved_answers_instead_of_sourcing_them(tmp_path):
    """Файл ответов ПАРСИМ, а не сорсим: `source` конфига = запуск чего угодно от root.

    Ровно тот же класс дыр, что SEC-1: install.sh идёт от root, а значение из файла попадало
    в шелл как код. Файл лежит в /etc, но защита не может держаться на «туда никто не запишет».
    """
    from engine.tests.test_install_scripts import (  # локальный импорт: общие заглушки
        INSTALL,
        _answers,
        _base_stub,
        _fake_engine_repo,
    )

    marker = tmp_path / "PWNED-CONF"
    etc = tmp_path / "etc" / "unpacker"
    etc.mkdir(parents=True)
    (etc / "install.conf").write_text(
        f"UNPACKER_ALLOWED_USERS=111\nUNPACKER_BRAINS_DIR=$(touch {marker})\n"
    )
    stub = _base_stub(tmp_path)
    engine = _fake_engine_repo(tmp_path)
    env = _answers(tmp_path, DEPLOY_ARGV=str(tmp_path / "argv.txt"))
    for k in ("UNPACKER_ALLOWED_USERS", "UNPACKER_BRAINS_DIR"):
        env.pop(k, None)
    subprocess.run(
        [
            "bash",
            str(INSTALL),
            "--ram-mb",
            "8192",
            "--non-interactive",
            "--no-hardening",
            "--engine-dir",
            str(engine),
            "--run-user",
            os.environ.get("USER", "nobody"),
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{stub}:{os.environ['PATH']}", **env},
        timeout=180,
    )
    assert not marker.exists(), "значение из файла ответов исполнилось как команда"


def test_update_refuses_world_writable_common_sh(tmp_path):
    """Второй слой: `_common.sh`, писуемый не только владельцем, не сорсится.

    Сценарий — испорченные права на /opt/unpacker (ученик «починил руками» chmod 777).
    Дешёвая проверка mode & 022 ловит его до того, как root исполнит чужой код.
    """
    marker = tmp_path / "PWNED-MODE"
    engine = tmp_path / "opt" / "unpacker"
    (engine / "deploy").mkdir(parents=True)
    common = engine / "deploy" / "_common.sh"
    common.write_text(_EVIL_COMMON.format(marker=marker))
    common.chmod(0o666)
    subprocess.run(["git", "init", "-q", str(engine)], check=True, capture_output=True)
    upd = engine / "update.sh"
    upd.write_text(UPDATE.read_text())
    upd.chmod(0o755)
    r = _run("--dry-run", script=upd)
    assert not marker.exists(), "писуемый миром _common.sh был засорсен"
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "прав" in out.lower(), f"нужна инструкция, что делать с правами:\n{out}"
