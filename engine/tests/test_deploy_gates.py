"""TDD гейтов §7.3 — «уговорить словами нельзя, потому что гейт в bash».

Философия среза С3 (§7.5): LLM предлагает — код исполняет. Распаковщик-мозг может
сколько угодно «решить», что токен нормальный и клиенту можно на подписке, — deploy.sh
всё равно откажет. Поэтому каждый гейт здесь проверяется как чёрный ящик через
subprocess, а не пересказом в скилле.

Гейты сети (getMe) гоняются через локальную заглушку Bot API (`TG_API_BASE`) —
честный curl-путь без интернета и без живого токена.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from engine.tests._tgapi_stub import BAD_TOKEN, DEAD_BASE
from engine.tests._tgapi_stub import api_base as api_base  # noqa: PLC0414 — фикстура для pytest

# Green-path нужен настоящий RUNTIME (шаг seed делает `uv run`). Переиспользуем session-фикстуру
# из test_deploy_scripts.py: pytest кеширует её один раз на сессию, второй копии движка и
# второго `uv sync` не будет.
from engine.tests.test_deploy_scripts import isolated_runtime as isolated_runtime  # noqa: PLC0414

REPO = Path(__file__).resolve().parents[2]
DEPLOY = REPO / "deploy" / "deploy.sh"
GOOD_TOKEN = "123456:AAbbCC-dd_ee"


def run_deploy(*args: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ}
    if env_extra:
        env.update(env_extra)
    return subprocess.run(["bash", str(DEPLOY), *args], capture_output=True, text=True, env=env)


def _brain(tmp_path: Path) -> Path:
    b = tmp_path / "brainsrc"
    b.mkdir(exist_ok=True)  # хелпер зовут дважды за тест (сам мозг + аргументы деплоя)
    (b / "CLAUDE.md").write_text("# Тест-мозг\n")
    return b


def _env(tmp_path: Path, api: str, **extra: str) -> dict[str, str]:
    e = {
        "TG_RUN_USER": os.environ.get("USER", ""),
        "TG_AGENTS_BASE": str(tmp_path / "agents"),
        "TG_BRAINS_BASE": str(tmp_path / "brains"),
        "TG_RUNTIME": str(REPO),
        "TG_API_BASE": api,
    }
    e.update(extra)
    return e


def _args(tmp_path: Path, name: str = "gated", token: str = GOOD_TOKEN, *rest: str) -> list[str]:
    return [
        "--surface",
        "tg",
        "--name",
        name,
        "--token",
        token,
        "--users",
        "111",
        "--brain",
        str(_brain(tmp_path)),
        "--dry-run",
        *rest,
    ]


def _out(r: subprocess.CompletedProcess) -> str:
    return (r.stdout + r.stderr).lower()


# ── гейт 1: токен валиден (getMe), в выводе — [REDACTED] ─────────────────────


def test_gate_getme_valid_token_passes_and_never_prints_token(tmp_path, api_base):
    r = run_deploy(*_args(tmp_path), env_extra=_env(tmp_path, api_base))
    assert r.returncode == 0, r.stderr + r.stdout
    assert "stub_bot" in r.stdout, "гейт должен показать, ЧЕЙ это бот (getMe отработал)"
    assert GOOD_TOKEN not in (r.stdout + r.stderr), "токен не должен светиться в выводе"
    assert "[REDACTED]" in r.stdout


def test_gate_getme_rejects_bad_token(tmp_path, api_base):
    r = run_deploy(*_args(tmp_path, "badtok", BAD_TOKEN), env_extra=_env(tmp_path, api_base))
    assert r.returncode == 3, r.stdout + r.stderr
    assert "getme" in _out(r) or "токен" in _out(r)
    assert BAD_TOKEN not in (r.stdout + r.stderr)
    assert not (tmp_path / "agents" / "badtok").exists()


def test_gate_getme_rejects_bad_token_even_with_skip_preflight(tmp_path, api_base):
    """--skip-preflight не снимает гейты §7.3 — иначе «уговорить» = передать флаг."""
    r = run_deploy(
        *_args(tmp_path, "badtok2", BAD_TOKEN, "--skip-preflight"),
        env_extra=_env(tmp_path, api_base),
    )
    assert r.returncode == 3, r.stdout + r.stderr
    assert "getme" in _out(r) or "токен" in _out(r)


def test_gate_getme_network_down_refuses_loudly(tmp_path):
    """Сеть недоступна → внятный отказ, а НЕ тихий проход мимо гейта."""
    r = run_deploy(*_args(tmp_path, "netdown"), env_extra=_env(tmp_path, DEAD_BASE))
    assert r.returncode == 3, r.stdout + r.stderr
    text = _out(r)
    assert "сет" in text or "недоступ" in text, "должно быть сказано, что проверка не удалась"
    assert not (tmp_path / "agents" / "netdown").exists()


# ── гейт 2: токен не занят другим живым инстансом (двойной polling = 409) ────


def test_gate_token_busy_by_other_instance(tmp_path, api_base):
    other = tmp_path / "agents" / "twin"
    other.mkdir(parents=True)
    (other / ".env").write_text(f"TELEGRAM_BOT_TOKEN={GOOD_TOKEN}\nALLOWED_USER_IDS=1\n")
    r = run_deploy(*_args(tmp_path, "second"), env_extra=_env(tmp_path, api_base))
    assert r.returncode == 3, r.stdout + r.stderr
    assert "twin" in _out(r), "надо назвать инстанс, который уже держит этот токен"
    assert "409" in _out(r) or "занят" in _out(r)


def test_gate_token_busy_allows_redeploy_of_same_instance(tmp_path, api_base):
    inst = tmp_path / "agents" / "same"
    inst.mkdir(parents=True)
    (inst / ".env").write_text(f"TELEGRAM_BOT_TOKEN={GOOD_TOKEN}\nALLOWED_USER_IDS=1\n")
    r = run_deploy(*_args(tmp_path, "same"), env_extra=_env(tmp_path, api_base))
    assert r.returncode == 0, r.stderr + r.stdout


# ── гейт 3: уникальность имени инстанса (имя ↔ бот 1:1) ─────────────────────


def test_gate_name_taken_by_another_bot(tmp_path, api_base):
    inst = tmp_path / "agents" / "gated"
    inst.mkdir(parents=True)
    (inst / ".env").write_text("TELEGRAM_BOT_TOKEN=555555:OTHERbot\nALLOWED_USER_IDS=1\n")
    r = run_deploy(*_args(tmp_path, "gated"), env_extra=_env(tmp_path, api_base))
    assert r.returncode == 3, r.stdout + r.stderr
    assert "имя" in _out(r) and "gated" in _out(r)
    # чужой токен из .env тоже не должен утечь в вывод
    assert "555555:OTHERbot" not in (r.stdout + r.stderr)


# ── гейт 4: мозг существует и в нём CLAUDE.md — ДО любых мутаций ─────────────


def test_gate_missing_brain_path_refuses_early(tmp_path, api_base):
    r = run_deploy(
        "--surface",
        "tg",
        "--name",
        "nobrain",
        "--token",
        GOOD_TOKEN,
        "--users",
        "111",
        "--brain",
        str(tmp_path / "нет-такой-папки"),
        "--dry-run",
        env_extra=_env(tmp_path, api_base),
    )
    assert r.returncode == 3, r.stdout + r.stderr
    assert "мозг" in _out(r)


def test_gate_brain_without_claude_md_refuses_in_dry_run(tmp_path, api_base):
    b = tmp_path / "empty-brain"
    b.mkdir()
    (b / "notes.txt").write_text("нет личности\n")
    r = run_deploy(
        "--surface",
        "tg",
        "--name",
        "nocm",
        "--token",
        GOOD_TOKEN,
        "--users",
        "111",
        "--brain",
        str(b),
        "--dry-run",
        env_extra=_env(tmp_path, api_base),
    )
    assert r.returncode == 3, r.stdout + r.stderr
    assert "claude.md" in _out(r)


def test_gate_dirty_brain_refuses_in_dry_run(tmp_path, api_base):
    """Грязный git-мозг ловится и в dry-run: план не должен обещать невозможное."""
    b = _brain(tmp_path)
    subprocess.run(["git", "init", "-q", str(b)], check=True)
    subprocess.run(["git", "-C", str(b), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(b),
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
    (b / "CLAUDE.md").write_text("# правка без коммита\n")
    r = run_deploy(
        "--surface",
        "tg",
        "--name",
        "dirtydry",
        "--token",
        GOOD_TOKEN,
        "--users",
        "111",
        "--brain",
        str(b),
        "--dry-run",
        env_extra=_env(tmp_path, api_base),
    )
    assert r.returncode == 3, r.stdout + r.stderr
    assert "грязн" in _out(r) or "dirty" in _out(r)


# ── гейт 5: контур auth (§8.1) — ToS-гейт клиентской поверхности ─────────────


def test_gate_client_audience_on_subscription_refused_by_code(tmp_path, api_base):
    """Клиентская поверхность на подписке = нарушение ToS → отказ КОДОМ, не советом."""
    r = run_deploy(
        *_args(tmp_path, "clientbot", GOOD_TOKEN, "--audience", "client"),
        env_extra=_env(tmp_path, api_base),
    )
    assert r.returncode == 3, r.stdout + r.stderr
    text = _out(r)
    assert "tos" in text or "подписк" in text
    assert "--auth-mode api" in (r.stdout + r.stderr), "отказ должен объяснять, что делать"


def test_gate_client_audience_refused_even_with_api_mode_phase4(tmp_path, api_base):
    """С api-контуром клиентская поверхность всё равно закрыта: ToolPolicy — Фаза 4."""
    r = run_deploy(
        *_args(
            tmp_path,
            "clientapi",
            GOOD_TOKEN,
            "--audience",
            "client",
            "--auth-mode",
            "api",
            "--api-key",
            "sk-ant-fake",
            "--limit-requests-per-day",
            "100",
            "--limit-tokens-per-day",
            "100000",
        ),
        env_extra=_env(tmp_path, api_base),
    )
    assert r.returncode == 3, r.stdout + r.stderr
    assert "фаза 4" in _out(r) or "toolpolicy" in _out(r)


def test_gate_api_mode_requires_limits(tmp_path, api_base):
    r = run_deploy(
        *_args(tmp_path, "apinolim", GOOD_TOKEN, "--auth-mode", "api", "--api-key", "sk-ant-fake"),
        env_extra=_env(tmp_path, api_base),
    )
    assert r.returncode == 3, r.stdout + r.stderr
    assert "лимит" in _out(r)


def test_gate_api_mode_requires_api_key(tmp_path, api_base):
    r = run_deploy(
        *_args(
            tmp_path,
            "apinokey",
            GOOD_TOKEN,
            "--auth-mode",
            "api",
            "--limit-requests-per-day",
            "100",
            "--limit-tokens-per-day",
            "100000",
        ),
        env_extra=_env(tmp_path, api_base),
    )
    assert r.returncode == 3, r.stdout + r.stderr
    assert "api-key" in _out(r)


def test_gate_mixed_contours_refused(tmp_path, api_base):
    """Один инстанс — один контур (§8.1): api-ключ и oauth-токен вместе = отказ."""
    r = run_deploy(
        *_args(
            tmp_path,
            "mixed",
            GOOD_TOKEN,
            "--auth-mode",
            "api",
            "--api-key",
            "sk-ant-fake",
            "--cc-token",
            "sk-oauth-fake",
            "--limit-requests-per-day",
            "100",
            "--limit-tokens-per-day",
            "100000",
        ),
        env_extra=_env(tmp_path, api_base),
    )
    assert r.returncode == 3, r.stdout + r.stderr
    assert "контур" in _out(r)


def test_gate_rejects_unknown_auth_mode(tmp_path, api_base):
    r = run_deploy(
        *_args(tmp_path, "badmode", GOOD_TOKEN, "--auth-mode", "freebeer"),
        env_extra=_env(tmp_path, api_base),
    )
    assert r.returncode == 2, r.stdout + r.stderr
    assert "auth-mode" in _out(r)


def test_gate_rejects_non_numeric_limits(tmp_path, api_base):
    r = run_deploy(
        *_args(
            tmp_path,
            "badlim",
            GOOD_TOKEN,
            "--auth-mode",
            "api",
            "--api-key",
            "sk-ant-fake",
            "--limit-requests-per-day",
            "много",
            "--limit-tokens-per-day",
            "100000",
        ),
        env_extra=_env(tmp_path, api_base),
    )
    assert r.returncode == 2, r.stdout + r.stderr
    assert "лимит" in _out(r)


# ── инъекции: safe_arg держит даже в новых флагах ────────────────────────────


@pytest.mark.parametrize(
    "evil",
    ["x; touch {m}; :", "x$(touch {m})", "x`touch {m}`", "x|touch {m}", "x&&touch {m}"],
)
def test_gate_injection_in_brain_never_executes(tmp_path, evil, api_base):
    marker = tmp_path / "pwned"
    r = run_deploy(
        "--surface",
        "tg",
        "--name",
        "inj",
        "--token",
        GOOD_TOKEN,
        "--users",
        "111",
        "--brain",
        evil.format(m=marker),
        "--dry-run",
        env_extra=_env(tmp_path, api_base),
    )
    assert r.returncode != 0
    assert not marker.exists(), "инъекция в --brain не должна исполниться"


def test_gate_injection_in_api_base_rejected(tmp_path):
    marker = tmp_path / "pwned-api"
    r = run_deploy(
        *_args(tmp_path, "injapi"),
        env_extra=_env(tmp_path, f"http://x$(touch {marker})"),
    )
    assert r.returncode == 2, r.stdout + r.stderr
    assert not marker.exists()


# ── гейты печатаются человеку: Распаковщик показывает то, что проверил КОД ───


def test_gates_print_summary_block(tmp_path, api_base):
    r = run_deploy(*_args(tmp_path), env_extra=_env(tmp_path, api_base))
    assert r.returncode == 0, r.stderr + r.stdout
    assert "гейты" in r.stdout.lower(), "нужен явный блок результатов гейтов для отчёта в чат"


# ── .env инстанса: контур auth записан, секреты — с правами 600 ──────────────


def _has_key(env_body: str, key: str) -> bool:
    """Есть ли ЖИВОЕ присваивание ключа (закомментированный пример в шаблоне — не считается)."""
    return any(line.startswith(f"{key}=") for line in env_body.splitlines())


def _green(tmp_path: Path, api: str, runtime: str) -> dict[str, str]:
    e = _env(tmp_path, api)
    e["TG_RUNTIME"] = runtime
    return e


def _real_args(tmp_path: Path, name: str, *rest: str, brain: Path | None = None) -> list[str]:
    return [
        "--surface",
        "tg",
        "--name",
        name,
        "--token",
        GOOD_TOKEN,
        "--users",
        "111",
        "--brain",
        str(brain if brain is not None else _brain(tmp_path)),
        "--project-slug",
        name,
        *rest,
    ]


def test_env_records_subscription_contour(tmp_path, api_base, isolated_runtime):
    env = _green(tmp_path, api_base, isolated_runtime)
    r = run_deploy(*_real_args(tmp_path, "subctr", "--cc-token", "sk-oauth-fake"), env_extra=env)
    assert r.returncode == 0, r.stderr + r.stdout
    body = (Path(env["TG_AGENTS_BASE"]) / "subctr" / ".env").read_text()
    assert "AUTH_MODE=subscription" in body
    assert not _has_key(body, "ANTHROPIC_API_KEY"), "в контуре подписки api-ключа быть не должно"
    assert "CLAUDE_CODE_OAUTH_TOKEN=sk-oauth-fake" in body


def test_env_api_contour_writes_key_and_limits(tmp_path, api_base, isolated_runtime):
    env = _green(tmp_path, api_base, isolated_runtime)
    r = run_deploy(
        *_real_args(
            tmp_path,
            "apictr",
            "--auth-mode",
            "api",
            "--api-key",
            "sk-ant-fake-key",
            "--limit-requests-per-day",
            "300",
            "--limit-tokens-per-day",
            "500000",
        ),
        env_extra=env,
    )
    assert r.returncode == 0, r.stderr + r.stdout
    envf = Path(env["TG_AGENTS_BASE"]) / "apictr" / ".env"
    body = envf.read_text()
    assert "AUTH_MODE=api" in body
    assert "ANTHROPIC_API_KEY=sk-ant-fake-key" in body
    assert "LIMIT_REQUESTS_PER_DAY=300" in body
    assert "LIMIT_TOKENS_PER_DAY=500000" in body
    assert not _has_key(body, "CLAUDE_CODE_OAUTH_TOKEN"), "контуры не смешиваем"
    assert oct(envf.stat().st_mode)[-3:] == "600"
    assert "sk-ant-fake-key" not in (r.stdout + r.stderr), "api-ключ не светится в выводе"


# ── мост «мозг → инстанс»: buttons.yaml (контракт со срезом С2) ──────────────


def _brain_with_buttons(tmp_path: Path, label: str = "Создать КП") -> Path:
    """Мозг с паспортом: проверяем сквозной контракт «кнопка из .brain.yaml доехала в инстанс»."""
    b = _brain(tmp_path)
    (b / ".brain.yaml").write_text(
        f'name: "Тест-мозг"\nslug: testbrain\nbuttons:\n  - label: "{label}"\n'
        '    prompt: "Собери КП по данным клиента"\n',
        encoding="utf-8",
    )
    return b


def test_buttons_bridge_warns_when_brainkit_absent(tmp_path, api_base, isolated_runtime):
    """Модуля моста нет в движке → предупреждение и продолжение, а не падение деплоя.

    Сценарий не гипотетический: у ученика на VPS движок обновляется git-тегами, и деплой не
    имеет права падать целиком из-за отсутствующей необязательной части. Модуль прячем
    переименованием (runtime у тестов общий на сессию — удалять его насовсем нельзя).
    """
    real = Path(isolated_runtime) / "engine" / "brainkit.py"
    hidden = real.with_suffix(".py.hidden")
    real.rename(hidden)
    try:
        env = _green(tmp_path, api_base, isolated_runtime)
        r = run_deploy(*_real_args(tmp_path, "nobuttons"), env_extra=env)
        assert r.returncode == 0, r.stderr + r.stdout
        out = _out(r)
        assert "buttons" in out and ("brainkit" in out or "кнопк" in out)
        assert not (Path(env["TG_AGENTS_BASE"]) / "nobuttons" / "buttons.yaml").exists()
    finally:
        hidden.rename(real)


def test_buttons_bridge_exports_when_brainkit_present(tmp_path, api_base, isolated_runtime):
    """Стык С2×С3 вживую: deploy.sh дергает настоящий CLI, кнопка мозга доезжает в инстанс."""
    env = _green(tmp_path, api_base, isolated_runtime)
    brain = _brain_with_buttons(tmp_path)
    r = run_deploy(*_real_args(tmp_path, "withbtn", brain=brain), env_extra=env)
    assert r.returncode == 0, r.stderr + r.stdout
    btn = Path(env["TG_AGENTS_BASE"]) / "withbtn" / "buttons.yaml"
    assert btn.exists(), "мост мозг→инстанс должен создать buttons.yaml"
    body = btn.read_text()
    assert "buttons:" in body
    assert "Создать КП" in body, "label из паспорта мозга должен доехать до инстанса"


def test_buttons_bridge_does_not_overwrite_existing(tmp_path, api_base, isolated_runtime):
    """buttons.yaml инстанса правит владелец — синк мозга его НЕ перезатирает (§4)."""
    env = _green(tmp_path, api_base, isolated_runtime)
    brain = _brain_with_buttons(tmp_path)
    inst = Path(env["TG_AGENTS_BASE"]) / "keepbtn"
    inst.mkdir(parents=True)
    (inst / "buttons.yaml").write_text("buttons:\n  - label: Моя\n    prompt: моя\n")
    r = run_deploy(*_real_args(tmp_path, "keepbtn", brain=brain), env_extra=env)
    assert r.returncode == 0, r.stderr + r.stdout
    kept = (inst / "buttons.yaml").read_text()
    assert "Моя" in kept
    assert "Создать КП" not in kept, "правки владельца не затираются кнопками мозга"
