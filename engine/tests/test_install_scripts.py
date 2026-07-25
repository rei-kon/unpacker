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
import subprocess
from pathlib import Path

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
        body = {"sudo": _STUB_SUDO, "claude": _STUB_CLAUDE}.get(n, _STUB_GENERIC)
        p = d / n
        p.write_text(body)
        p.chmod(0o755)
    return d


def _run(script: Path, *args: str, env_extra: dict[str, str] | None = None, stub: Path | None = None):
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


def test_install_prints_pool_ceiling_from_ram(tmp_path):
    # 8ГБ → (8 − 1.5)/1 = 6 живых агентов; формула та же, что в engine/core/pool.py
    stub = _stub_bin(tmp_path, ("uv", "tmux", "claude", "gh", "sudo", "git"))
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
    assert "6" in out, f"потолок пула из 8ГБ = 6; вывод:\n{out}"


def test_install_ram_ceiling_matches_engine_formula():
    # единственный источник правды по формуле — engine/core/pool.py; install.sh обязан
    # называть ученику то же число, иначе обещание «столько агентов» лживо
    from engine.core.pool import compute_pool_ceiling

    assert compute_pool_ceiling(8 * 1024**3) == 6
    assert compute_pool_ceiling(4 * 1024**3) == 2
    assert compute_pool_ceiling(2 * 1024**3) == 1  # ниже гейта install.sh (он такой VPS отвергнет)


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
    assert "3.11" in out and "uv" in out, f"должен объяснить, почему старый python не блокер:\n{out}"
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
            "uv", "tmux", "claude", "gh", "sudo", "ufw", "systemctl",
            "useradd", "chown", "apt-get", "git", "tee",
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
    }
    env.update(over)
    return env


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
    for mutating in ("ufw allow", "ufw default", "ufw --force", "useradd", "systemctl", "apt-get install"):
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
    r = run_install(
        "--dry-run", "--ram-mb", "8192", "--non-interactive", env_extra=env, stub=stub
    )
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


def test_install_interactive_asks_four_answers_and_does_not_echo_token(tmp_path):
    """Интерактивный путь: 4 ответа со stdin, токен не появляется в выводе."""
    stub = _base_stub(tmp_path)
    engine = _fake_engine_repo(tmp_path)
    secret = "999999:SUPERSECRETTOKEN"
    env = _answers(tmp_path)
    for k in ("UNPACKER_BOT_TOKEN", "UNPACKER_ALLOWED_USERS", "UNPACKER_AUTH_MODE"):
        env.pop(k, None)
    env["DEPLOY_ARGV"] = str(tmp_path / "argv.txt")
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
        input=f"{secret}\n111,222\n{tmp_path / 'brains'}\n1\n",
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{stub}:{os.environ['PATH']}", **env},
        timeout=180,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert secret not in out, "токен не должен появляться в выводе установщика"
    argv = (tmp_path / "argv.txt").read_text().splitlines()
    assert secret in argv, "токен обязан дойти до deploy.sh"


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
    assert argv_log.exists(), f"install.sh обязан ЗВАТЬ deploy.sh, а не дублировать провизию:\n{out}"
    argv = argv_log.read_text().splitlines()
    # детерминированный bootstrap ровно по §10.5
    assert "--surface" in argv and argv[argv.index("--surface") + 1] == "tg"
    assert "--name" in argv and argv[argv.index("--name") + 1] == "unpacker"
    assert argv[argv.index("--brain") + 1] == str(engine / "brains" / "unpacker")
    assert argv[argv.index("--users") + 1] == "111,222"
    assert argv[argv.index("--token") + 1] == "123456:AAbbCC-dd_ee"
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
    conf = engine / ".install.conf"
    assert conf.exists(), "не-секретные ответы запоминаются для идемпотентного повтора"
    assert oct(conf.stat().st_mode)[-3:] == "600"
    assert secret not in conf.read_text(), "секрет живёт только в .env инстанса (600)"


def test_install_second_run_reuses_answers_and_existing_token(tmp_path):
    """Повторный запуск = обновление: ответы из .install.conf, токен — из .env инстанса."""
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
    }
    r2 = run_install(*args, env_extra=env2, stub=stub)
    out2 = r2.stdout + r2.stderr
    assert r2.returncode == 0, out2
    argv = argv_log.read_text().splitlines()
    assert argv[argv.index("--users") + 1] == "111,222", "ответы должны переиспользоваться"
    assert argv[argv.index("--token") + 1] == "123456:AAbbCC-dd_ee", (
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
