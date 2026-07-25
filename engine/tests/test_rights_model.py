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


# Р1/SEC-2: боевое дерево движка принадлежит root, а не run-user'у. На Маке/в CI мы root'ом
# не владеем, поэтому «root-owned» имитируем ЧУЖИМ run-user'ом: владелец файлов (тест-юзер)
# ≠ RUN_USER — ровно то отношение, которое проверяет install-sudoers.sh.
FOREIGN_USER = "unpacker"


def _inst_env(tmp_path: Path, rt: Path, run_user: str = FOREIGN_USER) -> dict[str, str]:
    return {
        "TG_RUN_USER": run_user,
        "TG_RUNTIME": str(rt),
        "UNPACKER_SUDOERS_DIR": str(tmp_path / "sudoers.d"),
        "UNPACKER_ENGINE_CONF": str(tmp_path / "no-such-engine.conf"),
        "UNPACKER_DEV_OWNER_OK": "",
    }


def _sudoers_fields(rule: str) -> tuple[str, str, str, str]:
    """Разобрать строку правила sudoers на 4 поля: кто, откуда, от кого, что.

    H4: линт не проверял ПОЛЕ ЮЗЕРА — правило `ALL ALL=(root) NOPASSWD: …` (полный
    passwordless на весь whitelist для ВСЕХ юзеров машины) проходило все тесты.
    """
    who, rest = rule.split(None, 1)
    host, rest = rest.split("=", 1)
    runas = ""
    rest = rest.strip()
    if rest.startswith("("):
        runas, rest = rest[1:].split(")", 1)
    return who.strip(), host.strip(), runas.strip(), rest.strip()


def _user_rules(body: str) -> list[str]:
    """Строки-правила (не Defaults/Cmnd_Alias/комментарии) с продолжениями, склеенными в одну."""
    joined = re.sub(r"\\\s*\n\s*", " ", body)
    out = []
    for ln in joined.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        if ln.startswith(("Defaults", "Cmnd_Alias", "User_Alias", "Runas_Alias", "Host_Alias")):
            continue
        out.append(ln)
    return out


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
    assert FOREIGN_USER in body and str(rt) in body
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
    r = run_installer(env_extra=_inst_env(tmp_path, rt, run_user=ME))
    assert r.returncode != 0
    text = (r.stdout + r.stderr).lower()
    assert "запис" in text or "writable" in text
    assert not (tmp_path / "sudoers.d" / "unpacker").exists()


def test_installer_refuses_when_script_world_writable(tmp_path):
    """Даже чужой (root-owned) скрипт с o+w перезапишет кто угодно, включая агента."""
    rt = _fake_runtime(tmp_path, mode=0o557)
    r = run_installer(env_extra=_inst_env(tmp_path, rt))
    assert r.returncode == 3, r.stdout + r.stderr
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


# ── SEC-2/Р1: whitelist ставится только на дерево, которое агент не перепишет ─


def test_installer_refuses_when_runtime_owned_by_run_user(tmp_path):
    """Владелец скрипта == run-user → ОТКАЗ, а не «продолжаю (dev-режим)».

    SEC-2: право записи владелец возвращает себе одним chmod'ом, значит NOPASSWD на такой
    скрипт = полный root в одну строку. Раньше здесь было предупреждение — и продукт из
    коробки жил именно в этом состоянии (install.sh делал chown -R run-user на движок).
    """
    rt = _fake_runtime(tmp_path)  # владелец = тест-юзер
    r = run_installer(env_extra=_inst_env(tmp_path, rt, run_user=ME))
    assert r.returncode == 3, r.stdout + r.stderr
    text = (r.stdout + r.stderr).lower()
    assert "root" in text and "chown" in text, "отказ должен нести готовую команду починки"
    assert not (tmp_path / "sudoers.d" / "unpacker").exists()


def test_installer_dev_mode_requires_explicit_flag(tmp_path):
    """Тот же расклад + явный --dev-owner-ok → установка проходит и честно названа dev-режимом."""
    rt = _fake_runtime(tmp_path)
    r = run_installer("--dev-owner-ok", env_extra=_inst_env(tmp_path, rt, run_user=ME))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "dev" in (r.stdout + r.stderr).lower()
    assert (tmp_path / "sudoers.d" / "unpacker").exists()


