"""TDD для install.sh и update.sh (Фаза 1b/3, §10 конституции).

Скрипты — чёрный ящик через subprocess, как в test_deploy_scripts.py. Реального VPS
(systemd/apt/ufw/сети) в тестах нет, поэтому:
- внешние команды подменяются заглушками в начале PATH (`_stub_bin`) и логируют вызовы;
- `deploy.sh` тоже заглушка — install.sh обязан ЗВАТЬ его, а не дублировать провизию;
- каждый гейт проверяется тестом, а не «на глаз» (критерий приёмки среза).

Живая установка на чистом VPS проверяется смоуком, не юнит-тестом.
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
INSTALL = REPO / "install.sh"
UPDATE = REPO / "update.sh"

# Заглушка общего вида: пишет свой вызов в $STUB_LOG и молча выходит 0.
_STUB_GENERIC = """#!/usr/bin/env bash
printf '%s %s\\n' "$(basename "$0")" "$*" >> "$STUB_LOG"
exit 0
"""

# sudo не логируем, а прозрачно прокидываем — иначе `sudo systemctl` не дошёл бы
# до заглушки systemctl и порядок рестартов было бы нечем проверить.
_STUB_SUDO = """#!/usr/bin/env bash
while [ $# -gt 0 ]; do case "$1" in -n|-H) shift ;; -u) shift 2 ;; *) break ;; esac; done
exec "$@"
"""

_STUB_CLAUDE = """#!/usr/bin/env bash
printf '%s %s\\n' claude "$*" >> "$STUB_LOG"
echo "1.0.0 (Claude Code)"
"""

# sshd -T печатает эффективный конфиг. Именно оттуда установщик обязан узнать реальный порт
# ssh: `ufw allow OpenSSH` открывает только 22 и на нестандартном порту теряет сервер (ADV-15).
_STUB_SSHD = """#!/usr/bin/env bash
printf '%s %s\\n' sshd "$*" >> "$STUB_LOG"
if [ "$1" = "-T" ]; then printf 'port 22\\naddressfamily any\\n'; fi
exit 0
"""

# deploy.sh-заглушка: дословно пишет argv в $DEPLOY_ARGV (по строке на аргумент —
# токен с пробелами не размылся бы) и создаёт .env инстанса, как настоящий скрипт.
_STUB_DEPLOY = """#!/usr/bin/env bash
: > "$DEPLOY_ARGV"
for a in "$@"; do printf '%s\\n' "$a" >> "$DEPLOY_ARGV"; done
name=""; token=""
while [ $# -gt 0 ]; do
  case "$1" in
    --name) name="$2"; shift 2 ;;
    --token) token="$2"; shift 2 ;;
    # секреты приходят файлом (Р5): фиксируем содержимое и права — argv их не увидит
    --token-file)
      token="$(cat "$2")"
      if [ -n "${TOKEN_FILE_REPORT:-}" ]; then
        { printf 'path=%s\\n' "$2"; printf 'content=%s\\n' "$token"
          printf 'mode=%s\\n' "$(ls -l "$2" | cut -c1-10)"; } > "$TOKEN_FILE_REPORT"
      fi
      shift 2 ;;
    --cc-token-file)
      if [ -n "${CC_TOKEN_FILE_REPORT:-}" ]; then
        { printf 'path=%s\\n' "$2"; printf 'content=%s\\n' "$(cat "$2")"
          printf 'mode=%s\\n' "$(ls -l "$2" | cut -c1-10)"; } > "$CC_TOKEN_FILE_REPORT"
      fi
      shift 2 ;;
    *) shift ;;
  esac
done
inst="${TG_AGENTS_BASE:-$HOME/agents}/$name"
mkdir -p "$inst/state"
if [ ! -f "$inst/.env" ]; then
  printf 'TELEGRAM_BOT_TOKEN=%s\\n' "$token" > "$inst/.env"
  chmod 600 "$inst/.env"
