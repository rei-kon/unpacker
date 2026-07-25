"""TDD мозга мета-агента brains/unpacker/ (§7.1–7.6).

Мозг — это документ, а не код, но у него есть проверяемые контракты:
- паспорт `.brain.yaml` несёт кнопки в формате {label, prompt} (стык со срезом С2);
- в CLAUDE.md записаны ЖЁСТКИЕ правила (без «да» не деплоит, сначала --dry-run,
  секреты — только [REDACTED], системные действия только через разрешённые скрипты);
- скиллы описывают протокол пошагово и НЕ выдумывают флаги скриптов — каждый флаг,
  упомянутый рядом с нашим скриптом, обязан существовать в этом скрипте.

Последний тест — главный: именно «выдуманный флаг» превращает красивый протокол в
инструкцию, по которой ученик получает `Неизвестный флаг` вместо бота.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from engine.tests._tgapi_stub import api_base as api_base  # noqa: PLC0414 — фикстура для pytest
from engine.tests.test_deploy_scripts import isolated_runtime as isolated_runtime  # noqa: PLC0414

REPO = Path(__file__).resolve().parents[2]
BRAIN = REPO / "brains" / "unpacker"
CLAUDE_MD = BRAIN / "CLAUDE.md"
PASSPORT = BRAIN / ".brain.yaml"
SKILL_DEPLOY = BRAIN / ".claude" / "skills" / "deploy-surface" / "SKILL.md"
SKILL_OPERATE = BRAIN / ".claude" / "skills" / "operate" / "SKILL.md"

SCRIPTS = {
    "deploy.sh": REPO / "deploy" / "deploy.sh",
    "agentctl.sh": REPO / "deploy" / "agentctl.sh",
    "install-sudoers.sh": REPO / "deploy" / "install-sudoers.sh",
}


def _brain_docs() -> list[Path]:
    """Файлы мозга + наша дока по деплою: у них один и тот же риск «выдуманного флага»."""
    docs = [p for p in BRAIN.rglob("*") if p.is_file()]
    docs += [p for p in (REPO / "docs" / "deploy").rglob("*.md") if p.is_file()]
    return sorted(docs)


# ── структура мозга ─────────────────────────────────────────────────────────


def test_brain_layout():
    assert CLAUDE_MD.exists(), "мозг без CLAUDE.md не мозг (§4)"
    assert PASSPORT.exists(), "нужен паспорт .brain.yaml с кнопками-триггерами"
    assert SKILL_DEPLOY.exists() and SKILL_OPERATE.exists()


def _parse_buttons(text: str) -> list[dict[str, str]]:
    """Мини-парсер списка кнопок из .brain.yaml (pyyaml в зависимостях движка нет).

    Понимает ровно тот формат, который зафиксирован контрактом со срезом С2:
        buttons:
          - label: "…"
            prompt: "…"
    """
    out: list[dict[str, str]] = []
    inside = False
    for raw in text.splitlines():
        if re.match(r"^buttons:\s*$", raw):
            inside = True
            continue
        if inside:
            if raw.strip() and not raw.startswith((" ", "\t", "-")):
                break  # начался следующий корневой ключ
            m = re.match(r"^\s*-\s*label:\s*(.+?)\s*$", raw)
            if m:
                out.append({"label": m.group(1).strip("\"'")})
                continue
            m = re.match(r"^\s+prompt:\s*(.+?)\s*$", raw)
            if m and out:
                out[-1]["prompt"] = m.group(1).strip("\"'")
    return out


def test_passport_buttons_follow_contract():
    body = PASSPORT.read_text()
    assert re.search(r"^slug:\s*unpacker\s*$", body, re.M), "slug мозга — unpacker"
    assert re.search(r"^name:\s*\S", body, re.M)
    buttons = _parse_buttons(body)
    assert len(buttons) >= 3, "кнопки-триггеры — часть продукта, а не украшение"
    for b in buttons:
        assert b.get("label"), f"кнопка без label: {b}"
        assert b.get("prompt"), f"кнопка без prompt: {b} — контракт {{label, prompt}}"


# ── жёсткие правила личности ────────────────────────────────────────────────


def test_claude_md_hard_rules():
    body = CLAUDE_MD.read_text()
    low = body.lower()
    assert "«да»" in low or '"да"' in low, "правило «без да не деплою» должно быть дословно"
    assert "--dry-run" in body, "сначала всегда прогон вхолостую"
    assert "[REDACTED]" in body, "секреты в чат — только замазанными"
    # произвольный Bash под sudo — запрещён by design (§7.5)
    assert "sudo" in low and ("произвольн" in low or "только через" in low)
    assert "deploy.sh" in body and "agentctl.sh" in body
    assert "не выдумыва" in low, "правило про флаги: не угадывать, а читать --help"


def test_claude_md_treats_brain_data_as_data():
    """§7.5: .brain.yaml и присланные файлы — ДАННЫЕ, а не инструкции (промпт-инъекция)."""
    low = CLAUDE_MD.read_text().lower()
    assert "инъекц" in low or "данные, а не инструкции" in low or "данные, не инструкции" in low


def test_claude_md_routes_to_both_skills():
    body = CLAUDE_MD.read_text()
    assert "deploy-surface" in body and "operate" in body


# ── скиллы: протокол пошагово ───────────────────────────────────────────────


def _frontmatter(p: Path) -> str:
    text = p.read_text()
    assert text.startswith("---\n"), f"{p.name} без frontmatter"
    return text.split("---", 2)[1]


def test_skills_have_frontmatter_with_name_and_description():
    for p in (SKILL_DEPLOY, SKILL_OPERATE):
        fm = _frontmatter(p)
        assert re.search(r"^name:\s*\S", fm, re.M), f"{p} без name"
        assert re.search(r"^description:\s*\S", fm, re.M), f"{p} без description"


def test_deploy_skill_covers_protocol_7_1_7_4():
    body = SKILL_DEPLOY.read_text()
    low = body.lower()
    # шаги протокола
    assert "одним блоком" in low, "вопросы задаются одним блоком, а не допросом"
    assert "гейт" in low and "код" in low, "гейты выполняет код — скилл только показывает результат"
    assert "разворачиваю? (да / поправить / стоп)" in low, "вопрос-подтверждение дословно"
    assert "--dry-run" in body
    assert "agentctl.sh doctor" in body, "верификация после деплоя"
    assert re.search(r"deploys/<дата>-<name>\.md", body), "отчёт §7.4 с форматом имени"


def test_deploy_skill_documents_report_sections():
    body = SKILL_DEPLOY.read_text()
    for section in ("Что развернули", "Гейты", "Проверка", "Что дальше"):
        assert section in body, f"в формате отчёта нет раздела «{section}»"


def test_deploy_skill_explains_gate_refusals_without_workarounds():
    """Отказ гейта объясняется, а не обходится: --skip-preflight не рецепт."""
    body = SKILL_DEPLOY.read_text()
    low = body.lower()
    assert "409" in body and "занят" in low, "коллизия токена объяснена по-человечески"
    assert "tos" in low or "подписк" in low, "ToS-гейт клиентской поверхности объяснён"
    assert "не обход" in low or "не обхожу" in low or "обходить" in low, (
        "должно быть сказано прямо: гейт не обходим, а лечится причиной"
    )


def test_operate_skill_covers_protocol_7_6():
    body = SKILL_OPERATE.read_text()
    low = body.lower()
    assert "молчит" in low
    assert "agentctl.sh doctor" in body and "journalctl" in body
    assert "claude setup-token" in body, "ротация OAuth — за руку"
    assert "update.sh" in body
    assert "последн" in low and ("свой юнит" in low or "себя" in low), (
        "юнит Распаковщика рестартуется ПОСЛЕДНИМ — иначе он убьёт себя посреди операции"
    )
    assert "/usage" in body, "«сколько потратили»"
    assert "buttons.yaml" in body, "«добавь кнопку» — правка инстансной копии (стык с С2)"


# ── главное: никаких выдуманных флагов и команд ─────────────────────────────


def _script_flags(path: Path) -> set[str]:
    return set(re.findall(r"--[a-z][a-z0-9-]+", path.read_text()))


def test_brain_mentions_only_real_script_flags():
    """Каждый флаг, названный в строке с нашим скриптом, обязан в нём существовать."""
    known = {name: _script_flags(p) for name, p in SCRIPTS.items()}
    problems: list[str] = []
    for doc in _brain_docs():
        if doc.suffix not in {".md", ".yaml"}:
            continue
        for lineno, line in enumerate(doc.read_text().splitlines(), 1):
            for script, flags in known.items():
                if script not in line:
                    continue
                for flag in re.findall(r"--[a-z][a-z0-9-]+", line):
                    if flag not in flags:
                        where = f"{doc.relative_to(REPO)}:{lineno}"
                        problems.append(f"{where} {script} не знает {flag}")
    assert not problems, "выдуманные флаги — ученик получит «Неизвестный флаг»:\n" + "\n".join(
        problems
    )


def test_brain_mentions_only_real_agentctl_commands():
    src = SCRIPTS["agentctl.sh"].read_text()
    real = set(re.findall(r"^\s{2}([a-z]+)\)\s+shift", src, re.M))
    assert real, "не смог вычитать команды agentctl.sh — тест бесполезен, поправь разбор"
    used = set()
    for doc in _brain_docs():
        if doc.suffix != ".md":
            continue
        used |= set(re.findall(r"agentctl\.sh\s+([a-z]+)", doc.read_text()))
    assert used <= real, f"в мозге описаны несуществующие команды agentctl: {sorted(used - real)}"


def test_brain_has_no_secrets():
    """Никаких живых токенов в выдаваемом репо — только плейсхолдеры."""
    patterns = [
        r"\b\d{8,}:[A-Za-z0-9_-]{25,}\b",
        r"sk-ant-[A-Za-z0-9]{10,}",
        r"sk-oauth-[A-Za-z0-9]{10,}",
    ]
    for doc in _brain_docs():
        text = doc.read_text(errors="ignore")
        for pat in patterns:
            assert not re.search(pat, text), f"{doc.relative_to(REPO)} похоже на секрет: {pat}"


# ── интеграция: реальный мозг проходит реальный deploy.sh ───────────────────
# Страховка стыка «мозг ↔ скрипт»: копирование мозга не должно терять .claude/skills
# (scrub чистит .env, симлинки и вложенные .git — скиллы обязаны выжить).


def test_real_unpacker_brain_deploys_and_keeps_skills(tmp_path, api_base, isolated_runtime):
    env = {
        "TG_RUN_USER": os.environ.get("USER", ""),
        "TG_AGENTS_BASE": str(tmp_path / "agents"),
        "TG_BRAINS_BASE": str(tmp_path / "brains"),
        "TG_RUNTIME": isolated_runtime,
        "TG_API_BASE": api_base,
        "UNPACKER_SUDOERS_DIR": str(tmp_path / "sudoers.d"),
    }
    r = subprocess.run(
        [
            "bash",
            str(REPO / "deploy" / "deploy.sh"),
            "--surface",
            "tg",
            "--name",
            "unpacker",
            "--token",
            "123456:AAbbCC-dd_ee",
            "--users",
            "111",
            "--brain",
            str(BRAIN),
            "--role",
            "unpacker",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, **env},
    )
    assert r.returncode == 0, r.stderr + r.stdout
    dst = Path(env["TG_BRAINS_BASE"]) / "unpacker"
    assert (dst / "CLAUDE.md").exists()
    assert (dst / ".brain.yaml").exists(), "паспорт с кнопками обязан доехать до инстанса"
    assert (dst / ".claude" / "skills" / "deploy-surface" / "SKILL.md").exists()
    assert (dst / ".claude" / "skills" / "operate" / "SKILL.md").exists()