def test_installer_refuses_when_runtime_dir_is_group_writable(tmp_path):
    """Каталог с правом записи = подмена скрипта переименованием, даже если сам файл 0555."""
    rt = _fake_runtime(tmp_path)
    (rt / "deploy").chmod(0o775)
    r = run_installer(env_extra=_inst_env(tmp_path, rt))
    assert r.returncode == 3, r.stdout + r.stderr
    assert "катал" in (r.stdout + r.stderr).lower()
    assert not (tmp_path / "sudoers.d" / "unpacker").exists()


def test_installer_refuses_when_runtime_dir_owned_by_run_user(tmp_path):
    rt = _fake_runtime(tmp_path)
    r = run_installer(env_extra=_inst_env(tmp_path, rt, run_user=ME))
    assert r.returncode == 3, r.stdout + r.stderr


# ── SEC-3: NOPASSWD не выдаётся на несуществующий файл ──────────────────────


def test_installer_refuses_when_update_sh_absent(tmp_path):
    """Перевёрнутый тест: раньше отсутствие update.sh закреплялось как «предупреждение».

    NOPASSWD на путь, которого ещё нет, — это разрешение на файл, который кто-то создаст
    позже. После зоны shell update.sh существует всегда, значит его отсутствие = битое дерево.
    """
    rt = _fake_runtime(tmp_path, with_update=False)
    r = run_installer(env_extra=_inst_env(tmp_path, rt))
    assert r.returncode == 3, r.stdout + r.stderr
    assert "update.sh" in (r.stdout + r.stderr)
    assert not (tmp_path / "sudoers.d" / "unpacker").exists()


# ── H4: правило разбирается на поля, права выданы ИМЕННО run-user'у ──────────


def test_sudoers_field_parser_rejects_all_users_mutant():
    """Мутант-контроль самого парсера: правило «всем юзерам машины» обязано ловиться."""
    who, host, runas, cmds = _sudoers_fields("ALL ALL=(root) NOPASSWD: UNPACKER_DEPLOY")
    assert who == "ALL" and runas == "root"
    assert host == "ALL"
    assert "UNPACKER_DEPLOY" in cmds


def test_sudoers_template_rule_targets_single_user(tmp_path):
    rules = _user_rules(SUDOERS_TPL.read_text())
    assert len(rules) == 1, f"ожидалось одно правило, а не {len(rules)}: {rules}"
    who, host, runas, cmds = _sudoers_fields(rules[0])
    assert who == "REPLACE_WITH_LINUX_USER", (
        f"права выданы '{who}', а должны — только run-user'у Распаковщика (H4)"
    )
    assert who != "ALL" and not who.startswith("%"), "ни ALL, ни группа"
    assert runas == "root", f"runas должен быть root, а не '{runas}'"
    assert "NOPASSWD:" in cmds
    assert "SETENV" not in cmds, "тег SETENV дал бы агенту протаскивать окружение в root-вызов"


def test_installed_sudoers_grants_rights_to_run_user_only(tmp_path):
    """H4: `ME in body` выполнялось случайно — имя юзера входит в путь runtime.

    Проверяем ПОЛЕ, а не подстроку: в установленном файле права выданы ровно run-user'у.
    """
    rt = _fake_runtime(tmp_path)
    r = run_installer(env_extra=_inst_env(tmp_path, rt))
    assert r.returncode == 0, r.stdout + r.stderr
    rules = _user_rules((tmp_path / "sudoers.d" / "unpacker").read_text())
    assert len(rules) == 1
    who, host, runas, _ = _sudoers_fields(rules[0])
    assert who == FOREIGN_USER, f"права выданы '{who}', ожидался run-user '{FOREIGN_USER}'"
    assert host == "ALL" and runas == "root"


# ── SEC-3: Defaults и перечисленные формы вызова ─────────────────────────────


def test_sudoers_defaults_reset_env_and_forbid_setenv():
    body = SUDOERS_TPL.read_text()
    defaults = [ln.strip() for ln in body.splitlines() if ln.strip().startswith("Defaults")]
    assert defaults, "нужен блок Defaults: окружение root-вызова задаём мы, а не агент"
    joined = " ".join(defaults)
    assert "env_reset" in joined, "env_reset: переменные вызывающего в root-вызов не текут"
    assert "!setenv" in joined, "!setenv: агент не должен подставлять переменные окружения"