fi
echo "==> готово (заглушка deploy.sh)"
"""


def _stub_bin(tmp_path: Path, names: tuple[str, ...]) -> Path:
    """Каталог заглушек, который тест ставит первым в PATH."""
    d = tmp_path / "stubbin"
    d.mkdir(exist_ok=True)
    for n in names:
        special = {"sudo": _STUB_SUDO, "claude": _STUB_CLAUDE, "sshd": _STUB_SSHD}
        body = special.get(n, _STUB_GENERIC)
        p = d / n
        p.write_text(body)
        p.chmod(0o755)
    return d


def _run(
    script: Path, *args: str, env_extra: dict[str, str] | None = None, stub: Path | None = None
):
    env = {**os.environ}
    if stub is not None:
        env["PATH"] = f"{stub}:{env.get('PATH', '')}"
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(script), *args], capture_output=True, text=True, env=env, timeout=180
    )


def run_install(*args: str, **kw):
    return _run(INSTALL, *args, **kw)


def _secret_report(path: Path) -> dict[str, str]:
    """Что заглушка deploy.sh увидела в файле секрета: путь, содержимое, права."""
    assert path.exists(), "deploy.sh не получил секрет файлом (--token-file/--cc-token-file)"
    return dict(
        line.split("=", 1)
        for line in path.read_text().splitlines()
        if "=" in line  # noqa: C416
    )


def run_update(*args: str, **kw):
    return _run(UPDATE, *args, **kw)


# ── install.sh: базовый контракт ────────────────────────────────────────────


def test_install_script_is_executable_bash():
    assert INSTALL.exists(), "install.sh обязателен — это единственная команда новичка"
    assert INSTALL.read_text().startswith("#!/usr/bin/env bash")
    assert os.access(INSTALL, os.X_OK), "install.sh должен быть исполняемым (chmod +x)"


def test_install_help_exits_zero_and_explains():
    r = run_install("--help")
    assert r.returncode == 0, r.stderr
    out = r.stdout.lower()
    assert "install.sh" in out
    assert "--dry-run" in out


def test_install_rejects_unknown_flag():
    r = run_install("--volume-up")
    assert r.returncode != 0
    assert "--volume-up" in r.stderr + r.stdout


# ── install.sh: гейты среды (§10 шаг 1) ─────────────────────────────────────


def test_install_refuses_too_small_ram_with_instruction(tmp_path):
    # (RAM − 1.5ГБ)/1ГБ < 1 → движок не поднимет ни одного агента (§5.1)
    stub = _stub_bin(tmp_path, ("uv", "tmux", "claude", "gh", "sudo"))
    r = run_install(
        "--dry-run",
        "--ram-mb",
        "2048",
        "--non-interactive",
        env_extra={"STUB_LOG": str(tmp_path / "log")},
        stub=stub,
    )
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "RAM" in out
    # провал = инструкция, а не «ошибка»
    assert "4" in out and ("ГБ" in out or "GB" in out), "должен сказать, какой VPS брать"


@pytest.mark.parametrize("ram_mb", [4096, 8192, 16384])
def test_install_prints_pool_ceiling_from_ram(tmp_path, ram_mb):
    """Потолок пула печатается ЧИСЛОМ из engine/core/pool.py, а не второй копией формулы.

    Ассерт по строке потолка, а не «цифра встречается в выводе»: прошлый вариант
    (`assert "6" in out`) выполнялся из-за «chmod 600» в том же выводе — сломать потолок
    можно было любым способом, тест держался (H7).
    """
    from engine.core.pool import compute_pool_ceiling

    stub = _stub_bin(tmp_path, ("uv", "tmux", "claude", "gh", "sudo", "git", "sshd"))
    r = run_install(
        "--dry-run",
        "--ram-mb",
        str(ram_mb),
        "--non-interactive",
        env_extra={
            "STUB_LOG": str(tmp_path / "log"),
            "UNPACKER_BOT_TOKEN": "123456:AAbb",
            "UNPACKER_ALLOWED_USERS": "111",
            "UNPACKER_AUTH_MODE": "subscription",
            "UNPACKER_ETC": str(tmp_path / "etc" / "unpacker"),
        },
        stub=stub,
    )
    out = r.stdout + r.stderr
    expected = compute_pool_ceiling(ram_mb * 1024**2)
    m = re.search(r"потолок тёплого пула:\s*(\d+)", out)
    assert m, f"строки с потолком пула нет в выводе:\n{out}"
    assert int(m.group(1)) == expected, (
        f"{ram_mb} МБ → движок поднимет {expected} агент(ов), а установщик обещает "
        f"{m.group(1)}: расхождение формул = ложное обещание"
    )


def test_install_pool_ceiling_has_single_source_of_truth():
    """M-11: формулы в bash быть не должно — install.sh обязан спрашивать питон.

    Две копии формулы (bash `max(1,…)` против блокера, README — третье) уже разошлись
    по семантике; чинится это не сверкой чисел, а удалением второй копии.
    """
    text = INSTALL.read_text()
    assert "compute_pool_ceiling" in text, "потолок обязан считать engine/core/pool.py"
    code = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    for ln in code:
        assert "1536" not in ln and "POOL_RESERVE_MB" not in ln, (
            f"константы формулы пула скопированы в bash — это вторая копия правды: {ln}"
        )


def test_install_refuses_too_small_disk_with_instruction(tmp_path):
    # порог диска задаётся флагом (он же — тестовый рычаг): места нет → блокер с инструкцией
    stub = _stub_bin(tmp_path, ("uv", "tmux", "claude", "gh", "sudo", "git"))
    r = run_install(
        "--dry-run",
        "--ram-mb",
        "8192",
        "--min-disk-mb",
        "99999999",
        "--non-interactive",
        env_extra={"STUB_LOG": str(tmp_path / "log")},
        stub=stub,
    )
    assert r.returncode != 0
    out = (r.stdout + r.stderr).lower()
    assert "диск" in out or "disk" in out


def test_install_blocks_when_git_missing_and_unfixable(tmp_path):
    """git не работает и поставить его нечем (нет рабочего apt-get) → блокер с инструкцией.

    apt-get тоже заглушен сломанным — иначе тест зависел бы от платформы (на Ubuntu-CI
    install.sh честно поставил бы git сам и вернул 0).
    """
    stub = _stub_bin(tmp_path, ("uv", "tmux", "claude", "sudo"))
    for name in ("git", "apt-get"):
        p = stub / name
        p.write_text("#!/usr/bin/env bash\nexit 127\n")
        p.chmod(0o755)
    r = run_install(
        "--dry-run",
        "--ram-mb",
        "8192",
        "--non-interactive",
        env_extra={"STUB_LOG": str(tmp_path / "log")},
        stub=stub,
    )
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "git" in out.lower()
    assert "apt-get install" in out, "новичку нужна готовая команда, а не факт отсутствия"


def test_install_blocks_root_as_engine_user(tmp_path):
    """run-user = root — безусловный блокер (та же грабля, что в deploy.sh и Makefile).

    Claude CLI отказывается работать с bypassPermissions под root → бот-зомби, молчащий
    на все сообщения. Ученик обязан узнать это ДО установки, а не по молчащему боту.
    """
    stub = _stub_bin(tmp_path, ("uv", "tmux", "claude", "gh", "sudo", "git"))
    r = run_install(
        "--dry-run",
        "--ram-mb",
        "8192",
        "--run-user",
        "root",
        "--non-interactive",
        env_extra={"STUB_LOG": str(tmp_path / "log")},
        stub=stub,
    )
    assert r.returncode != 0
    out = (r.stdout + r.stderr).lower()
    assert "root" in out
    assert "bypass" in out or "непривилегированн" in out


def test_install_tolerates_old_system_python_because_uv_brings_its_own(tmp_path):
    """Ubuntu 22.04 несёт python 3.10 — это НЕ блокер: движок бежит на python от uv.

    Гейт «3.11+» из §10 закрывается uv, поэтому старый системный python — предупреждение
    с объяснением. Хард-фейл здесь отрезал бы половину целевых VPS.
    """
    stub = _stub_bin(tmp_path, ("uv", "tmux", "claude", "gh", "sudo", "git"))
    old_py = stub / "python3"
    old_py.write_text('#!/usr/bin/env bash\necho "Python 3.9.18"\n')
    old_py.chmod(0o755)
    r = run_install(
        "--dry-run",
        "--ram-mb",
        "8192",
        "--non-interactive",
        env_extra={
            "STUB_LOG": str(tmp_path / "log"),
            "UNPACKER_BOT_TOKEN": "123456:AAbb",
            "UNPACKER_ALLOWED_USERS": "111",
            "UNPACKER_AUTH_MODE": "subscription",
        },
        stub=stub,
    )
    out = r.stdout + r.stderr
    assert "3.11" in out and "uv" in out, (
        f"должен объяснить, почему старый python не блокер:\n{out}"
    )
    assert r.returncode == 0, out


# ── install.sh: шаг 0 — мини-hardening (§10.0) ──────────────────────────────


def _base_stub(tmp_path):
    """Полный набор заглушек: ни одна команда не уходит в реальную систему или сеть.

    git тоже заглушен — иначе шаг «код движка» полез бы в github (медленно и флаки).
    Тесты update.sh работают с НАСТОЯЩИМ git: там важна логика тегов.
    """
    return _stub_bin(
        tmp_path,
        (
            "uv",
            "tmux",
            "claude",
            "gh",
            "sudo",
            "ufw",
            "sshd",
            "systemctl",
            "useradd",
            "chown",
            "apt-get",
            "git",
            "tee",
            # curl заглушен намеренно: без него любой промах в логике «ставить ли uv/claude»
            # уходил бы в сеть и писал в /usr/local/bin прямо с прогона тестов.
            "curl",
        ),
    )


def _answers(tmp_path, **over):
    env = {
        "STUB_LOG": str(tmp_path / "stub.log"),
        "UNPACKER_BOT_TOKEN": "123456:AAbbCC-dd_ee",
        "UNPACKER_ALLOWED_USERS": "111,222",
        "UNPACKER_BRAINS_DIR": str(tmp_path / "brains"),
        "UNPACKER_AUTH_MODE": "subscription",
        "TG_AGENTS_BASE": str(tmp_path / "agents"),
        # /etc/unpacker и venv движка — вне дерева кода (Р1/Р2); в тестах уводим в tmp
        "UNPACKER_ETC": str(tmp_path / "etc" / "unpacker"),
        "UV_PROJECT_ENVIRONMENT": str(tmp_path / "venv"),
    }
    env.update(over)
    return env


def _engine_conf(tmp_path) -> dict[str, str]:
    """Разобранный /etc/unpacker/engine.conf — машинный конфиг путей (Р2)."""
    path = tmp_path / "etc" / "unpacker" / "engine.conf"
    assert path.exists(), (
        "install.sh обязан оставить машинный конфиг: без него любая точка входа "
        "(root вручную, sudo от агента, cron) читает свою вселенную путей"
    )
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if line.strip() and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


def test_install_dry_run_plans_hardening_and_touches_nothing(tmp_path):
    stub = _base_stub(tmp_path)
    r = run_install(
        "--dry-run",
        "--ram-mb",
        "8192",
        "--non-interactive",
        "--engine-dir",
        str(tmp_path / "opt" / "unpacker"),
        "--run-user",
        os.environ.get("USER", "nobody"),
        env_extra=_answers(tmp_path),
        stub=stub,
    )
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    low = out.lower()
    assert "ufw" in low, "шаг 0 обязан настраивать файрвол"
    assert "unattended-upgrades" in low, "шаг 0 обязан включать автообновления безопасности"
    assert "ssh" in low, "шаг 0 обязан выключать вход по паролю в ssh"
    assert "[dry-run]" in out
    # ничего не изменено: read-only пробы версий допустимы, мутации — нет
    log = (tmp_path / "stub.log").read_text() if (tmp_path / "stub.log").exists() else ""
    for mutating in (
        "ufw allow",
        "ufw default",
        "ufw --force",
        "useradd",
        "systemctl",
        "apt-get install",
    ):
        assert mutating not in log, f"в dry-run '{mutating}' не должен исполняться:\n{log}"
    assert not (tmp_path / "opt" / "unpacker").exists()
    assert not (tmp_path / "agents").exists()


def test_install_hardening_declinable_by_flag(tmp_path):
    stub = _base_stub(tmp_path)
    r = run_install(
        "--dry-run",
        "--ram-mb",
        "8192",
        "--non-interactive",
        "--no-hardening",
        "--engine-dir",
        str(tmp_path / "opt" / "unpacker"),
        "--run-user",
        os.environ.get("USER", "nobody"),
        env_extra=_answers(tmp_path),
        stub=stub,
    )
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "ufw" not in out.lower(), "--no-hardening обязан отменить шаг 0 целиком"


# ── install.sh: диалог из 4 ответов (§10.4) ─────────────────────────────────


def test_install_non_interactive_names_missing_answer(tmp_path):
    stub = _base_stub(tmp_path)
    env = _answers(tmp_path)
    del env["UNPACKER_BOT_TOKEN"]
    r = run_install("--dry-run", "--ram-mb", "8192", "--non-interactive", env_extra=env, stub=stub)
    assert r.returncode != 0
    assert "UNPACKER_BOT_TOKEN" in r.stdout + r.stderr


def test_install_rejects_malformed_bot_token(tmp_path):
    stub = _base_stub(tmp_path)
    r = run_install(
        "--dry-run",
        "--ram-mb",
        "8192",
        "--non-interactive",
        env_extra=_answers(tmp_path, UNPACKER_BOT_TOKEN="это-не-токен"),
        stub=stub,
    )
    assert r.returncode != 0
    out = (r.stdout + r.stderr).lower()
    assert "токен" in out
    assert "это-не-токен" not in out, "даже кривой ввод не эхоим целиком (может быть настоящим)"


def test_install_rejects_malformed_user_ids(tmp_path):
    stub = _base_stub(tmp_path)
    r = run_install(
        "--dry-run",
        "--ram-mb",
        "8192",
        "--non-interactive",
        env_extra=_answers(tmp_path, UNPACKER_ALLOWED_USERS="@nikita"),
        stub=stub,
    )
    assert r.returncode != 0
    out = (r.stdout + r.stderr).lower()
    assert "id" in out and ("число" in out or "цифр" in out or "userinfobot" in out)


def test_install_refuses_api_contour_with_honest_explanation(tmp_path):
    """API-контур (обслуживание клиентов) — Фаза 4, движок его пока не поднимает.

    Честный отказ с объяснением лучше, чем бот, который поднимется и упадёт: deploy.sh
    физически умеет только контур подписки (§8.1 внутренний).
    """
    stub = _base_stub(tmp_path)
    r = run_install(
        "--dry-run",
        "--ram-mb",
        "8192",
        "--non-interactive",
        env_extra=_answers(tmp_path, UNPACKER_AUTH_MODE="api"),
        stub=stub,
    )
    assert r.returncode != 0
    out = (r.stdout + r.stderr).lower()
    assert "подписк" in out
    assert "фаза" in out or "пока" in out


def test_install_interactive_asks_answers_and_does_not_echo_token(tmp_path):
    """Интерактивный путь: ответы со stdin, токен не появляется ни в выводе, ни в argv."""
    stub = _base_stub(tmp_path)
    engine = _fake_engine_repo(tmp_path)
    secret = "999999:SUPERSECRETTOKEN"
    env = _answers(tmp_path)
    for k in ("UNPACKER_BOT_TOKEN", "UNPACKER_ALLOWED_USERS", "UNPACKER_AUTH_MODE"):
        env.pop(k, None)
    env["DEPLOY_ARGV"] = str(tmp_path / "argv.txt")
    env["TOKEN_FILE_REPORT"] = str(tmp_path / "token.report")
    proc = subprocess.run(
        [
            "bash",
            str(INSTALL),
            "--ram-mb",
            "8192",
            "--no-hardening",
            "--engine-dir",
            str(engine),
            "--run-user",
            os.environ.get("USER", "nobody"),
        ],
        input=f"{secret}\n111,222\n{tmp_path / 'brains'}\n",
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{stub}:{os.environ['PATH']}", **env},
        timeout=180,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert secret not in out, "токен не должен появляться в выводе установщика"
    argv = (tmp_path / "argv.txt").read_text().splitlines()
    assert secret not in argv, "токен в argv sudo/deploy.sh = токен навсегда в /var/log/auth.log"
    assert _secret_report(tmp_path / "token.report")["content"] == secret, (
        "токен обязан дойти до deploy.sh — файлом"
    )


# ── install.sh: bootstrap Распаковщика через deploy.sh (§10.5) ──────────────


def _fake_engine_repo(tmp_path, *, with_brain=True):
    """Каталог движка как после клона: git-репо, deploy.sh-заглушка, мозг Распаковщика."""
    engine = tmp_path / "opt" / "unpacker"
    (engine / "deploy" / "templates").mkdir(parents=True)
    dep = engine / "deploy" / "deploy.sh"
    dep.write_text(_STUB_DEPLOY)
    dep.chmod(0o755)
    # _common.sh берём настоящий: install.sh обязан резолвить пути ТЕМ ЖЕ кодом, что deploy.sh
    (engine / "deploy" / "_common.sh").write_text((REPO / "deploy" / "_common.sh").read_text())
    if with_brain:
        (engine / "brains" / "unpacker").mkdir(parents=True)
        (engine / "brains" / "unpacker" / "CLAUDE.md").write_text("# Распаковщик\n")
    subprocess.run(["git", "init", "-q", str(engine)], check=True)
    return engine


def test_install_bootstraps_unpacker_through_deploy_sh(tmp_path):
    stub = _base_stub(tmp_path)
    engine = _fake_engine_repo(tmp_path)
    argv_log = tmp_path / "argv.txt"
    r = run_install(
        "--ram-mb",
        "8192",
        "--non-interactive",
        "--no-hardening",
        "--engine-dir",
        str(engine),
        "--run-user",
        os.environ.get("USER", "nobody"),
        env_extra=_answers(tmp_path, DEPLOY_ARGV=str(argv_log)),
        stub=stub,
    )
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert argv_log.exists(), (
        f"install.sh обязан ЗВАТЬ deploy.sh, а не дублировать провизию:\n{out}"
    )
    argv = argv_log.read_text().splitlines()
    # детерминированный bootstrap ровно по §10.5
    assert "--surface" in argv and argv[argv.index("--surface") + 1] == "tg"
    assert "--name" in argv and argv[argv.index("--name") + 1] == "unpacker"
    assert argv[argv.index("--brain") + 1] == str(engine / "brains" / "unpacker")
    assert argv[argv.index("--users") + 1] == "111,222"
    # C3/H1: без --role unpacker мета-агент разворачивается БЕЗ прав (нет drop-in, нет
    # sudoers) — и не может развернуть ни одного бота. Главное обещание README мертво.
    assert "--role" in argv and argv[argv.index("--role") + 1] == "unpacker", (
        f"install.sh обязан просить у deploy.sh роль мета-агента:\n{argv}"
    )
    # финал: что делать дальше — одной строкой
    assert "/start" in out


def test_install_blocks_when_unpacker_brain_missing(tmp_path):
    stub = _base_stub(tmp_path)
    engine = _fake_engine_repo(tmp_path, with_brain=False)
    r = run_install(
        "--ram-mb",
        "8192",
        "--non-interactive",
        "--no-hardening",
        "--engine-dir",
        str(engine),
        "--run-user",
        os.environ.get("USER", "nobody"),
        env_extra=_answers(tmp_path, DEPLOY_ARGV=str(tmp_path / "argv.txt")),
        stub=stub,
    )
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "brains/unpacker" in out


def test_install_never_leaks_token_to_output_or_conf(tmp_path):
    stub = _base_stub(tmp_path)
    engine = _fake_engine_repo(tmp_path)
    secret = "777777:VERYSECRETVALUE"
    r = run_install(
        "--ram-mb",
        "8192",
        "--non-interactive",
        "--no-hardening",
        "--engine-dir",
        str(engine),
        "--run-user",
        os.environ.get("USER", "nobody"),
        env_extra=_answers(
            tmp_path, UNPACKER_BOT_TOKEN=secret, DEPLOY_ARGV=str(tmp_path / "argv.txt")
        ),
        stub=stub,
    )
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert secret not in out, "токен не должен светиться в выводе/логе установки"
    # Ответы живут ВНЕ дерева движка: сам движок root:root и go-w (Р1), а конфиг с ответами
    # ученика — в /etc/unpacker, где ему и место.
    assert not (engine / ".install.conf").exists(), (
        "конфиг ответов внутри каталога движка делает дерево кода изменяемым (SEC-2)"
    )
    conf = tmp_path / "etc" / "unpacker" / "install.conf"
    assert conf.exists(), "не-секретные ответы запоминаются для идемпотентного повтора"
    assert oct(conf.stat().st_mode)[-3:] == "600"
    assert secret not in conf.read_text(), "секрет живёт только в .env инстанса (600)"


def test_install_second_run_reuses_answers_and_existing_token(tmp_path):
    """Повторный запуск = обновление: ответы из install.conf, токен — из .env инстанса."""
    stub = _base_stub(tmp_path)
    engine = _fake_engine_repo(tmp_path)
    argv_log = tmp_path / "argv.txt"
    args = (
        "--ram-mb",
        "8192",
        "--non-interactive",
        "--no-hardening",
        "--engine-dir",
        str(engine),
        "--run-user",
        os.environ.get("USER", "nobody"),
    )
    r1 = run_install(*args, env_extra=_answers(tmp_path, DEPLOY_ARGV=str(argv_log)), stub=stub)
    assert r1.returncode == 0, r1.stdout + r1.stderr
    # второй запуск — БЕЗ ответов в окружении
    env2 = {
        "STUB_LOG": str(tmp_path / "stub.log"),
        "TG_AGENTS_BASE": str(tmp_path / "agents"),
        "DEPLOY_ARGV": str(argv_log),
        "UNPACKER_ETC": str(tmp_path / "etc" / "unpacker"),
        "UV_PROJECT_ENVIRONMENT": str(tmp_path / "venv"),
        "TOKEN_FILE_REPORT": str(tmp_path / "token.report"),
    }
    r2 = run_install(*args, env_extra=env2, stub=stub)
    out2 = r2.stdout + r2.stderr
    assert r2.returncode == 0, out2
    argv = argv_log.read_text().splitlines()
    assert argv[argv.index("--users") + 1] == "111,222", "ответы должны переиспользоваться"
    assert _secret_report(tmp_path / "token.report")["content"] == "123456:AAbbCC-dd_ee", (
        "токен берётся из .env инстанса — второй раз его не спрашивают"
    )


def test_install_creates_unprivileged_engine_user_when_absent(tmp_path):
    stub = _base_stub(tmp_path)
    engine = _fake_engine_repo(tmp_path)
    r = run_install(
        "--ram-mb",
        "8192",
        "--non-interactive",
        "--no-hardening",
        "--engine-dir",
        str(engine),
        "--run-user",
        "unpackerghost",
        env_extra=_answers(tmp_path, DEPLOY_ARGV=str(tmp_path / "argv.txt")),
        stub=stub,
    )
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    log = (tmp_path / "stub.log").read_text()
    assert "useradd" in log, f"несуществующий run-user обязан создаваться:\n{log}"
    assert "unpackerghost" in log


def test_install_blocks_private_repo_without_auth_and_names_both_paths(tmp_path):
    """Клон приватного репо без авторизации — главная точка обрыва новичка (§10.3).

    Ответ обязан содержать оба маршрута: дружелюбный (gh, вход по коду) и для продвинутых (PAT).
    """
    stub = _stub_bin(tmp_path, ("uv", "tmux", "claude", "sudo", "apt-get"))
    bad_git = stub / "git"
    bad_git.write_text(
        "#!/usr/bin/env bash\n"
        'printf "git %s\\n" "$*" >> "$STUB_LOG"\n'
        'case "$1" in ls-remote) exit 128 ;; esac\nexit 0\n'
    )
    bad_git.chmod(0o755)
    r = run_install(
        "--ram-mb",
        "8192",
        "--non-interactive",
        "--no-hardening",
        "--engine-dir",
        str(tmp_path / "opt" / "unpacker"),
        "--run-user",
        os.environ.get("USER", "nobody"),
        env_extra=_answers(tmp_path, DEPLOY_ARGV=str(tmp_path / "argv.txt")),
        stub=stub,
    )
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "gh" in out and "PAT" in out
    assert not (tmp_path / "argv.txt").exists(), "до кода движка деплой не запускается"


def test_install_does_not_lock_out_ssh_when_no_key_present(tmp_path):
    """Без ssh-ключа парольный вход НЕ выключаем — иначе ученик теряет сервер навсегда."""
    stub = _base_stub(tmp_path)
    r = run_install(
        "--dry-run",
        "--ram-mb",
        "8192",
        "--non-interactive",
        "--ssh-keys",
        str(tmp_path / "нет-такого-файла"),
        "--engine-dir",
        str(tmp_path / "opt" / "unpacker"),
        "--run-user",
        os.environ.get("USER", "nobody"),
        env_extra=_answers(tmp_path),
        stub=stub,
    )
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "ssh-copy-id" in out, "должен объяснить, как настроить ключ"
    assert "sshd_config" not in out, "конфиг ssh трогать нельзя, пока ключа нет"


# ── install.sh: запуск от root, uv, claude, apt, ufw, права (§10) ───────────
#
# Все эти находки лежат в зоне, которая ни разу не бежала живьём: install.sh запускают
# ОТ ROOT на свежем VPS, а ни один тест этого не делал. `id` — заглушка: она рисует root'а,
# не требуя root'а от прогона тестов.

_STUB_ID_ROOT = """#!/usr/bin/env bash
case "${1:-}" in
  -u) echo 0 ;;
  -un) printf '%s\\n' root ;;
  *) exec /usr/bin/id "$@" ;;
