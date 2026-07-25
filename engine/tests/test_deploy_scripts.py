"""TDD для deploy/deploy.sh и deploy/agentctl.sh (Срез E, §6.1 + §7.3).

Скрипты тестируются как чёрный ящик через subprocess. Всё должно проходить НА МАКЕ:
- валидация/гейты/dry-run — без мутаций;
- green-path — реальная провизия инстанса, но БЕЗ systemd (на Маке нет systemctl →
  шаг юнита честно пропускается с предупреждением; сам деплой не падает);
- agentctl — на фикстурах-каталогах, без живого systemd.

Живой systemd/reboot/resume проверяется отдельно смоуком на VPS (не юнит-тестом).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from engine.tests.conftest import (
    ME,
    REPO,
    UV,
    deploy_env,
    make_brain,
    run_agentctl,
    run_deploy,
)

# ── репо-гигиена: шаблоны обязаны ехать ученику вместе с репо ────────────────


def test_env_template_present_and_tracked():
    """`.env.template` НЕ должен попадать под `.env.*` в .gitignore.

    Иначе выдача ученикам ломается молча: репо клонируется без шаблона, deploy.sh
    падает на `sed: .env.template: No such file` уже после провизии инстанса.
    """
    tpl = REPO / "deploy" / "templates" / ".env.template"
    assert tpl.exists(), "шаблон .env инстанса обязан лежать в репо"
    r = subprocess.run(
        ["git", "check-ignore", "-q", str(tpl)], cwd=str(REPO), capture_output=True, text=True
    )
    assert r.returncode != 0, "шаблон .env игнорируется git — ученик его не получит"


def test_deploys_report_dir_exists_but_reports_ignored():
    """Каталог отчётов деплоя (§7.4) есть в репо, а сами отчёты — нет."""
    keep = REPO / "deploys" / ".gitkeep"
    assert keep.exists(), "нужен deploys/.gitkeep — Распаковщику некуда писать отчёт"
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", str(REPO / "deploys" / "2026-07-25-demo.md")],
        cwd=str(REPO),
        capture_output=True,
        text=True,
    )
    assert ignored.returncode == 0, "отчёты деплоя должны быть в .gitignore (пути/имена владельца)"
    tracked = subprocess.run(
        ["git", "check-ignore", "-q", str(keep)], cwd=str(REPO), capture_output=True, text=True
    )
    assert tracked.returncode != 0, ".gitkeep не должен игнорироваться — иначе каталога не будет"


# ── deploy.sh: валидация и гейты ────────────────────────────────────────────


def test_deploy_requires_name():
    r = run_deploy("--surface", "tg", "--token", "123:ABC", "--users", "1", "--brain", "/b")
    assert r.returncode != 0
    assert "name" in (r.stderr + r.stdout).lower()


def test_deploy_rejects_bad_slug():
    r = run_deploy(
        "--surface",
        "tg",
        "--name",
        "Bad Slug",
        "--token",
        "123:ABC",
        "--users",
        "1",
        "--brain",
        "/b",
        "--dry-run",
    )
    assert r.returncode != 0
    assert "slug" in (r.stderr + r.stdout).lower()


def test_deploy_rejects_bad_token_format():
    r = run_deploy(
        "--surface",
        "tg",
        "--name",
        "ok",
        "--token",
        "notatoken",
        "--users",
        "1",
        "--brain",
        "/b",
        "--dry-run",
    )
    assert r.returncode != 0
    assert "token" in (r.stderr + r.stdout).lower()


def test_deploy_refuses_non_tg_surface_in_c1():
    r = run_deploy(
        "--surface",
        "web",
        "--name",
        "ok",
        "--token",
        "123:ABC",
        "--users",
        "1",
        "--brain",
        "/b",
        "--dry-run",
    )
    assert r.returncode != 0
    assert "web" in (r.stderr + r.stdout).lower()


def test_deploy_blocks_root_run_user():
    # bypassPermissions под root запрещён CLI (Makefile canary) → run-user root = блокер
    r = run_deploy(
        "--surface",
        "tg",
        "--name",
        "ok",
        "--token",
        "123:ABC",
        "--users",
        "1",
        "--brain",
        "/b",
        "--dry-run",
        env_extra={"TG_RUN_USER": "root"},
    )
    assert r.returncode != 0
    assert "root" in (r.stderr + r.stdout).lower()


def test_deploy_dry_run_makes_no_instance_dir(tmp_path, api_base):
    agents = tmp_path / "agents"
    brain = tmp_path / "brainsrc"
    brain.mkdir()
    (brain / "CLAUDE.md").write_text("# brain\n")
    r = run_deploy(
        "--surface",
        "tg",
        "--name",
        "drytest",
        "--token",
        "123:ABC",
        "--users",
        "111",
        "--brain",
        str(brain),
        "--dry-run",
        env_extra={
            "TG_RUN_USER": ME,
            "TG_AGENTS_BASE": str(agents),
            "TG_BRAINS_BASE": str(tmp_path / "brains"),
            "TG_RUNTIME": str(REPO),
            "TG_UV_BIN": UV,
            "TG_API_BASE": api_base,
        },
    )
    assert r.returncode == 0, r.stderr
    assert "[dry-run]" in r.stdout
    assert not (agents / "drytest").exists(), "dry-run не должен создавать инстанс-каталог"


# ── deploy.sh: green-path (реальная провизия, systemd пропущен на Маке) ──────


def _green_env(tmp_path, runtime, api_base):
    """Окружение green-path: всё в tmp (фикстуры и переключатели — в conftest)."""
    return deploy_env(tmp_path, api_base, runtime=runtime)


_make_brain = make_brain


@pytest.mark.skipif(not UV, reason="нужен uv для seed-шага")
def test_deploy_green_provisions_instance(tmp_path, isolated_runtime, api_base):
    brain = _make_brain(tmp_path)
    env = _green_env(tmp_path, isolated_runtime, api_base)
    r = run_deploy(
        "--surface",
        "tg",
        "--name",
        "greeny",
        "--token",
        "123456:AAbbCC-dd_ee",
        "--users",
        "111,222",
        "--brain",
        str(brain),
        "--project-slug",
        "greeny",
        env_extra=env,
    )
    assert r.returncode == 0, r.stderr + r.stdout
    inst = Path(env["TG_AGENTS_BASE"]) / "greeny"
    envf = inst / ".env"
    assert envf.exists(), "должен быть создан .env"
    # chmod 600
    assert oct(envf.stat().st_mode)[-3:] == "600"
    body = envf.read_text()
    assert "TELEGRAM_BOT_TOKEN=123456:AAbbCC-dd_ee" in body
    # ALLOWED_USER_IDS — CSV, НЕ JSON (config.py NoDecode парсит CSV)
    assert "ALLOWED_USER_IDS=111,222" in body
    assert "DEFAULT_PROJECT_SLUG=greeny" in body
    # state/ и мозг
    assert (inst / "state").is_dir()
    assert (Path(env["TG_BRAINS_BASE"]) / "greeny" / "CLAUDE.md").exists()
    # сид проекта отработал: state.db несёт строку проекта
    import sqlite3

    db = inst / "state" / "state.db"
    assert db.exists(), "seed должен был создать state.db"
    c = sqlite3.connect(str(db))
    assert c.execute("SELECT count(*) FROM projects WHERE slug='greeny'").fetchone()[0] == 1
    c.close()
    # на Маке нет systemctl → шаг юнита пропущен явно, не тихо
    assert "systemctl" in (r.stdout + r.stderr).lower()


@pytest.mark.skipif(not UV, reason="нужен uv для seed-шага")
def test_deploy_idempotent_does_not_overwrite_env(tmp_path, isolated_runtime, api_base):
    brain = _make_brain(tmp_path)
    env = _green_env(tmp_path, isolated_runtime, api_base)
    args = (
        "--surface",
        "tg",
        "--name",
        "idem",
        "--token",
        "123456:AAbbCC-dd_ee",
        "--users",
        "111",
        "--brain",
        str(brain),
        "--project-slug",
        "idem",
    )
    r1 = run_deploy(*args, env_extra=env)
    assert r1.returncode == 0, r1.stderr + r1.stdout
    envf = Path(env["TG_AGENTS_BASE"]) / "idem" / ".env"
    # пользователь дописал строку — повторный деплой не должен её затереть
    with envf.open("a") as f:
        f.write("CUSTOM_MARKER=keepme\n")
    r2 = run_deploy(*args, env_extra=env)
    assert r2.returncode == 0, r2.stderr + r2.stdout
    assert "CUSTOM_MARKER=keepme" in envf.read_text(), (
        ".env не должен перезатираться (идемпотентно)"
    )
    assert "idempotent" in (r2.stdout + r2.stderr).lower()


@pytest.mark.skipif(not UV, reason="git required")
def test_deploy_stops_on_dirty_git_brain(tmp_path, isolated_runtime, api_base):
    # мозг — git-репо с грязным рабочим деревом → деплой отказывается (§6.1 dirty-мозг=стоп)
    brain = _make_brain(tmp_path)
    subprocess.run(["git", "init", "-q", str(brain)], check=True)
    subprocess.run(["git", "-C", str(brain), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(brain),
            "-c",
            "user.email=a@b.c",
            "-c",
            "user.name=t",
            "commit",
            "-qm",
            "init",
        ],
        check=True,
    )
    (brain / "CLAUDE.md").write_text("# изменено, не закоммичено\n")  # грязь
    env = _green_env(tmp_path, isolated_runtime, api_base)
    r = run_deploy(
        "--surface",
        "tg",
        "--name",
        "dirty",
        "--token",
        "123456:AAbbCC-dd_ee",
        "--users",
        "111",
        "--brain",
        str(brain),
        "--project-slug",
        "dirty",
        env_extra=env,
    )
    assert r.returncode != 0
    assert "dirty" in (r.stdout + r.stderr).lower() or "грязн" in (r.stdout + r.stderr).lower()


# ── agentctl.sh: verdict-логика на фикстурах ────────────────────────────────


def _fixture_instance(tmp_path, name, *, health_status=None, env_perms=0o600, oauth=True):
    base = tmp_path / "agents"
    inst = base / name
    (inst / "state").mkdir(parents=True)
    lines = [
        "TELEGRAM_BOT_TOKEN=123:ABC",
        "ALLOWED_USER_IDS=111",
        "DB_PATH=state/state.db",
        "HEALTH_PATH=state/health.json",
        "DEFAULT_PROJECT_SLUG=" + name,
    ]
    if oauth:
        lines.append("CLAUDE_CODE_OAUTH_TOKEN=sk-test")
    envf = inst / ".env"
    envf.write_text("\n".join(lines) + "\n")
    envf.chmod(env_perms)
    (inst / "state" / "state.db").write_text("")  # заглушка resume-БД
    if health_status is not None:
        import json

        (inst / "state" / "health.json").write_text(
            json.dumps({"status": health_status, "reason": "test", "ts": "2026-07-21T00:00:00Z"})
        )
    return base


def test_agentctl_doctor_not_deployed(tmp_path):
    base = tmp_path / "agents"
    base.mkdir()
    r = run_agentctl(
        "doctor",
        "ghost",
        env_extra={"TG_AGENTS_BASE": str(base), "TG_RUN_USER": ME},
    )
    assert r.returncode != 0
    assert "not deployed" in r.stdout.lower() or "не развёрнут" in r.stdout.lower()


def test_agentctl_health_degraded_marker(tmp_path):
    # health.json со status=degraded → DEGRADED, даже если бы процесс был жив
    base = _fixture_instance(tmp_path, "sick", health_status="degraded")
    r = run_agentctl(
        "health",
        "sick",
        env_extra={"TG_AGENTS_BASE": str(base), "TG_RUN_USER": ME},
    )
    assert r.returncode != 0
    assert "degraded" in r.stdout.lower()


def test_agentctl_status_reports_env_perms(tmp_path):
    base = _fixture_instance(tmp_path, "okperms", health_status="ok")
    r = run_agentctl(
        "status",
        "okperms",
        env_extra={"TG_AGENTS_BASE": str(base), "TG_RUN_USER": ME},
    )
    assert r.returncode == 0, r.stderr
    assert "600" in r.stdout


def test_agentctl_doctor_warns_bad_env_perms(tmp_path):
    base = _fixture_instance(tmp_path, "loose", health_status="ok", env_perms=0o644)
    r = run_agentctl(
        "doctor",
        "loose",
        env_extra={"TG_AGENTS_BASE": str(base), "TG_RUN_USER": ME},
    )
    # doctor должен пожаловаться на права .env (не 600)
    assert "644" in r.stdout or "600" in r.stdout


def test_agentctl_list_shows_instance(tmp_path):
    base = _fixture_instance(tmp_path, "listed", health_status="ok")
    r = run_agentctl("list", env_extra={"TG_AGENTS_BASE": str(base), "TG_RUN_USER": ME})
    assert r.returncode == 0, r.stderr
    assert "listed" in r.stdout


# ── регресс-тесты по ревью (докрутка С1) ────────────────────────────────────


def test_deploy_root_blocked_even_with_skip_preflight(tmp_path):
    # блокер root безусловен — --skip-preflight его НЕ снимает (security review)
    r = run_deploy(
        "--surface",
        "tg",
        "--name",
        "ok",
        "--token",
        "123:ABC",
        "--users",
        "1",
        "--brain",
        "/b",
        "--dry-run",
        "--skip-preflight",
        env_extra={"TG_RUN_USER": "root"},
    )
    assert r.returncode != 0
    assert "root" in (r.stderr + r.stdout).lower()


def test_deploy_rejects_brain_metachars_no_execution(tmp_path):
    # --brain с шелл-метасимволами отвергается ДО исполнения (command injection)
    marker = tmp_path / "pwned"
    r = run_deploy(
        "--surface",
        "tg",
        "--name",
        "inj",
        "--token",
        "123:ABC",
        "--users",
        "1",
        "--brain",
        f"x; touch {marker}; :",
        "--dry-run",
    )
    assert r.returncode != 0
    assert not marker.exists(), "инъекция в --brain не должна исполниться"


@pytest.mark.skipif(not UV, reason="нужен uv")
def test_deploy_brand_with_apostrophe_ok(tmp_path, isolated_runtime, api_base):
    # BRAND уходит в seed через argv → апостроф безопасен и НЕ ломает деплой
    brain = _make_brain(tmp_path)
    env = _green_env(tmp_path, isolated_runtime, api_base)
    r = run_deploy(
        "--surface",
        "tg",
        "--name",
        "brandy",
        "--token",
        "123456:AAbbCC-dd_ee",
        "--users",
        "111",
        "--brain",
        str(brain),
        "--project-slug",
        "brandy",
        "--brand",
        "Никита's бот",
        env_extra=env,
    )
    assert r.returncode == 0, r.stderr + r.stdout
    import sqlite3

    db = Path(env["TG_AGENTS_BASE"]) / "brandy" / "state" / "state.db"
    c = sqlite3.connect(str(db))
    name = c.execute("SELECT name FROM projects WHERE slug='brandy'").fetchone()[0]
    c.close()
    assert name == "Никита's бот", "имя с апострофом должно сохраниться дословно"


@pytest.mark.skipif(not UV, reason="нужен uv")
def test_deploy_scrubs_nested_env_and_symlink(tmp_path, isolated_runtime, api_base):
    brain = _make_brain(tmp_path)
    (brain / "sub").mkdir()
    (brain / "sub" / ".env").write_text("SECRET=leak\n")
    (brain / "outref").symlink_to("/etc/hostname")  # симлинк наружу
    env = _green_env(tmp_path, isolated_runtime, api_base)
    r = run_deploy(
        "--surface",
        "tg",
        "--name",
        "scrub",
        "--token",
        "123456:AAbbCC-dd_ee",
        "--users",
        "111",
        "--brain",
        str(brain),
        "--project-slug",
        "scrub",
        env_extra=env,
    )
    assert r.returncode == 0, r.stderr + r.stdout
    dst = Path(env["TG_BRAINS_BASE"]) / "scrub"
    assert (dst / "CLAUDE.md").exists()
    assert not (dst / "sub" / ".env").exists(), "вложенный .env должен быть вычищен"
    assert not (dst / "outref").exists() and not (dst / "outref").is_symlink(), (
        "симлинк наружу вычищен"
    )


@pytest.mark.skipif(not UV, reason="нужен uv")
def test_deploy_requires_claude_md(tmp_path, isolated_runtime, api_base):
    # мозг без CLAUDE.md (half-clone/битая папка) → СТОП (§7.3)
    brain = tmp_path / "brainsrc"
    brain.mkdir()
    (brain / "notes.txt").write_text("нет CLAUDE.md\n")
    env = _green_env(tmp_path, isolated_runtime, api_base)
    r = run_deploy(
        "--surface",
        "tg",
        "--name",
        "nocm",
        "--token",
        "123456:AAbbCC-dd_ee",
        "--users",
        "111",
        "--brain",
        str(brain),
        "--project-slug",
        "nocm",
        env_extra=env,
    )
    assert r.returncode != 0
    assert "claude.md" in (r.stdout + r.stderr).lower()


@pytest.mark.skipif(not UV, reason="нужен uv")
def test_deploy_cc_token_rotation(tmp_path, isolated_runtime, api_base):
    brain = _make_brain(tmp_path)
    env = _green_env(tmp_path, isolated_runtime, api_base)
    base = (
        "--surface",
        "tg",
        "--name",
        "rot",
        "--token",
        "123456:AAbbCC-dd_ee",
        "--users",
        "111",
        "--brain",
        str(brain),
        "--project-slug",
        "rot",
    )
    r1 = run_deploy(*base, "--cc-token", "sk-OLD", env_extra=env)
    assert r1.returncode == 0, r1.stderr + r1.stdout
    envf = Path(env["TG_AGENTS_BASE"]) / "rot" / ".env"
    assert "CLAUDE_CODE_OAUTH_TOKEN=sk-OLD" in envf.read_text()
    # ротация: новый токен заменяет старый, прочие строки и права 600 целы
    r2 = run_deploy(*base, "--cc-token", "sk-NEW", env_extra=env)
    assert r2.returncode == 0, r2.stderr + r2.stdout
    body = envf.read_text()
    assert "CLAUDE_CODE_OAUTH_TOKEN=sk-NEW" in body
    assert "sk-OLD" not in body, "старый токен должен быть вытеснен"
    assert "TELEGRAM_BOT_TOKEN=123456:AAbbCC-dd_ee" in body, "прочие строки целы"
    assert oct(envf.stat().st_mode)[-3:] == "600"


# ── systemd-юнит: секции/директивы (текст-ассерты, systemd не нужен) ─────────


def _unit_sections():
    txt = (REPO / "deploy" / "templates" / "agent-tg@.service").read_text()
    sections, cur = {}, None
    for line in txt.splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            cur = s
            sections[cur] = []
        elif cur and s and not s.startswith("#"):
            sections[cur].append(s)
    return sections


def test_unit_startlimit_in_unit_section():
    sec = _unit_sections()
    unit = "\n".join(sec.get("[Unit]", []))
    service = "\n".join(sec.get("[Service]", []))
    # StartLimit* читаются systemd ТОЛЬКО из [Unit] (в [Service] игнорятся → анти-крашлуп мёртв)
    assert "StartLimitBurst=" in unit and "StartLimitIntervalSec=" in unit
    assert "StartLimit" not in service


def test_unit_kill_mode_mixed_and_path_and_entrypoint():
    sec = _unit_sections()
    service = "\n".join(sec.get("[Service]", []))
    assert "KillMode=mixed" in service, "иначе subprocess claude сиротеет при restart"
    assert "Environment=PATH=" in service, "иначе SDK не найдёт claude на systemd-PATH"
    assert "python -m engine" in service
    assert "WantedBy=multi-user.target" in "\n".join(sec.get("[Install]", []))