def test_sudoers_enumerates_call_forms_for_update_and_agentctl():
    """Команда без аргументов в sudoers = «с ЛЮБЫМИ аргументами» (корень SEC-1).

    Для update.sh и agentctl.sh формы вызова перечислены; --engine-dir среди них нет —
    именно им подменяли `_common.sh` и получали root.
    """
    body = re.sub(r"\\\s*\n\s*", " ", SUDOERS_TPL.read_text())
    body = body.replace("REPLACE_WITH_RUNTIME_PATH", "/opt/unpacker")
    for alias, script in (
        ("UNPACKER_UPDATE", "/opt/unpacker/update.sh"),
        ("UNPACKER_AGENTCTL", "/opt/unpacker/deploy/agentctl.sh"),
    ):
        line = next(
            (ln for ln in body.splitlines() if ln.strip().startswith(f"Cmnd_Alias {alias}")), ""
        )
        assert line, f"нет алиаса {alias}"
        forms = [f.strip() for f in line.split("=", 1)[1].split(",") if f.strip()]
        assert forms, f"{alias}: пусто"
        for f in forms:
            assert f.startswith(script), f"{alias}: посторонняя команда {f}"
            args = f[len(script) :].strip()
            assert args, (
                f"{alias}: '{f}' без аргументов = разрешение на ЛЮБЫЕ аргументы (SEC-1/SEC-3)"
            )
        assert "--engine-dir" not in line, "--engine-dir подменяет _common.sh → root (SEC-1)"


# ── H5: гейт visudo — самый дорогой fail-closed, у него должен быть тест ─────

_VISUDO_REJECT = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "${VISUDO_LOG:-/dev/null}"
echo "syntax error near line 1" >&2
exit 1
"""

_VISUDO_LOGGING = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "${VISUDO_LOG:-/dev/null}"
# копируем проверяемый рендер, чтобы тест увидел, ЧТО именно валидировали
for a in "$@"; do [ -f "$a" ] && cp "$a" "${VISUDO_SEEN:?}"; done
exit 0
"""


def test_installer_refuses_when_visudo_rejects_render(tmp_path):
    """Битый /etc/sudoers.d ломает sudo целиком — значит рендер ставим только после visudo."""
    rt = _fake_runtime(tmp_path)
    env = _inst_env(tmp_path, rt)
    log = tmp_path / "visudo.log"
    env["UNPACKER_VISUDO"] = str(_stub(tmp_path, "visudo-reject", _VISUDO_REJECT))
    env["VISUDO_LOG"] = str(log)
    r = run_installer(env_extra=env)
    assert r.returncode == 3, r.stdout + r.stderr
    assert "visudo" in (r.stdout + r.stderr).lower()
    assert not (tmp_path / "sudoers.d" / "unpacker").exists(), "непроверенный файл ставить нельзя"
    assert log.exists() and "-cf" in log.read_text(), "visudo обязан вызываться с -cf"


def test_installer_validates_exactly_the_rendered_file(tmp_path):
    """visudo должен проверять ТОТ рендер, который поедет в /etc/sudoers.d, а не шаблон."""
    rt = _fake_runtime(tmp_path)
    env = _inst_env(tmp_path, rt)
    seen = tmp_path / "seen.sudoers"
    env["UNPACKER_VISUDO"] = str(_stub(tmp_path, "visudo-log", _VISUDO_LOGGING))
    env["VISUDO_LOG"] = str(tmp_path / "visudo2.log")
    env["VISUDO_SEEN"] = str(seen)
    r = run_installer(env_extra=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert seen.exists(), "visudo не получил файла на проверку"
    validated = seen.read_text()
    assert "REPLACE_WITH" not in validated, "проверяли шаблон вместо рендера"
    assert validated == (tmp_path / "sudoers.d" / "unpacker").read_text(), (
        "установлено НЕ то, что проверял visudo"
    )


def _stub(tmp_path: Path, name: str, body: str) -> Path:
    d = tmp_path / "bin"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_text(body, encoding="utf-8")
    p.chmod(0o755)
    return p