esac
"""


def _root_stub(tmp_path):
    """Полный набор заглушек + `id`, отвечающий «я root» (как на свежем VPS)."""
    stub = _base_stub(tmp_path)
    p = stub / "id"
    p.write_text(_STUB_ID_ROOT)
    p.chmod(0o755)
    return stub


def test_install_survives_being_launched_from_root(tmp_path):
    """C1: документированный путь — `ssh root@IP` → `bash install.sh`. Он обязан работать.

    От root `SUDO` пуст, и конструкция `$SUDO -u <юзер> …` разворачивалась в `-u`, то есть в
    команду `-u`: `-u: command not found`, а `set -e` убивал установку. Ровно на этом
    спотыкался бы каждый ученик, делающий всё по инструкции.
    """
    stub = _root_stub(tmp_path)
    engine = _fake_engine_repo(tmp_path)
    r = run_install(
        "--ram-mb",
        "8192",
        "--non-interactive",
        "--no-hardening",
        "--engine-dir",
        str(engine),
        "--run-user",
        "unpackerghost",
        env_extra=_answers(tmp_path, DEPLOY_ARGV=str(tmp_path / "argv.txt")),
        stub=stub,
    )
    out = r.stdout + r.stderr
    assert "command not found" not in out, f"это и есть C1:\n{out}"
    assert r.returncode == 0, out
    assert (tmp_path / "argv.txt").exists(), "деплой Распаковщика обязан состояться"


def test_install_prints_setup_token_command_that_works_from_root(tmp_path):
    """Та же грабля в печатаемой инструкции: `-u unpacker -H claude setup-token` — не команда."""
    stub = _root_stub(tmp_path)
    engine = _fake_engine_repo(tmp_path)
    r = run_install(
        "--ram-mb",
        "8192",
        "--non-interactive",
        "--no-hardening",
        "--engine-dir",
        str(engine),
        "--run-user",
        "unpackerghost",
        env_extra=_answers(tmp_path, DEPLOY_ARGV=str(tmp_path / "argv.txt")),
        stub=stub,
    )
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "sudo -u unpackerghost -H claude setup-token" in out, (
        f"печатать надо команду, которую можно скопировать и запустить:\n{out}"
    )


def test_install_puts_uv_where_run_user_can_reach_it(tmp_path):
    """ADV-01/C4: uv, поставленный от root, лежит в /root/.local/bin (700).

    Юнит бежит под run-user'ом и до /root не достаёт → preflight деплоя ПРОВАЛ на шаге 5,
    когда ученик уже ввёл все ответы и применил hardening. Ставим системно.
    """
    stub = _stub_bin(tmp_path, ("tmux", "claude", "gh", "sudo", "git", "sshd", "curl"))
    # PATH минимальный: uv не должен находиться «случайно» из окружения разработчика
    r = _run(
        INSTALL,
        "--dry-run",
        "--ram-mb",
        "8192",
        "--non-interactive",
        env_extra={**_answers(tmp_path), "PATH": f"{stub}:/usr/bin:/bin"},
    )
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "UV_INSTALL_DIR=/usr/local/bin" in out, (
        f"uv обязан ставиться системно, а не в домашний каталог root'а:\n{out}"
    )


def test_install_reinstalls_uv_that_run_user_cannot_execute(tmp_path):
    """Ученик уже ставил uv от root руками — install.sh обязан заметить и поставить системно.

    Проверка «есть ли uv» обязана идти В КОНТЕКСТЕ юзера движка: uv в /root/.local/bin
    прекрасно виден root'у и недостижим для run-user'а (mode 700) — юнит падал бы 203/EXEC
    уже на preflight деплоя, после того как ученик ввёл все ответы (ADV-01/C4).
    """
    stub = _stub_bin(tmp_path, ("tmux", "claude", "gh", "git", "sshd", "curl"))
    roothome = tmp_path / "roothome" / ".local" / "bin"
    roothome.mkdir(parents=True)
    (roothome / "uv").write_text("#!/usr/bin/env bash\necho 'uv 0.5.0'\n")
    (roothome / "uv").chmod(0o755)
    # sudo, который «не пускает» юзера движка именно к этому uv — так и ведёт себя реальный
    # /root с правами 700. Всё остальное под этим юзером работает.
    (stub / "sudo").write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "-u" ]; then\n'
        '  case "$*" in *roothome*) exit 1 ;; esac\n'
        "fi\n" + _STUB_SUDO.split("\n", 1)[1]
    )
    (stub / "sudo").chmod(0o755)
    r = _run(
        INSTALL,
        "--dry-run",
        "--ram-mb",
        "8192",
        "--non-interactive",
        "--run-user",
        "unpackerghost",
        env_extra={**_answers(tmp_path), "PATH": f"{roothome}:{stub}:/usr/bin:/bin"},
    )
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "UV_INSTALL_DIR=/usr/local/bin" in out, (
        f"uv, до которого не достаёт юзер движка, надо переставить системно:\n{out}"
    )


def test_install_records_paths_in_machine_config(tmp_path):
    """Р2: /etc/unpacker/engine.conf — единая вселенная путей для всех точек входа.

    Без него `TG_BRAINS_BASE` жил только в процессе установщика и стирался env_reset при
    sudo: первый мозг лёг в один каталог, все следующие Распаковщик кладёт в другой (ADV-09).
    """
    stub = _base_stub(tmp_path)
    engine = _fake_engine_repo(tmp_path)
    r = run_install(
        "--ram-mb",
        "8192",
        "--non-interactive",
        "--no-hardening",
        "--engine-dir",
        str(engine),
        "--run-user",
        os.environ.get("USER", "nobody"),
        env_extra=_answers(tmp_path, DEPLOY_ARGV=str(tmp_path / "argv.txt")),
        stub=stub,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    conf = _engine_conf(tmp_path)
    assert conf["TG_RUN_USER"] == os.environ.get("USER", "nobody")
    assert conf["TG_RUNTIME"] == str(engine)
    assert conf["TG_BRAINS_BASE"] == str(tmp_path / "brains")
    assert conf["TG_AGENTS_BASE"], "инстансы обязаны быть в карте путей"
    assert conf["TG_UV_BIN"], "uv обязан быть в карте путей: юнит зовёт его абсолютным путём"
    assert not conf["TG_UV_BIN"].startswith("/root/"), (
        "uv из /root недостижим для юзера движка (mode 700) — это и есть C4"
    )
    # Стык зон: deploy/_common.sh читает TG_VENV, чтобы `uv sync`/`uv run` от run-user'а не
    # полезли в $RUNTIME/.venv — дерево движка root-owned и go-w (Р1), там запись запрещена.
    # Без этой строки деплой любого нового бота из чата упирается в права, а причина не видна.
    assert conf["TG_VENV"], "venv движка обязан быть в карте путей (Р1: он вынесен из дерева)"
    assert not conf["TG_VENV"].startswith(str(engine)), (
        "venv внутри root-owned дерева движка недостижим для записи юзеру движка"
    )
    path = tmp_path / "etc" / "unpacker" / "engine.conf"
    assert oct(path.stat().st_mode)[-3:] == "644", "карту путей читают все точки входа"


def test_install_blocks_when_claude_is_missing_for_run_user(tmp_path):
    """ADV-16: `claude`, поставленный от root, проходит проверку в оболочке root.

    Под run-user'ом CLI нет → бот поднимется и будет молчать на каждое сообщение, а doctor
    скажет HEALTHY. Проверять надо ИМЕННО в контексте run-user, и отсутствие — блокер.
    """
    stub = _base_stub(tmp_path)
    # sudo, под которым у юзера движка нет claude: любая команда с claude от его имени
    # проваливается. Ровно так выглядит «claude поставлен от root» — CLI есть в оболочке
    # root'а и отсутствует у того, кто реально бежит бота.
    (stub / "sudo").write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "-u" ]; then\n'
        '  case "$*" in *claude*) echo "bash: claude: command not found" >&2; exit 127 ;; esac\n'
        "fi\n" + _STUB_SUDO.split("\n", 1)[1]
    )
    (stub / "sudo").chmod(0o755)
    engine = _fake_engine_repo(tmp_path)
    r = run_install(
        "--ram-mb",
        "8192",
        "--non-interactive",
        "--no-hardening",
        "--engine-dir",
        str(engine),
        "--run-user",
        "unpackerghost",
        env_extra=_answers(tmp_path, DEPLOY_ARGV=str(tmp_path / "argv.txt")),
        stub=stub,
    )
    out = r.stdout + r.stderr
    assert r.returncode != 0, f"без claude у юзера движка бот будет молчать — это блокер:\n{out}"
    # ассерт на ГОТОВУЮ команду починки: «слово claude встретилось в выводе» выполнялось бы
    # случайно — install.sh печатает «✓ claude уже стоит» ровно про оболочку root'а
    assert "claude.ai/install.sh" in out, f"нужна команда, которой ученик это починит:\n{out}"
    assert "unpackerghost" in out, "и имя юзера, под которым CLI должен появиться"
    assert "установка остановлена" in out, "это блокер, а не warn"
    assert not (tmp_path / "argv.txt").exists(), "деплой такого бота запускать нельзя"


def test_install_opens_real_ssh_port_in_firewall(tmp_path):
    """ADV-15: `ufw allow OpenSSH` открывает ровно 22/tcp.

    Если sshd слушает другой порт, текущая сессия выживает (ESTABLISHED), а следующий вход
    невозможен — потеря сервера. Порт берём из `sshd -T`.
    """
    stub = _base_stub(tmp_path)
    (stub / "sshd").write_text(
        "#!/usr/bin/env bash\n"
        'printf "sshd %s\\n" "$*" >> "$STUB_LOG"\n'
        'if [ "$1" = "-T" ]; then printf "port 2222\\nport 22\\n"; fi\nexit 0\n'
    )
    (stub / "sshd").chmod(0o755)
    engine = _fake_engine_repo(tmp_path)
    r = run_install(
        "--ram-mb",
        "8192",
        "--non-interactive",
        "--engine-dir",
        str(engine),
        "--run-user",
        os.environ.get("USER", "nobody"),
        "--ssh-keys",
        str(tmp_path / "нет-ключа"),
        env_extra=_answers(tmp_path, DEPLOY_ARGV=str(tmp_path / "argv.txt")),
        stub=stub,
    )
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    log = (tmp_path / "stub.log").read_text()
    assert "ufw allow 2222/tcp" in log, f"настоящий порт ssh обязан быть открыт:\n{log}"
    assert "ufw allow 22/tcp" in log, "второй порт из конфига тоже открываем"
    assert "allow OpenSSH" not in log, "профиль OpenSSH — это только 22, он теряет сервер"


def test_install_does_not_enable_firewall_when_ssh_port_unknown(tmp_path):
    """Не смог определить порт ssh — файрвол не включаем и говорим почему.

    `ufw --force enable` с политикой deny и без правила на реальный порт = сервер потерян.
    Fail-closed здесь означает «не трогать файрвол», а не «включить наугад».
    """
    stub = _base_stub(tmp_path)
    (stub / "sshd").write_text("#!/usr/bin/env bash\nexit 1\n")  # sshd не отвечает
    (stub / "sshd").chmod(0o755)
    engine = _fake_engine_repo(tmp_path)
    r = run_install(
        "--ram-mb",
        "8192",
        "--non-interactive",
        "--engine-dir",
        str(engine),
        "--run-user",
        os.environ.get("USER", "nobody"),
        "--ssh-keys",
        str(tmp_path / "нет-ключа"),
        env_extra=_answers(tmp_path, DEPLOY_ARGV=str(tmp_path / "argv.txt")),
        stub=stub,
    )
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    log = (tmp_path / "stub.log").read_text()
    assert "ufw --force enable" not in log, f"вслепую файрвол включать нельзя:\n{log}"
    assert "порт" in out.lower() and "ufw" in out.lower(), "ученик обязан узнать, что и почему"


def test_install_updates_apt_indexes_before_installing(tmp_path):
    """C15: без `apt-get update` установка падает сырой ошибкой apt на любом залежавшемся VPS."""
    stub = _base_stub(tmp_path)
    (stub / "tmux").write_text("#!/usr/bin/env bash\nexit 127\n")  # инструмента нет — поставь
    (stub / "tmux").chmod(0o755)
    engine = _fake_engine_repo(tmp_path)
    run_install(
        "--ram-mb",
        "8192",
        "--non-interactive",
        "--no-hardening",
        "--engine-dir",
        str(engine),
        "--run-user",
        os.environ.get("USER", "nobody"),
        env_extra=_answers(tmp_path, DEPLOY_ARGV=str(tmp_path / "argv.txt")),
        stub=stub,
    )
    log = (tmp_path / "stub.log").read_text()
    upd = log.find("apt-get update")
    ins = log.find("apt-get install")
    assert upd >= 0, f"apt-get update обязателен перед установкой пакетов:\n{log}"
    assert ins > upd, f"индексы обновляются ДО установки:\n{log}"


def test_install_blocks_with_instruction_when_apt_install_fails(tmp_path):
    """C15: отказ apt — это инструкция «сделай вот это», а не сырой стек apt."""
    stub = _base_stub(tmp_path)
    (stub / "tmux").write_text("#!/usr/bin/env bash\nexit 127\n")
    (stub / "tmux").chmod(0o755)
    (stub / "apt-get").write_text(
        "#!/usr/bin/env bash\n"
        'printf "apt-get %s\\n" "$*" >> "$STUB_LOG"\n'
        'case "$1" in install) echo "E: Unable to locate package tmux" >&2; exit 100 ;; esac\n'
        "exit 0\n"
    )
    (stub / "apt-get").chmod(0o755)
    r = run_install(
        "--ram-mb",
        "8192",
        "--non-interactive",
        "--no-hardening",
        "--engine-dir",
        str(tmp_path / "opt" / "unpacker"),
        "--run-user",
        os.environ.get("USER", "nobody"),
        env_extra=_answers(tmp_path, DEPLOY_ARGV=str(tmp_path / "argv.txt")),
        stub=stub,
    )
    out = r.stdout + r.stderr
    assert r.returncode != 0, out
    assert "tmux" in out and "apt-get install" in out, "нужна готовая команда"
    assert not (tmp_path / "argv.txt").exists(), "с неполной средой деплой не запускаем"


def test_install_waits_for_dpkg_lock_and_explains_on_timeout(tmp_path):
    """ADV-04: свежий VPS занят cloud-init'ом, dpkg-lock держится минуту-другую.

    Без ожидания установка падает сырой ошибкой «Could not get lock» на первой же минуте
    жизни сервера. Ждём, а если так и не отпустил — объясняем, что происходит.
    """
    stub = _base_stub(tmp_path)
    (stub / "tmux").write_text("#!/usr/bin/env bash\nexit 127\n")
    (stub / "tmux").chmod(0o755)
    (stub / "fuser").write_text("#!/usr/bin/env bash\nexit 0\n")  # лок держат всегда
    (stub / "fuser").chmod(0o755)
    r = run_install(
        "--ram-mb",
        "8192",
        "--non-interactive",
        "--no-hardening",
        "--engine-dir",
        str(tmp_path / "opt" / "unpacker"),
        "--run-user",
        os.environ.get("USER", "nobody"),
        env_extra=_answers(tmp_path, DEPLOY_ARGV=str(tmp_path / "argv.txt"), APT_LOCK_WAIT_SEC="2"),
        stub=stub,
    )
    out = r.stdout + r.stderr
    assert "жду" in out.lower(), f"ожидание лока обязано быть видимым:\n{out}"
    assert r.returncode != 0
    assert "cloud-init" in out or "dpkg" in out, "ученик обязан понять, кто держит лок"


def test_install_leaves_engine_tree_root_owned_and_read_only(tmp_path):
    """Р1/SEC-2/C16: каталог движка — root:root и go-w.

    Иначе агент с bypassPermissions перепишет любой скрипт, на который выдан NOPASSWD
    (и заодно git-вызовы от root упрутся в safe.directory: «в репо нет тегов» на репо с тегами).
    """
    stub = _base_stub(tmp_path)
    engine = _fake_engine_repo(tmp_path)
    me = os.environ.get("USER", "nobody")
    # Входное состояние — испорченные права: так выглядит и «починка руками» (chmod 777),
    # и прежнее поведение установщика, отдававшего дерево движка юзеру движка.
    for p in (engine, engine / "deploy", engine / "deploy" / "deploy.sh"):
        p.chmod(0o777)
    (engine / "deploy" / "_common.sh").chmod(0o666)
    r = run_install(
        "--ram-mb",
        "8192",
        "--non-interactive",
        "--no-hardening",
        "--engine-dir",
        str(engine),
        "--run-user",
        me,
        env_extra=_answers(tmp_path, DEPLOY_ARGV=str(tmp_path / "argv.txt")),
        stub=stub,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    log = (tmp_path / "stub.log").read_text()
    assert f"chown -R root:root {engine}" in log, f"движок обязан принадлежать root:\n{log}"
    assert f"chown -R {me} {engine}" not in log, (
        "отдать дерево движка run-user'у = отдать ему все whitelisted-скрипты (SEC-2)"
    )
    # Пост-условие, а не запись в логе: НИ ОДИН файл движка не писуем никем, кроме владельца.
    # Это и есть условие безопасности из sudoers-шаблона.
    writable = [
        p
        for p in [
            engine,
            engine / "deploy",
            engine / "deploy" / "deploy.sh",
            engine / "deploy" / "_common.sh",
        ]
        if p.stat().st_mode & 0o022
    ]
    assert not writable, f"писуемо группой/миром: {writable} — агент перепишет whitelisted-скрипт"


def test_install_gives_run_user_write_only_on_venv_outside_engine(tmp_path):
    """Р1: venv выносится ИЗ дерева движка и принадлежит run-user'у.

    Иначе требования взаимоисключающие: `uv sync` хочет писать в дерево, а sudoers-whitelist
    требует, чтобы дерево было неизменяемым.
    """
    stub = _base_stub(tmp_path)
    engine = _fake_engine_repo(tmp_path)
    me = os.environ.get("USER", "nobody")
    r = run_install(
        "--ram-mb",
        "8192",
        "--non-interactive",
        "--no-hardening",
        "--engine-dir",
        str(engine),
        "--run-user",
        me,
        env_extra=_answers(tmp_path, DEPLOY_ARGV=str(tmp_path / "argv.txt")),
        stub=stub,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    venv = tmp_path / "venv"
    log = (tmp_path / "stub.log").read_text()
    assert f"chown -R {me} {venv}" in log, f"venv обязан принадлежать юзеру движка:\n{log}"
    assert str(engine) not in str(venv), "venv не может лежать внутри дерева движка"


def test_install_passes_secrets_to_deploy_as_files_not_argv(tmp_path):
    """Р5/SEC-4: секрет в argv `sudo` — это секрет навсегда в /var/log/auth.log.

    Файл 600 читается один раз и удаляется. Через argv токен подписки утекал ещё и в
    history root'а, и в ~/.claude/projects/*.jsonl, которые агент читает сам.
    """
    stub = _base_stub(tmp_path)
    engine = _fake_engine_repo(tmp_path)
    bot = "555555:BOTSECRET-xyz"
    cc = "sk-ant-oat01-CCSECRET"
    r = run_install(
        "--ram-mb",
        "8192",
        "--non-interactive",
        "--no-hardening",
        "--engine-dir",
        str(engine),
        "--run-user",
        os.environ.get("USER", "nobody"),
        env_extra=_answers(
            tmp_path,
            UNPACKER_BOT_TOKEN=bot,
            UNPACKER_CC_TOKEN=cc,
            DEPLOY_ARGV=str(tmp_path / "argv.txt"),
            TOKEN_FILE_REPORT=str(tmp_path / "token.report"),
            CC_TOKEN_FILE_REPORT=str(tmp_path / "cc.report"),
        ),
        stub=stub,
    )
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    argv = (tmp_path / "argv.txt").read_text()
    assert bot not in argv and cc not in argv, f"секреты не должны попадать в argv:\n{argv}"
    assert "--token-file" in argv and "--cc-token-file" in argv
    tok = _secret_report(tmp_path / "token.report")
    assert tok["content"] == bot
    assert tok["mode"] == "-rw-------", f"файл секрета — только владельцу: {tok}"
    assert _secret_report(tmp_path / "cc.report")["content"] == cc
    # файл-носитель живёт ровно на время деплоя
    assert not Path(tok["path"]).exists(), "файл секрета обязан удаляться после деплоя"


def test_install_tells_how_to_pass_cc_token_by_file(tmp_path):
    """Р5: то, что печатается ученику, тоже не должно учить светить секрет в argv."""
    stub = _base_stub(tmp_path)
    engine = _fake_engine_repo(tmp_path)
    r = run_install(
        "--ram-mb",
        "8192",
        "--non-interactive",
        "--no-hardening",
        "--engine-dir",
        str(engine),
        "--run-user",
        os.environ.get("USER", "nobody"),
        env_extra=_answers(tmp_path, DEPLOY_ARGV=str(tmp_path / "argv.txt")),
        stub=stub,
    )
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "UNPACKER_CC_TOKEN_FILE" in out, (
        f"ученику надо показать путь через файл, а не «UNPACKER_CC_TOKEN=<токен> bash …»:\n{out}"
    )
    assert "UNPACKER_CC_TOKEN=<" not in out, "это учит вписать секрет в командную строку"


def test_install_prints_only_whitelisted_command_forms(tmp_path):
    """M-18: `sudo bash update.sh` даёт «user is not allowed to execute /bin/bash».

    В sudoers-whitelist'е стоят сами скрипты; оболочек там нет сознательно.
    """
    stub = _base_stub(tmp_path)
    engine = _fake_engine_repo(tmp_path)
    r = run_install(
        "--ram-mb",
        "8192",
        "--non-interactive",
        "--no-hardening",
        "--engine-dir",
        str(engine),
        "--run-user",
        os.environ.get("USER", "nobody"),
        env_extra=_answers(tmp_path, DEPLOY_ARGV=str(tmp_path / "argv.txt")),
        stub=stub,
    )
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "sudo bash" not in out, f"под sudo оболочка запрещена whitelist'ом:\n{out}"
    assert f"sudo {engine}/update.sh" in out, "обновление печатаем в разрешённой форме"
    assert f"sudo {engine}/deploy/agentctl.sh doctor" in out, "и диагностику тоже"


def test_install_refuses_api_contour_before_asking_anything(tmp_path):
    """M-12: четвёртый вопрос — тупик: выбор «API-ключ» гарантированно обрывал установку.

    Причём уже ПОСЛЕ ввода токена и остальных ответов. Отказ обязан случиться раньше, чем
    ученик что-то введёт, — иначе это просто издевательство.
    """
    stub = _base_stub(tmp_path)
    engine = _fake_engine_repo(tmp_path)
    env = _answers(tmp_path, UNPACKER_AUTH_MODE="api", DEPLOY_ARGV=str(tmp_path / "argv.txt"))
    for k in ("UNPACKER_BOT_TOKEN", "UNPACKER_ALLOWED_USERS"):
        env.pop(k, None)
    proc = subprocess.run(
        [
            "bash",
            str(INSTALL),
            "--ram-mb",
            "8192",
            "--no-hardening",
            "--engine-dir",
            str(engine),
            "--run-user",
            os.environ.get("USER", "nobody"),
        ],
        input="",  # ученик ещё ничего не ввёл
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{stub}:{os.environ['PATH']}", **env},
        timeout=180,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out
    assert "подписк" in out.lower()
    assert "токен" not in out.lower().split("контур")[0], (
        f"про токен спрашивать раньше отказа нельзя:\n{out}"
    )


def test_install_does_not_offer_unsupported_auth_choice(tmp_path):
    """M-12 (вторая половина): не предлагай выбор, который гарантированно провалится."""
    text = INSTALL.read_text()
    assert "2 — API-ключ" not in text, (
        "вариант «API-ключ» в диалоге — это кнопка «оборвать установку»: контур появится в Фазе 4"
    )


# ── update.sh: релизы по тегам, бэкапы, откат (§10) ─────────────────────────
#
# Здесь git НАСТОЯЩИЙ: смысл update.sh — логика тегов, заглушка её бы стёрла.
# Заглушены только uv (не тянуть зависимости) и systemctl/sudo (нет systemd).


def _git(*args, cwd):
    subprocess.run(
        ["git", "-c", "user.email=t@t.t", "-c", "user.name=t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _engine_with_tags(tmp_path):
    """Репо движка с двумя релизами и одним коммитом ПОСЛЕ последнего тега.

    Коммит после тега — это и есть проверяемая ситуация: «плохой пуш владельца» не должен
    приезжать ученикам, update.sh обязан встать на тег, а не на HEAD.

    update.sh КОПИРУЕТСЯ внутрь — ровно как на сервере (/opt/unpacker/update.sh). Каталог
    движка он берёт из своего расположения, флага --engine-dir больше нет (SEC-1).
    """
    engine = tmp_path / "opt" / "unpacker"
    (engine / "deploy").mkdir(parents=True)
    (engine / "deploy" / "_common.sh").write_text((REPO / "deploy" / "_common.sh").read_text())
    (engine / "engine").mkdir()
    upd = engine / "update.sh"
    upd.write_text(UPDATE.read_text())
    upd.chmod(0o755)
    subprocess.run(["git", "init", "-q", str(engine)], check=True, capture_output=True)
    (engine / "VERSION").write_text("v0.1.0\n")
    _git("add", "-A", cwd=str(engine))
    _git("commit", "-qm", "v0.1.0", cwd=str(engine))
    _git("tag", "v0.1.0", cwd=str(engine))
    (engine / "VERSION").write_text("v0.2.0\n")
    _git("commit", "-aqm", "v0.2.0", cwd=str(engine))
    _git("tag", "v0.2.0", cwd=str(engine))
    (engine / "VERSION").write_text("сломанный пуш владельца\n")
    _git("commit", "-aqm", "bad push", cwd=str(engine))
    return engine


def _sqlite_db(path: Path, rows: int = 3) -> None:
    """НАСТОЯЩАЯ база SQLite: бэкап проверяется чтением, а не сравнением байтов.

    Текстовая «заглушка» тут не годится осознанно: согласованный снимок делает
    `sqlite3 .backup`/`Connection.backup`, и на не-базе он обязан честно провалиться (ADV-12).
    """
    con = sqlite3.connect(path)
    with con:
        con.execute("CREATE TABLE IF NOT EXISTS sessions(id INTEGER PRIMARY KEY, text TEXT)")
        con.executemany(
            "INSERT INTO sessions(text) VALUES(?)", [(f"переписка {i}",) for i in range(rows)]
        )
    con.close()


def _db_rows(path: Path) -> list[str]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return [r[0] for r in con.execute("SELECT text FROM sessions ORDER BY id")]
    finally:
        con.close()


def _instances(tmp_path, names=("unpacker", "sales")):
    base = tmp_path / "agents"
    for n in names:
        (base / n / "state").mkdir(parents=True)
        (base / n / ".env").write_text("TELEGRAM_BOT_TOKEN=1:x\n")
        _sqlite_db(base / n / "state" / "state.db")
    return base


def _upd_env(tmp_path, engine, **over):
    env = {
        "STUB_LOG": str(tmp_path / "stub.log"),
        "TG_AGENTS_BASE": str(tmp_path / "agents"),
        "TG_RUN_USER": os.environ.get("USER", "nobody"),
        # машинный конфиг и venv — вне дерева движка (Р1/Р2); в тестах уводим в tmp
        "UNPACKER_ETC": str(tmp_path / "etc" / "unpacker"),
        "UV_PROJECT_ENVIRONMENT": str(tmp_path / "venv"),
    }
    env.update(over)
    return env


def run_update_in(engine: Path, *args: str, **kw):
    """Запуск update.sh ИЗ каталога движка — единственная поддерживаемая форма (SEC-1)."""
    return _run(engine / "update.sh", *args, **kw)


def test_update_script_exists_and_helps():
    assert UPDATE.exists(), "update.sh обязателен: обновление и откат без ssh-магии"
    assert os.access(UPDATE, os.X_OK)
    r = run_update("--help")
    assert r.returncode == 0, r.stderr
    assert "--rollback" in r.stdout and "--dry-run" in r.stdout
    assert "--restore-db" in r.stdout, "откат КОДА без восстановления ДАННЫХ — половина операции"


def test_update_checks_out_latest_tag_not_head(tmp_path):
    stub = _stub_bin(tmp_path, ("uv", "systemctl", "sudo"))
    engine = _engine_with_tags(tmp_path)
    _instances(tmp_path)
    r = run_update_in(engine, env_extra=_upd_env(tmp_path, engine), stub=stub)
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert (engine / "VERSION").read_text().strip() == "v0.2.0", (
        "должен встать на последний ТЕГ, а не на HEAD ветки (плохой пуш владельца)"
    )
    assert "v0.2.0" in out


def test_update_backup_is_consistent_sqlite_snapshot(tmp_path):
    """Бэкап — читаемая база с теми же строками, а не «копия файла» (ADV-12)."""
    stub = _stub_bin(tmp_path, ("uv", "systemctl", "sudo"))
    engine = _engine_with_tags(tmp_path)
    base = _instances(tmp_path)
    before = _db_rows(base / "unpacker" / "state" / "state.db")
    r = run_update_in(engine, env_extra=_upd_env(tmp_path, engine), stub=stub)
    assert r.returncode == 0, r.stdout + r.stderr
    copies = sorted((base / "unpacker" / "state" / "backups").glob("*/state.db"))
    assert copies, "перед миграциями обязан лежать бэкап БД сессий"
    assert _db_rows(copies[0]) == before, (
        "бэкап обязан открываться как база и содержать те же сессии"
    )


def test_update_refuses_to_proceed_when_backup_impossible(tmp_path):
    """Согласованный снимок не сделался → обновления НЕ будет (fail-closed, ADV-12).

    Раньше здесь стоял `cp` живой WAL-базы: копия выходила битой или без последних сессий,
    а скрипт бодро писал «бэкап (копия)» и шёл дальше.
    """
    stub = _stub_bin(tmp_path, ("uv", "systemctl", "sudo"))
    engine = _engine_with_tags(tmp_path)
    base = _instances(tmp_path)
    (base / "unpacker" / "state" / "state.db").write_text("это вообще не sqlite\n")
    r = run_update_in(engine, env_extra=_upd_env(tmp_path, engine), stub=stub)
    out = r.stdout + r.stderr
    assert r.returncode != 0, f"без бэкапа обновляться нельзя:\n{out}"
    assert "sqlite3" in out, "нужна готовая команда починки"
    assert (engine / "VERSION").read_text().strip() != "v0.2.0", "код не должен быть переключён"


def test_update_backs_up_state_db_before_switching(tmp_path):
    """M5: «до переключения» проверяется срывом переключения, а не фактом файла.

    Рабочее дерево делаем грязным по VERSION — `git checkout` на тег обязан отказаться.
    Если бэкап делается ПОСЛЕ checkout, его после этого падения не будет.
    """
    stub = _stub_bin(tmp_path, ("uv", "systemctl", "sudo"))
    engine = _engine_with_tags(tmp_path)
    base = _instances(tmp_path)
    (engine / "VERSION").write_text("правка ученика прямо в /opt\n")
    r = run_update_in(engine, env_extra=_upd_env(tmp_path, engine), stub=stub)
    assert r.returncode != 0, "checkout поверх незакоммиченной правки обязан провалиться"
    copies = sorted((base / "unpacker" / "state" / "backups").glob("*/state.db"))
    assert copies, "бэкап обязан быть сделан РАНЬШЕ переключения кода"


def test_update_restarts_initiator_last(tmp_path):
    """Юнит Распаковщика рестартуется последним: иначе он убьёт себя посреди операции."""
    stub = _stub_bin(tmp_path, ("uv", "systemctl", "sudo"))
    engine = _engine_with_tags(tmp_path)
    _instances(tmp_path, names=("alpha", "unpacker", "zeta"))
    r = run_update_in(engine, env_extra=_upd_env(tmp_path, engine), stub=stub)
    assert r.returncode == 0, r.stdout + r.stderr
    # фильтруем по началу строки: путь tmp_path сам содержит слово restart
    restarts = [
        ln
        for ln in (tmp_path / "stub.log").read_text().splitlines()
        if ln.startswith("systemctl restart")
    ]
    assert len(restarts) == 3, f"должны рестартоваться все три юнита:\n{restarts}"
    assert "unpacker" in restarts[-1], f"инициатор — последним:\n{restarts}"
    assert "unpacker" not in "\n".join(restarts[:-1])


def test_update_does_not_die_midway_when_one_unit_is_missing(tmp_path):
    """C12: у папки без юнита рестарт падает — остальных это не должно лишать обновления.

    И «готово» в конце не печатается: часть ботов осталась лежать.
    """
    stub = _stub_bin(tmp_path, ("uv", "sudo"))
    sysctl = stub / "systemctl"
    sysctl.write_text(
        "#!/usr/bin/env bash\n"
        'printf "systemctl %s\\n" "$*" >> "$STUB_LOG"\n'
        'if [ "$*" = "restart agent-tg@alpha" ]; then\n'
        '  echo "Failed to restart agent-tg@alpha.service: Unit not found." >&2; exit 5\nfi\n'
        "exit 0\n"
    )
    sysctl.chmod(0o755)
    engine = _engine_with_tags(tmp_path)
    _instances(tmp_path, names=("alpha", "unpacker", "zeta"))
    r = run_update_in(engine, env_extra=_upd_env(tmp_path, engine), stub=stub)
    out = r.stdout + r.stderr
    restarts = [
        ln
        for ln in (tmp_path / "stub.log").read_text().splitlines()
        if ln.startswith("systemctl restart")
    ]
    assert len(restarts) == 3, f"падение одного юнита не должно рвать обновление:\n{restarts}"
    assert r.returncode != 0, "часть ботов лежит — это не успех"
    assert "==> готово" not in out, f"«готово», не сделав дела:\n{out}"
    assert "journalctl" in out, "нужна команда, чтобы посмотреть причину"


def test_update_dry_run_changes_nothing(tmp_path):
    stub = _stub_bin(tmp_path, ("uv", "systemctl", "sudo"))
    engine = _engine_with_tags(tmp_path)
    base = _instances(tmp_path)
    before = (engine / "VERSION").read_text()
    r = run_update_in(engine, "--dry-run", env_extra=_upd_env(tmp_path, engine), stub=stub)
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "[dry-run]" in out
    assert (engine / "VERSION").read_text() == before, "dry-run не переключает код"
    assert not (base / "unpacker" / "state" / "backups").exists(), "dry-run не делает бэкапов"
    log = (tmp_path / "stub.log").read_text() if (tmp_path / "stub.log").exists() else ""
    assert "systemctl restart" not in log, "dry-run не рестартует ботов"


def test_update_is_idempotent_on_second_run(tmp_path):
    stub = _stub_bin(tmp_path, ("uv", "systemctl", "sudo"))
    engine = _engine_with_tags(tmp_path)
    _instances(tmp_path)
    r1 = run_update_in(engine, env_extra=_upd_env(tmp_path, engine), stub=stub)
    assert r1.returncode == 0, r1.stdout + r1.stderr
    r2 = run_update_in(engine, env_extra=_upd_env(tmp_path, engine), stub=stub)
    out2 = r2.stdout + r2.stderr
    assert r2.returncode == 0, out2
    assert "уже" in out2, "повторный запуск обязан сказать «уже на последнем релизе», а не чинить"
    assert (engine / "VERSION").read_text().strip() == "v0.2.0"


def test_update_rollback_returns_previous_tag(tmp_path):
    stub = _stub_bin(tmp_path, ("uv", "systemctl", "sudo"))
    engine = _engine_with_tags(tmp_path)
    _instances(tmp_path)
    # встаём на v0.1.0, затем обновляемся до v0.2.0 — чтобы «предыдущий» был осмысленным
    subprocess.run(
        ["git", "-C", str(engine), "checkout", "--quiet", "v0.1.0"], check=True, capture_output=True
    )
    r1 = run_update_in(engine, env_extra=_upd_env(tmp_path, engine), stub=stub)
    assert r1.returncode == 0, r1.stdout + r1.stderr
    assert (engine / "VERSION").read_text().strip() == "v0.2.0"
    r2 = run_update_in(engine, "--rollback", env_extra=_upd_env(tmp_path, engine), stub=stub)
    out2 = r2.stdout + r2.stderr
    assert r2.returncode == 0, out2
    assert (engine / "VERSION").read_text().strip() == "v0.1.0", "откат обязан вернуть прошлый тег"
    assert "v0.1.0" in out2


def test_update_rollback_without_history_explains(tmp_path):
    stub = _stub_bin(tmp_path, ("uv", "systemctl", "sudo"))
    engine = _engine_with_tags(tmp_path)
    _instances(tmp_path)
    r = run_update_in(engine, "--rollback", env_extra=_upd_env(tmp_path, engine), stub=stub)
    assert r.returncode != 0
    out = (r.stdout + r.stderr).lower()
    assert "откат" in out or "rollback" in out
    assert "--ref" in out, "должен подсказать, как встать на конкретный релиз руками"


def test_update_restores_databases_from_backup(tmp_path):
    """--restore-db: откат КОДА без возврата ДАННЫХ — половина операции (ADV-12).

    Сценарий ученика: обновился, миграция новой версии переписала базу, откатил код —
    и остался с базой из будущего. Возврат данных обязан быть отдельной командой.
    """
    stub = _stub_bin(tmp_path, ("uv", "systemctl", "sudo"))
    engine = _engine_with_tags(tmp_path)
    base = _instances(tmp_path)
    db = base / "unpacker" / "state" / "state.db"
    good = _db_rows(db)
    r1 = run_update_in(engine, env_extra=_upd_env(tmp_path, engine), stub=stub)
    assert r1.returncode == 0, r1.stdout + r1.stderr
    # «миграция новой версии» испортила базу и оставила мусорный -wal
    db.unlink()
    _sqlite_db(db, rows=1)
    (base / "unpacker" / "state" / "state.db-wal").write_text("мусор от прерванной миграции")
    r2 = run_update_in(engine, "--restore-db", env_extra=_upd_env(tmp_path, engine), stub=stub)
    out2 = r2.stdout + r2.stderr
    assert r2.returncode == 0, out2
    assert _db_rows(db) == good, "база обязана вернуться к состоянию до обновления"
    assert not (base / "unpacker" / "state" / "state.db-wal").exists(), (
        "оставленный -wal подмешал бы к восстановленной базе чужие страницы"
    )
    log = (tmp_path / "stub.log").read_text()
    assert "systemctl stop agent-tg@unpacker" in log, "бота обязательно остановить перед подменой"
    assert "systemctl start agent-tg@unpacker" in log, "и поднять обратно"


def test_update_restore_db_without_backups_says_so(tmp_path):
    stub = _stub_bin(tmp_path, ("uv", "systemctl", "sudo"))
    engine = _engine_with_tags(tmp_path)
    _instances(tmp_path)
    r = run_update_in(engine, "--restore-db", env_extra=_upd_env(tmp_path, engine), stub=stub)
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "бэкап" in out.lower()


def test_update_stops_when_dependencies_fail_and_offers_rollback(tmp_path):
    """C13: код переключён, uv sync провалился → нельзя молчать и нельзя рестартовать ботов.

    Ровно этот случай приезжает с новым релизом, добавившим зависимость: код новый,
    библиотеки старые. Ученику нужна одна команда — --rollback.
    """
    stub = _stub_bin(tmp_path, ("systemctl", "sudo"))
    bad_uv = stub / "uv"
    bad_uv.write_text(
        "#!/usr/bin/env bash\n"
        'printf "uv %s\\n" "$*" >> "$STUB_LOG"\n'
        'echo "error: Failed to download pyyaml" >&2\nexit 1\n'
    )
    bad_uv.chmod(0o755)
    engine = _engine_with_tags(tmp_path)
    _instances(tmp_path)
    r = run_update_in(engine, env_extra=_upd_env(tmp_path, engine), stub=stub)
    out = r.stdout + r.stderr
    assert r.returncode != 0, out
    assert "--rollback" in out, "ученик обязан узнать, как вернуться, из этого же сообщения"
    assert "v0.1.0" in out or "v0.2.0" in out, "и на какой релиз он вернётся"
    log = (tmp_path / "stub.log").read_text()
    assert "systemctl restart" not in log, "на битых зависимостях ботов не рестартуем"
    assert "готово" not in out


def test_update_reports_git_failure_instead_of_no_tags(tmp_path):
    """C16: жалоба git ≠ «в репо нет тегов».

    Каталог движка с чужим владельцем даёт `dubious ownership`, а `|| true` превращал это в
    «обновлять не на что» — ученик искал теги, которых у него полно.
    """
    stub = _stub_bin(tmp_path, ("uv", "systemctl", "sudo"))
    engine = _engine_with_tags(tmp_path)
    _instances(tmp_path)
    bad_git = stub / "git"
    bad_git.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$3" = "tag" ]; then\n'
        "  echo \"fatal: detected dubious ownership in repository at '/opt/unpacker'\" >&2\n"
        "  exit 128\nfi\n"
        'exec /usr/bin/git "$@"\n'
    )
    bad_git.chmod(0o755)
    r = run_update_in(engine, env_extra=_upd_env(tmp_path, engine), stub=stub)
    out = r.stdout + r.stderr
    assert r.returncode != 0
    assert "нет тегов" not in out, f"это не «нет тегов», это отказ git:\n{out}"
    assert "chown" in out and "root" in out, "нужна готовая команда починки владельца"


def test_update_refuses_when_run_user_resolves_to_root(tmp_path):
    """C6/ADV-06: не знаю юзера ботов — не обновляю, а не «готово» вхолостую.

    Так выглядит запуск от root без /etc/unpacker/engine.conf: RUN_USER=root →
    AGENTS_BASE=/root/agents → ни бэкапов, ни рестартов, и бодрый финал «готово: v1 → v2».
    """
    stub = _stub_bin(tmp_path, ("uv", "systemctl", "sudo"))
    engine = _engine_with_tags(tmp_path)
    _instances(tmp_path)
    env = _upd_env(tmp_path, engine, TG_RUN_USER="root")
    del env["TG_AGENTS_BASE"]
    r = run_update_in(engine, env_extra=env, stub=stub)
    out = r.stdout + r.stderr
    assert r.returncode != 0, out
    assert "engine.conf" in out, "должен назвать файл, которого не хватает"
    assert "TG_RUN_USER" in out, "и путь починки руками"
    assert "готово" not in out


def test_update_takes_paths_from_engine_conf(tmp_path):
    """Р2: единая вселенная путей. Без окружения update.sh обязан прочитать engine.conf.

    Иначе любая точка входа (root вручную, sudo от агента, cron) читает свою вселенную и
    тихо рапортует успех, не тронув ни одного бота.
    """
    stub = _stub_bin(tmp_path, ("uv", "systemctl", "sudo"))
    engine = _engine_with_tags(tmp_path)
    base = _instances(tmp_path)
    etc = tmp_path / "etc" / "unpacker"
    etc.mkdir(parents=True)
    me = os.environ.get("USER", "nobody")
    (etc / "engine.conf").write_text(
        f"TG_RUN_USER={me}\nTG_RUNTIME={engine}\nTG_AGENTS_BASE={base}\n"
        f"TG_BRAINS_BASE={tmp_path}/brains\nTG_UV_BIN={stub}/uv\n"
    )
    env = {
        "STUB_LOG": str(tmp_path / "stub.log"),
        "UNPACKER_ETC": str(etc),
        "UV_PROJECT_ENVIRONMENT": str(tmp_path / "venv"),
    }
    r = run_update_in(engine, env_extra=env, stub=stub)
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert sorted((base / "unpacker" / "state" / "backups").glob("*/state.db")), (
        f"инстансы обязаны находиться по конфигу, а не по окружению вызывающего:\n{out}"
    )


def test_update_fails_closed_when_units_exist_but_no_instances(tmp_path):
    """C6: юниты в системе есть, инстансов в AGENTS_BASE нет → я смотрю не туда.

    «Нечего делать» здесь — ложь: боты остались бы на старом коде без бэкапов, а скрипт
    напечатал бы «готово».
    """
    stub = _stub_bin(tmp_path, ("uv", "sudo"))
    sysctl = stub / "systemctl"
    sysctl.write_text(
        "#!/usr/bin/env bash\n"
        'printf "systemctl %s\\n" "$*" >> "$STUB_LOG"\n'
        'case "$*" in *list-units*)\n'
        '  echo "  agent-tg@sales.service loaded active running бот" ;; esac\n'
        "exit 0\n"
    )
    sysctl.chmod(0o755)
    engine = _engine_with_tags(tmp_path)
    (tmp_path / "agents").mkdir()
    r = run_update_in(engine, env_extra=_upd_env(tmp_path, engine), stub=stub)
    out = r.stdout + r.stderr
    assert r.returncode != 0, out
    assert "agent-tg@" in out and "готово" not in out
    assert (engine / "VERSION").read_text().strip() != "v0.2.0", "код не переключаем вслепую"


def test_update_prints_only_whitelisted_command_forms(tmp_path):
    """M-18: печатаем ровно то, что разрешено sudoers (без `bash` под sudo).

    `sudo bash /opt/unpacker/update.sh` даёт «user is not allowed to execute /bin/bash»:
    в whitelist'е стоит сам скрипт, а не оболочка.
    """
    stub = _stub_bin(tmp_path, ("uv", "systemctl", "sudo"))
    engine = _engine_with_tags(tmp_path)
    _instances(tmp_path)
    r = run_update_in(engine, env_extra=_upd_env(tmp_path, engine), stub=stub)
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "sudo bash" not in out, f"под sudo оболочка запрещена whitelist'ом:\n{out}"
    assert f"sudo {engine}/update.sh --rollback" in out


def test_update_refuses_non_repo_dir(tmp_path):
    stub = _stub_bin(tmp_path, ("uv", "systemctl", "sudo"))
    notarepo = tmp_path / "notarepo"
    notarepo.mkdir()
    upd = notarepo / "update.sh"
    upd.write_text(UPDATE.read_text())
    upd.chmod(0o755)
    r = _run(upd, env_extra=_upd_env(tmp_path, notarepo), stub=stub)
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "install.sh" in out, "если движок не установлен — отправь к install.sh"


# ── шаблон мозга и пример (§4) ───────────────────────────────────────────────

TEMPLATE = REPO / "brains" / "_template"
EXAMPLE = REPO / "examples" / "assistant"


def _parse_buttons(path: Path) -> list[dict[str, str]]:
    """Мини-парсер корневого списка buttons.

    pyyaml в зависимостях движка нет, а тащить его ради теста — лишняя зависимость в
    продукте. Нам нужен ровно контракт среза: корневой ключ buttons, элементы с label и
    prompt (см. `buttons.yaml` инстанса).
    """
    items: list[dict[str, str]] = []
    cur: dict[str, str] | None = None
    in_buttons = False
    for raw in path.read_text().splitlines():
        if not raw.strip() or raw.strip().startswith("#"):
            continue
        if not raw.startswith((" ", "\t", "-")):  # корневой ключ
            in_buttons = raw.split(":", 1)[0].strip() == "buttons"
            cur = None
            continue
        if not in_buttons:
            continue
        item = raw.strip()
        if item.startswith("- "):
            cur = {}
            items.append(cur)
            item = item[2:]
        if cur is not None and ":" in item:
            k, v = item.split(":", 1)
            cur[k.strip()] = v.strip().strip('"').strip("'")
    return items


def test_brain_template_has_commented_claude_md():
    cm = TEMPLATE / "CLAUDE.md"
    assert cm.exists(), "brains/_template/CLAUDE.md — обязательный минимум мозга (§4)"
    text = cm.read_text()
    # шаблон обязан ОБЪЯСНЯТЬ, что писать, а не быть пустым файлом
    assert len(text.splitlines()) > 20
    assert "что тут писать" in text.lower() or "заполни" in text.lower()


def test_brain_template_buttons_follow_cross_slice_contract():
    y = TEMPLATE / ".brain.yaml"
    assert y.exists(), "паспорт мозга с примером кнопок (§4)"
    buttons = _parse_buttons(y)
    assert buttons, "в примере паспорта обязаны быть кнопки — иначе шаблон не показывает формат"
    for b in buttons:
        assert "label" in b and "prompt" in b, f"контракт кнопки — label + prompt, получено: {b}"


def test_brain_template_has_places_for_knowledge_and_skills():
    assert (TEMPLATE / "knowledge").is_dir(), "место под знания агента"
    assert (TEMPLATE / ".claude" / "skills").is_dir(), "место под скиллы агента"


def test_example_brain_works_without_edits():
    """Пример мозга должен разворачиваться как есть: минимум §4 — один CLAUDE.md."""
    cm = EXAMPLE / "CLAUDE.md"
    assert cm.exists(), "examples/assistant/CLAUDE.md"
    text = cm.read_text()
    assert len(text.splitlines()) > 10
    # плейсхолдеров быть не должно — иначе «работает без правок» ложь
    for placeholder in ("<...>", "TODO", "ЗАПОЛНИ", "замени это"):
        assert placeholder not in text, f"в рабочем примере не должно быть '{placeholder}'"
    buttons = _parse_buttons(EXAMPLE / ".brain.yaml")
    assert len(buttons) >= 2
    for b in buttons:
        assert b.get("label") and b.get("prompt")


# ── README: путь новичка (§11) ───────────────────────────────────────────────


def test_readme_covers_all_newbie_path_sections():
    text = (REPO / "README.md").read_text()
    low = text.lower()
    required = {
        "что это": "что это и зачем",
        "стоимость": "стоимость владения (VPS + подписка)",
        "botfather": "пре-флайт: бот в BotFather",
        "topics in private chats": "включение топиков — шаг, который нельзя сделать скриптом",
        "install.sh": "быстрый старт одной командой",
        "папка-мозг": "модель мозга и шаблон",
        "agentctl": "диагностика для продвинутых",
        "journalctl": "диагностика для продвинутых",
        "update.sh": "обновление",
        "--rollback": "откат",
        "152-фз": "персональные данные",
        "фаза 3": "честный статус фаз — веб ещё не сделан",
    }
    missing = [why for marker, why in required.items() if marker not in low]
    assert not missing, f"README не покрывает путь новичка: {missing}"


def test_readme_warns_about_subscription_limit_and_client_ban():
    """Две вещи, которые ученик обязан узнать из README, а не из бана (§8.1)."""
    low = (REPO / "README.md").read_text().lower()
    assert "лимит" in low and "подписк" in low, "SDK жжёт лимит подписки — предупредить"
    assert "клиент" in low, "обслуживать внешних клиентов с подписки нельзя — сказать прямо"


def test_install_dry_run_does_not_claim_bot_is_alive(tmp_path):
    """В dry-run финал не должен утверждать, что бот поднят: ничего же не делали."""
    stub = _base_stub(tmp_path)
    engine = _fake_engine_repo(tmp_path)
    r = run_install(
        "--dry-run",
        "--ram-mb",
        "8192",
        "--non-interactive",
        "--no-hardening",
        "--engine-dir",
        str(engine),
        "--run-user",
        os.environ.get("USER", "nobody"),
        env_extra=_answers(tmp_path, DEPLOY_ARGV=str(tmp_path / "argv.txt")),
        stub=stub,
    )
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "Бот поднят" not in out
    assert "ничего не изменено" in out, "должен честно сказать, что это был только план"
    assert "install.sh" in out, "и подсказать, как запустить по-настоящему"


def test_install_new_env_answers_win_over_saved_ones(tmp_path):
    """Повторный запуск с ДРУГИМ ответом обязан взять новый, а не запомненный.

    Иначе «поменял список пользователей и переустановил» тихо не срабатывает: сохранённый
    .install.conf перезаписывал бы переменные окружения.
    """
    stub = _base_stub(tmp_path)
    engine = _fake_engine_repo(tmp_path)
    argv_log = tmp_path / "argv.txt"
    args = (
        "--ram-mb",
        "8192",
        "--non-interactive",
        "--no-hardening",
        "--engine-dir",
        str(engine),
        "--run-user",
        os.environ.get("USER", "nobody"),
    )
    r1 = run_install(*args, env_extra=_answers(tmp_path, DEPLOY_ARGV=str(argv_log)), stub=stub)
    assert r1.returncode == 0, r1.stdout + r1.stderr
    r2 = run_install(
        *args,
        env_extra=_answers(tmp_path, UNPACKER_ALLOWED_USERS="333", DEPLOY_ARGV=str(argv_log)),
        stub=stub,
    )
    assert r2.returncode == 0, r2.stdout + r2.stderr
    argv = argv_log.read_text().splitlines()
    assert argv[argv.index("--users") + 1] == "333", "новый ответ должен побеждать запомненный"
    conf = (tmp_path / "etc" / "unpacker" / "install.conf").read_text()
    assert "UNPACKER_ALLOWED_USERS=333" in conf


def test_install_survives_distro_where_ssh_unit_is_named_differently(tmp_path):
    """`systemctl reload ssh` падает (юнит зовётся sshd) — установка обязана продолжиться.

    Под `set -e` необработанный отказ убил бы install.sh посреди шага 0, и ученик остался
    бы с полу-настроенным сервером без объяснений.
    """
    stub = _base_stub(tmp_path)
    sysctl = stub / "systemctl"
    sysctl.write_text(
        "#!/usr/bin/env bash\n"
        'printf "systemctl %s\\n" "$*" >> "$STUB_LOG"\n'
        'if [ "$1 $2" = "reload ssh" ]; then echo "Unit ssh.service not found." >&2; exit 5; fi\n'
        "exit 0\n"
    )
    sysctl.chmod(0o755)
    keys = tmp_path / "authorized_keys"
    keys.write_text("ssh-ed25519 AAAA... test@key\n")
    engine = _fake_engine_repo(tmp_path)
    r = run_install(
        "--ram-mb",
        "8192",
        "--non-interactive",
        "--ssh-keys",
        str(keys),
        "--engine-dir",
        str(engine),
        "--run-user",
        os.environ.get("USER", "nobody"),
        env_extra=_answers(tmp_path, DEPLOY_ARGV=str(tmp_path / "argv.txt")),
        stub=stub,
    )
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert (tmp_path / "argv.txt").exists(), "деплой должен состояться, несмотря на имя ssh-юнита"
    log = (tmp_path / "stub.log").read_text()
    assert "reload sshd" in log, "должен попробовать второе имя юнита"


def test_update_dry_run_does_not_claim_it_updated(tmp_path):
    """Финал dry-run не должен рапортовать «готово: X → Y» — ничего не переключали."""
    stub = _stub_bin(tmp_path, ("uv", "systemctl", "sudo"))
    engine = _engine_with_tags(tmp_path)
    _instances(tmp_path)
    r = run_update_in(engine, "--dry-run", env_extra=_upd_env(tmp_path, engine), stub=stub)
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "==> готово" not in out
    assert "ничего не изменено" in out


def test_install_probes_private_repo_without_credential_prompt(tmp_path):
    """Проба доступа к репо не должна уметь спросить логин/пароль.

    На свежем VPS git по HTTPS спрашивает логин прямо в терминале и ждёт вечно — для
    новичка это «установка зависла». GIT_TERMINAL_PROMPT=0 превращает это в честный отказ.
    """
    stub = _stub_bin(tmp_path, ("uv", "tmux", "claude", "sudo", "apt-get"))
    probe_git = stub / "git"
    probe_git.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "ls-remote" ]; then\n'
        '  printf "ls-remote prompt=%s\\n" "${GIT_TERMINAL_PROMPT:-unset}" >> "$STUB_LOG"\n'
        "  exit 128\nfi\nexit 0\n"
    )
    probe_git.chmod(0o755)
    run_install(
        "--ram-mb",
        "8192",
        "--non-interactive",
        "--no-hardening",
        "--engine-dir",
        str(tmp_path / "opt" / "unpacker"),
        "--run-user",
        os.environ.get("USER", "nobody"),
        env_extra=_answers(tmp_path, DEPLOY_ARGV=str(tmp_path / "argv.txt")),
        stub=stub,
    )
    assert "ls-remote prompt=0" in (tmp_path / "stub.log").read_text()
