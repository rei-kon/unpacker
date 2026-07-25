"""TDD модели прав Распаковщика (§7.5): sudoers-whitelist + hardening юнита.

Ревью безопасности конституции: «агент с полным sudo читает .brain.yaml чужого мозга» =
промпт-инъекция с правами root. Поэтому проверяем не намерение, а ФАЙЛЫ:
- шаблон sudoers не даёт ни ALL, ни wildcard в путях скриптов (иначе можно подсунуть свой);
- install-sudoers.sh идемпотентен, проверяет синтаксис визудо, ставит 0440 и отказывается
  выдавать права на скрипт, который run-user может перезаписать;
- drop-in Распаковщика снимает NoNewPrivileges (иначе sudo не работает), но остальной
  hardening держит — и НЕ ломает обычный agent-tg@.service.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from engine.tests.conftest import (
    ME,
    REPO,
    deploy_args,
    deploy_env,
    make_brain,
    run_deploy,
    run_installer,
)

SUDOERS_TPL = REPO / "deploy" / "sudoers.d" / "unpacker"
INSTALLER = REPO / "deploy" / "install-sudoers.sh"
DROPIN = REPO / "deploy" / "templates" / "unpacker-dropin.conf"
BASE_UNIT = REPO / "deploy" / "templates" / "agent-tg@.service"


def _lines(p: Path) -> list[str]:
    return [
        ln.strip()
        for ln in p.read_text().splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]


def _rules(p: Path) -> str:
    """Только ПРАВИЛА sudoers: комментарии выкинуты, продолжения строк склеены,
    плейсхолдеры отрендерены — иначе проверки путей смотрели бы на текст, а не на то,
    что реально попадёт в /etc/sudoers.d."""
    body = re.sub(r"\\\s*\n\s*", " ", p.read_text())
    body = body.replace("REPLACE_WITH_RUNTIME_PATH", "/opt/unpacker")
    body = body.replace("REPLACE_WITH_LINUX_USER", "agentuser")
    return "\n".join(
        ln for ln in body.splitlines() if ln.strip() and not ln.lstrip().startswith("#")
    )


# ── шаблон sudoers: читаем и проверяем, что лишнего нет ─────────────────────


def test_sudoers_template_exists_with_placeholders():
    assert SUDOERS_TPL.exists(), "нужен шаблон deploy/sudoers.d/unpacker"
    body = SUDOERS_TPL.read_text()
    assert "REPLACE_WITH_LINUX_USER" in body
    assert "REPLACE_WITH_RUNTIME_PATH" in body


def test_sudoers_grants_no_all_command():
    """Ни одной строки вида `NOPASSWD: ALL` и никакого runas (ALL) — только (root)."""
    for logical in _rules(SUDOERS_TPL).splitlines():
        line = logical.strip()
        if not line or line.startswith("#") or "NOPASSWD" not in line:
            continue
        cmds = line.split("NOPASSWD:", 1)[1]
        tokens = [t.strip() for t in cmds.split(",")]
        assert "ALL" not in tokens, f"полный sudo запрещён (§7.5): {line}"
        assert "(ALL" not in line.replace(" ", ""), f"runas должен быть (root), не (ALL): {line}"


def test_sudoers_no_wildcard_in_binary_paths():
    """Wildcard разрешён только в АРГУМЕНТЕ (agent-tg@*), но не в пути до бинаря/скрипта.

    Путь с `*` (например /opt/*/deploy.sh) = дыра: под маску можно положить свой скрипт
    и выполнить его от root.
    """
    for tok in re.findall(r"\S+", _rules(SUDOERS_TPL)):
        if tok.startswith("/"):
            assert "*" not in tok, f"wildcard в пути запрещён: {tok}"
            assert ".." not in tok, f"относительный переход в пути запрещён: {tok}"


def test_sudoers_whitelists_only_known_commands():
    body = _rules(SUDOERS_TPL)
    paths = {t for t in re.findall(r"\S+", body) if t.startswith("/")}
    allowed_tails = ("/deploy/deploy.sh", "/update.sh", "/deploy/agentctl.sh", "/systemctl")
    for p in paths:
        assert p.endswith(allowed_tails), f"в whitelist пролез посторонний путь: {p}"
    # никакой оболочки и никакого редактора — иначе whitelist бессмыслен
    for banned in ("/bin/sh", "/bin/bash", "/usr/bin/env", "sudoedit", "/bin/su"):
        assert banned not in body, f"{banned} в sudoers = полный root в обход whitelist"


def test_sudoers_systemctl_limited_to_agent_units_and_verbs():
    body = _rules(SUDOERS_TPL)
    for m in re.finditer(r"(/(?:usr/)?bin/systemctl)\s+([a-z-]+)\s+(\S+)", body):
        verb, target = m.group(2), m.group(3).rstrip(",")
        assert verb in {"start", "stop", "restart", "status"}, f"глагол {verb} не разрешён"
        assert target.startswith("agent-tg@"), f"юнит {target} вне зоны Распаковщика"
    assert "daemon-reload" not in body, (
        "daemon-reload/enable агенту не выдаём: юниты ставит deploy.sh, уже будучи root"
    )


# ── install-sudoers.sh: идемпотентная установка с проверками ────────────────


def _fake_runtime(tmp_path: Path, *, mode: int = 0o555, with_update: bool = True) -> Path:
    rt = tmp_path / "runtime"
    (rt / "deploy").mkdir(parents=True)
    for rel in ("deploy/deploy.sh", "deploy/agentctl.sh"):
        f = rt / rel
        f.write_text("#!/usr/bin/env bash\n:\n")
        f.chmod(mode)
    if with_update:
        u = rt / "update.sh"
        u.write_text("#!/usr/bin/env bash\n:\n")
        u.chmod(mode)
    return rt


def _inst_env(tmp_path: Path, rt: Path) -> dict[str, str]:
    return {
        "TG_RUN_USER": ME,
        "TG_RUNTIME": str(rt),
        "UNPACKER_SUDOERS_DIR": str(tmp_path / "sudoers.d"),
    }


def test_installer_dry_run_writes_nothing(tmp_path):
    rt = _fake_runtime(tmp_path)
    r = run_installer("--dry-run", env_extra=_inst_env(tmp_path, rt))
    assert r.returncode == 0, r.stderr + r.stdout
    assert "[dry-run]" in r.stdout
    assert not (tmp_path / "sudoers.d" / "unpacker").exists()


def test_installer_renders_and_sets_0440(tmp_path):
    rt = _fake_runtime(tmp_path)
    r = run_installer(env_extra=_inst_env(tmp_path, rt))
    assert r.returncode == 0, r.stderr + r.stdout
    target = tmp_path / "sudoers.d" / "unpacker"
    assert target.exists()
    assert oct(target.stat().st_mode)[-3:] == "440", "sudoers-файл обязан быть 0440"
    body = target.read_text()
    assert "REPLACE_WITH" not in body, "плейсхолдеры должны быть подставлены"
    assert ME in body and str(rt) in body
    assert "visudo" in (r.stdout + r.stderr).lower(), "синтаксис обязан проверяться визудо"


def test_installer_is_idempotent(tmp_path):
    rt = _fake_runtime(tmp_path)
    env = _inst_env(tmp_path, rt)
    assert run_installer(env_extra=env).returncode == 0
    target = tmp_path / "sudoers.d" / "unpacker"
    before = target.stat().st_mtime_ns
    r2 = run_installer(env_extra=env)
    assert r2.returncode == 0, r2.stderr + r2.stdout
    assert "актуален" in (r2.stdout + r2.stderr).lower()
    assert target.stat().st_mtime_ns == before, "повторный запуск не должен перезаписывать файл"


def test_installer_refuses_when_script_writable_by_run_user(tmp_path):
    """Права на скрипт, который агент может перезаписать = root-дыра, а не whitelist."""
    rt = _fake_runtime(tmp_path, mode=0o755)
    r = run_installer(env_extra=_inst_env(tmp_path, rt))
    assert r.returncode != 0
    text = (r.stdout + r.stderr).lower()
    assert "запис" in text or "writable" in text
    assert not (tmp_path / "sudoers.d" / "unpacker").exists()


def test_installer_refuses_when_deploy_script_missing(tmp_path):
    rt = tmp_path / "runtime"
    (rt / "deploy").mkdir(parents=True)
    r = run_installer(env_extra=_inst_env(tmp_path, rt))
    assert r.returncode != 0
    assert "deploy.sh" in (r.stdout + r.stderr)


def test_installer_refuses_root_as_run_user(tmp_path):
    rt = _fake_runtime(tmp_path)
    env = _inst_env(tmp_path, rt)
    env["TG_RUN_USER"] = "root"
    r = run_installer(env_extra=env)
    assert r.returncode != 0
    assert "root" in (r.stdout + r.stderr).lower()


def test_installer_refuses_without_visudo(tmp_path):
    """Нет визудо → не ставим непроверенный файл: битый sudoers ломает sudo целиком."""
    rt = _fake_runtime(tmp_path)
    env = _inst_env(tmp_path, rt)
    env["UNPACKER_VISUDO"] = str(tmp_path / "no-such-visudo")
    r = run_installer(env_extra=env)
    assert r.returncode != 0
    assert "visudo" in (r.stdout + r.stderr).lower()
    assert not (tmp_path / "sudoers.d" / "unpacker").exists()


def test_installer_warns_when_update_sh_absent(tmp_path):
    """update.sh появится в С4 — это предупреждение, а не блокер установки прав."""
    rt = _fake_runtime(tmp_path, with_update=False)
    r = run_installer(env_extra=_inst_env(tmp_path, rt))
    assert r.returncode == 0, r.stderr + r.stdout
    assert "update.sh" in (r.stdout + r.stderr)


# ── systemd: drop-in Распаковщика vs обычный агент ──────────────────────────


def _unit_kv(p: Path) -> str:
    return "\n".join(_lines(p))


def test_dropin_allows_sudo_but_keeps_hardening():
    assert DROPIN.exists(), "нужен drop-in для юнита Распаковщика"
    body = _unit_kv(DROPIN)
    assert "[Service]" in body
    # sudo не работает под NoNewPrivileges — снимаем ИМЕННО его, остальное держим
    assert "NoNewPrivileges=no" in body
    assert "NoNewPrivileges=true" not in body and "NoNewPrivileges=yes" not in body
    assert "ProtectSystem=" in body
    assert "PrivateTmp=true" in body
    assert "CapabilityBoundingSet=" in body
    # /etc read-only при ProtectSystem=full → без исключений deploy.sh не поставит юнит
    assert "ReadWritePaths=" in body
    assert "/etc/systemd/system" in body and "/etc/sudoers.d" in body


def test_dropin_capability_set_is_narrow():
    caps = ""
    for ln in _lines(DROPIN):
        if ln.startswith("CapabilityBoundingSet="):
            caps = ln.split("=", 1)[1]
    assert caps, "CapabilityBoundingSet обязан быть задан явно"
    for banned in ("CAP_SYS_ADMIN", "CAP_SYS_MODULE", "CAP_SYS_PTRACE", "CAP_NET_RAW", "~"):
        assert banned not in caps, f"{banned} в CapabilityBoundingSet — это уже не «ограниченный»"
    assert "CAP_SETUID" in caps and "CAP_SETGID" in caps, "без них sudo не сможет стать root"


def test_base_agent_unit_stays_locked_down():
    """Послабление для Распаковщика НЕ должно течь на обычных агентов (§7.5)."""
    body = BASE_UNIT.read_text()
    assert "NoNewPrivileges=true" in body
    assert "NoNewPrivileges=no" not in body


# ── deploy.sh --role unpacker: шаги прав видны в плане ──────────────────────


@pytest.fixture()
def brain(tmp_path: Path) -> Path:
    return make_brain(tmp_path)


_deploy_env = deploy_env


def _deploy_args(brain: Path, name: str, *rest: str) -> list[str]:
    return deploy_args(brain.parent, name, "--dry-run", *rest, brain=brain)


def test_deploy_role_unpacker_plans_rights_steps(tmp_path, brain, api_base):
    r = run_deploy(
        *_deploy_args(brain, "unpacker", "--role", "unpacker"),
        env_extra=_deploy_env(tmp_path, api_base),
    )
    assert r.returncode == 0, r.stderr + r.stdout
    out = r.stdout.lower()
    assert "sudoers" in out, "план должен показать шаг прав (§7.5)"
    assert "drop-in" in out or "dropin" in out
    assert not (tmp_path / "sudoers.d" / "unpacker").exists(), "dry-run ничего не ставит"


def test_deploy_role_agent_gets_no_rights_steps(tmp_path, brain, api_base):
    """Обычному агенту sudo не нужен — и он его не получает (§7.5)."""
    r = run_deploy(*_deploy_args(brain, "plain"), env_extra=_deploy_env(tmp_path, api_base))
    assert r.returncode == 0, r.stderr + r.stdout
    assert "sudoers" not in r.stdout.lower()
