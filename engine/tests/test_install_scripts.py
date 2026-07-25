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
            "systemctl",
            "useradd",
            "chown",
            "apt-get",
            "git",
            "tee",
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
    assert argv_log.exists(), (
        f"install.sh обязан ЗВАТЬ deploy.sh, а не дублировать провизию:\n{out}"
    )
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
    """
    engine = tmp_path / "opt" / "unpacker"
    (engine / "deploy").mkdir(parents=True)
    (engine / "deploy" / "_common.sh").write_text((REPO / "deploy" / "_common.sh").read_text())
    (engine / "engine").mkdir()
    _git("init", "-q", ".", cwd=str(engine)) if False else subprocess.run(
        ["git", "init", "-q", str(engine)], check=True, capture_output=True
    )
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


def _instances(tmp_path, names=("unpacker", "sales")):
    base = tmp_path / "agents"
    for n in names:
        (base / n / "state").mkdir(parents=True)
        (base / n / ".env").write_text("TELEGRAM_BOT_TOKEN=1:x\n")
        (base / n / "state" / "state.db").write_text("SQLite-заглушка\n")
    return base


def _upd_env(tmp_path, engine, **over):
    env = {
        "STUB_LOG": str(tmp_path / "stub.log"),
        "TG_AGENTS_BASE": str(tmp_path / "agents"),
        "TG_RUN_USER": os.environ.get("USER", "nobody"),
    }
    env.update(over)
    return env


def test_update_script_exists_and_helps():
    assert UPDATE.exists(), "update.sh обязателен: обновление и откат без ssh-магии"
    assert os.access(UPDATE, os.X_OK)
    r = run_update("--help")
    assert r.returncode == 0, r.stderr
    assert "--rollback" in r.stdout and "--dry-run" in r.stdout


def test_update_checks_out_latest_tag_not_head(tmp_path):
    stub = _stub_bin(tmp_path, ("uv", "systemctl", "sudo"))
    engine = _engine_with_tags(tmp_path)
    _instances(tmp_path)
    r = run_update("--engine-dir", str(engine), env_extra=_upd_env(tmp_path, engine), stub=stub)
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert (engine / "VERSION").read_text().strip() == "v0.2.0", (
        "должен встать на последний ТЕГ, а не на HEAD ветки (плохой пуш владельца)"
    )
    assert "v0.2.0" in out


def test_update_backs_up_state_db_before_switching(tmp_path):
    stub = _stub_bin(tmp_path, ("uv", "systemctl", "sudo"))
    engine = _engine_with_tags(tmp_path)
    base = _instances(tmp_path)
    r = run_update("--engine-dir", str(engine), env_extra=_upd_env(tmp_path, engine), stub=stub)
    assert r.returncode == 0, r.stdout + r.stderr
    backups = list((base / "unpacker" / "state" / "backups").glob("state.db*"))
    assert backups, "перед миграциями обязан лежать бэкап БД сессий"
    assert backups[0].read_text() == "SQLite-заглушка\n", "бэкап должен быть копией, а не пустышкой"


def test_update_restarts_initiator_last(tmp_path):
    """Юнит Распаковщика рестартуется последним: иначе он убьёт себя посреди операции."""
    stub = _stub_bin(tmp_path, ("uv", "systemctl", "sudo"))
    engine = _engine_with_tags(tmp_path)
    _instances(tmp_path, names=("alpha", "unpacker", "zeta"))
    r = run_update("--engine-dir", str(engine), env_extra=_upd_env(tmp_path, engine), stub=stub)
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


def test_update_dry_run_changes_nothing(tmp_path):
    stub = _stub_bin(tmp_path, ("uv", "systemctl", "sudo"))
    engine = _engine_with_tags(tmp_path)
    base = _instances(tmp_path)
    before = (engine / "VERSION").read_text()
    r = run_update(
        "--engine-dir", str(engine), "--dry-run", env_extra=_upd_env(tmp_path, engine), stub=stub
    )
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
    r1 = run_update("--engine-dir", str(engine), env_extra=_upd_env(tmp_path, engine), stub=stub)
    assert r1.returncode == 0, r1.stdout + r1.stderr
    r2 = run_update("--engine-dir", str(engine), env_extra=_upd_env(tmp_path, engine), stub=stub)
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
    r1 = run_update("--engine-dir", str(engine), env_extra=_upd_env(tmp_path, engine), stub=stub)
    assert r1.returncode == 0, r1.stdout + r1.stderr
    assert (engine / "VERSION").read_text().strip() == "v0.2.0"
    r2 = run_update(
        "--engine-dir", str(engine), "--rollback", env_extra=_upd_env(tmp_path, engine), stub=stub
    )
    out2 = r2.stdout + r2.stderr
    assert r2.returncode == 0, out2
    assert (engine / "VERSION").read_text().strip() == "v0.1.0", "откат обязан вернуть прошлый тег"
    assert "v0.1.0" in out2


def test_update_rollback_without_history_explains(tmp_path):
    stub = _stub_bin(tmp_path, ("uv", "systemctl", "sudo"))
    engine = _engine_with_tags(tmp_path)
    _instances(tmp_path)
    r = run_update(
        "--engine-dir", str(engine), "--rollback", env_extra=_upd_env(tmp_path, engine), stub=stub
    )
    assert r.returncode != 0
    out = (r.stdout + r.stderr).lower()
    assert "откат" in out or "rollback" in out
    assert "--ref" in out, "должен подсказать, как встать на конкретный релиз руками"


def test_update_refuses_non_repo_dir(tmp_path):
    stub = _stub_bin(tmp_path, ("uv", "systemctl", "sudo"))
    (tmp_path / "notarepo").mkdir()
    r = run_update(
        "--engine-dir",
        str(tmp_path / "notarepo"),
        env_extra=_upd_env(tmp_path, tmp_path / "notarepo"),
        stub=stub,
    )
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
    assert "UNPACKER_ALLOWED_USERS=333" in (engine / ".install.conf").read_text()


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
