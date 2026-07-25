"""TDD мозга мета-агента brains/unpacker/ (§7.1–7.6).

Мозг — это документ, а не код, но у него есть ПРОВЕРЯЕМЫЕ контракты, и только они здесь и
проверяются. Ревью честности тестов (находки M3/M4) вычистило из этого файла фразовые грепы
вида «в скилле есть слова "одним блоком"»: наличие подстроки не доказывает поведения агента,
зато создаёт ощущение покрытия и мешает править текст протокола. Осталось то, что можно
сверить с кодом или с артефактом:

- паспорт `.brain.yaml` разбирается ПРОДАКШН-парсером `engine.core.brain` (не самописным);
- каждый флаг, названный рядом с нашим скриптом, в этом скрипте существует;
- каждая команда `agentctl.sh` из документов существует в `agentctl.sh`;
- скрипты зовутся полным путём и в форме, которую пропустит whitelist sudoers;
- отчёт о деплое пишется АБСОЛЮТНЫМ путём вне папки-мозга (иначе агент пишет внутрь мозга);
- в выдаваемом репо нет живых секретов.

Дословные строки-артефакты (вопрос-подтверждение §7.4, разделы отчёта) проверяются как
артефакты: их формулировку задаёт конституция, а не настроение автора скилла.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

# Паспорта поставляемых мозгов проверяем ПРОДАКШН-парсером, а не самописным в тесте (M1):
# иначе тест зелёный на файле, который движок у ученика отвергнет. Фикстуры api_base и
# isolated_runtime приходят из engine/tests/conftest.py — импортировать их больше не нужно.
from engine.core.brain import load_passport

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
    "update.sh": REPO / "update.sh",
}

# Долгов нет: `--token-file`/`--cc-token-file` доехали в deploy.sh (Р5 — секрет передаётся
# файлом, а не argv, иначе токен навсегда в /var/log/auth.log). Пустой словарь оставлен
# сознательно: пока он пуст, проверка «мозг не выдумывает флаги» работает без дырок.
PENDING_FLAGS: dict[str, set[str]] = {}


def _brain_docs() -> list[Path]:
    """Файлы мозга, README и наша дока по деплою.

    У всех трёх один и тот же риск: напечатанный флаг или подкоманда, которых в скрипте нет.
    README тут не для красоты — он печатает те же вызовы `agentctl.sh`, и мёртвая подкоманда
    в нём стоит ученику того же, что мёртвый флаг в скилле.
    """
    docs = [p for p in BRAIN.rglob("*") if p.is_file()]
    docs += [p for p in (REPO / "docs" / "deploy").rglob("*.md") if p.is_file()]
    docs.append(REPO / "README.md")
    return sorted(docs)


# ── структура мозга ─────────────────────────────────────────────────────────


def test_brain_layout():
    assert CLAUDE_MD.exists(), "мозг без CLAUDE.md не мозг (§4)"
    assert PASSPORT.exists(), "нужен паспорт .brain.yaml с кнопками-триггерами"
    assert SKILL_DEPLOY.exists() and SKILL_OPERATE.exists()


def test_passport_buttons_follow_contract():
    """Паспорт мозга читает тот же парсер, что и движок в бою (находка M1).

    Самописный YAML-парсер в тесте проверял не продукт, а сам себя: `extra="forbid"`,
    лимиты длины и чистку label он не знал, поэтому мозг с лишним ключом проходил тест
    и падал на деплое.
    """
    passport = load_passport(BRAIN)
    assert passport is not None, "паспорт обязателен именно у мозга Распаковщика"
    assert passport.slug == "unpacker"
    assert passport.name, "имя мозга видно человеку в списке проектов"
    assert len(passport.buttons) >= 3, "кнопки-триггеры — часть продукта, а не украшение"


# ── жёсткие правила личности ────────────────────────────────────────────────


def test_claude_md_hard_rules():
    """Дословные правила §7.4/§7.5, которые задаёт конституция, а не автор текста."""
    body = CLAUDE_MD.read_text()
    low = body.lower()
    assert "«да»" in low or '"да"' in low, "правило «без да не деплою» должно быть дословно"
    assert "--dry-run" in body, "сначала всегда прогон вхолостую"
    assert "[REDACTED]" in body, "секреты в чат — только замазанными"
    assert "deploy.sh" in body and "agentctl.sh" in body, "маршрут к разрешённым скриптам"


def test_claude_md_routes_to_both_skills():
    body = CLAUDE_MD.read_text()
    assert "deploy-surface" in body and "operate" in body


# ── скиллы: артефакты протокола ─────────────────────────────────────────────


def _frontmatter(p: Path) -> str:
    text = p.read_text()
    assert text.startswith("---\n"), f"{p.name} без frontmatter"
    return text.split("---", 2)[1]


def test_skills_have_frontmatter_with_name_and_description():
    """Без frontmatter скилл не подхватится Claude Code — это контракт формата, не стиль."""
    for p in (SKILL_DEPLOY, SKILL_OPERATE):
        fm = _frontmatter(p)
        assert re.search(r"^name:\s*\S", fm, re.M), f"{p} без name"
        assert re.search(r"^description:\s*\S", fm, re.M), f"{p} без description"


def test_deploy_skill_asks_confirmation_in_the_wording_of_the_constitution():
    """§7.4 задаёт вопрос-подтверждение ДОСЛОВНО — это артефакт, а не пересказ."""
    low = SKILL_DEPLOY.read_text().lower()
    assert "разворачиваю? (да / поправить / стоп)" in low
    assert "--dry-run" in low, "первый прогон — вхолостую"


def test_deploy_skill_documents_report_sections():
    """Шаблон отчёта §7.4 — структура файла, которую агент воспроизводит."""
    body = SKILL_DEPLOY.read_text()
    for section in ("Что развернули", "Гейты", "Проверка", "Что дальше"):
        assert section in body, f"в формате отчёта нет раздела «{section}»"


def test_deploy_report_path_is_absolute_and_outside_the_brain():
    """M-05/C20: относительный `deploys/` = запись ВНУТРЬ папки-мозга.

    cwd агента — папка-мозг (§4), значит относительный путь отчёта ведёт в мозг. А мозг у
    Распаковщика лежит в дереве движка (root-owned, read-only), да и §4 прямо запрещает
    агенту писать в мозг: git-мозг становится грязным и гейт чистоты блокирует следующий
    деплой. Отчёт живёт в состоянии инстанса.
    """
    found: list[tuple[Path, str]] = []
    for doc in _brain_docs():
        if doc.suffix != ".md":
            continue
        for path in re.findall(r"[\w~/.<>-]*deploys/<дата>-<name>\.md", doc.read_text()):
            found.append((doc, path))
    assert found, "путь отчёта с форматом имени должен быть назван (§7.4)"
    assert any(doc == SKILL_DEPLOY for doc, _ in found), "в скилле деплоя — обязательно"
    for doc, path in found:
        where = doc.relative_to(REPO)
        assert path.startswith(("/", "~/")), f"{where}: путь отчёта не абсолютный: {path}"
        assert "/state/" in path, f"{where}: отчёт пишется в состояние инстанса (§4): {path}"
        assert "brains/" not in path, f"{where}: отчёт не может лежать в папке-мозге: {path}"


def test_operate_skill_does_not_restart_unit_after_button_edit():
    """C19: ButtonRegistry перечитывает buttons.yaml сам — рестарт не нужен и вреден.

    Для самого Распаковщика рестарт своего юнита посреди диалога = обрыв разговора с
    владельцем на полуслове (см. engine/core/buttons.py: реестр сверяет отпечаток файла).
    """
    body = SKILL_OPERATE.read_text()
    section = body.split("## «Добавь кнопку»", 1)
    assert len(section) == 2, "в скилле должен быть раздел про добавление кнопки"
    tail = section[1].split("\n## ", 1)[0].lower()
    assert "systemctl restart" not in tail, "рестарта юнита из-за одной кнопки быть не должно"
    # Ищем именно ДЕЙСТВИЕ («рестартую юнит», «перезапускаю бота»), а не слово «рестарт»:
    # объяснить владельцу, почему рестарта НЕ будет, скилл обязан.
    assert not re.search(r"(рестарт\w*|перезапус\w*)\s+(юнит|бот)", tail), (
        "после правки buttons.yaml рестарт не нужен: реестр перечитывает файл сам"
    )


def test_operate_skill_does_not_promise_usage_command():
    """M-06/C10/ADV-11: обещания расхода нет ни в скилле, ни в кнопках паспорта.

    Перевёрнутый тест: раньше здесь стояло `assert "/usage" in body` — тест ЗАКРЕПЛЯЛ
    дефект. Команды в боте нет (Фаза 2), поэтому владелец получал бы выдуманные цифры.
    """
    body = SKILL_OPERATE.read_text()
    assert "/usage" not in body, "команды /usage в боте нет — обещать её нельзя"
    low = body.lower()
    assert "не умею" in low or "не считает" in low, "на вопрос о расходе — честное «не умею»"
    buttons = load_passport(BRAIN)
    assert buttons is not None
    for b in buttons.buttons:
        assert "потратил" not in b.prompt.lower(), (
            f"кнопка «{b.label}» просит расход, которого движок не считает"
        )


def test_secrets_are_handed_over_by_file_not_by_chat():
    """Р5/SEC-4: токен не просят прислать в чат — он остаётся там навсегда.

    Чат Распаковщика лежит в `~/.claude/projects/*.jsonl` и читается самим агентом, а
    значение из argv попадает в `/var/log/auth.log`. Поэтому протокол ведёт через файл 600
    и напоминает про перевыпуск, если токен где-то светился.
    """
    for skill in (SKILL_DEPLOY, SKILL_OPERATE):
        body = skill.read_text()
        low = body.lower()
        assert not re.search(r"пришл\w+\s+(его|мне|токен)", low), (
            f"{skill.name}: нельзя просить владельца прислать секрет в чат"
        )
        assert "chmod 600" in body, f"{skill.name}: файл с секретом создаётся с правами 600"
        assert "перевыпуст" in low, f"{skill.name}: напоминание о перевыпуске светившегося токена"


# ── главное: никаких выдуманных флагов и команд ─────────────────────────────


def _script_flags(path: Path) -> set[str]:
    return set(re.findall(r"--[a-z][a-z0-9-]+", path.read_text()))


def test_no_pending_flags_left():
    """Страховка от вечного исключения: список долгов обязан быть пустым.

    Каждое имя здесь — это флаг, который мозгу разрешено печатать, не имея его в скрипте.
    То есть ровно та ситуация, от которой защищает соседний тест: агент говорит владельцу
    команду, а скрипт отвечает «Неизвестный флаг».
    """
    assert not PENDING_FLAGS, (
        f"долги по флагам не закрыты: {PENDING_FLAGS} — реализовать флаг в скрипте "
        "или убрать его из документов мозга, но не держать исключение"
    )


def test_brain_mentions_only_real_script_flags():
    """Каждый флаг, названный в строке с нашим скриптом, обязан в нём существовать."""
    known = {name: _script_flags(p) | PENDING_FLAGS.get(name, set()) for name, p in SCRIPTS.items()}
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


# ── вызов скриптов: полный путь и форма, которую пропустит sudoers ───────────

# «Скрипт ПОЗВАЛИ» = за именем идёт латинский токен: флаг (`--dry-run`) или подкоманда
# (`doctor`, `restart`). Русский текст после имени — это проза («update.sh ставит релиз»),
# и трогать её незачем. Прежняя эвристика перечисляла первые буквы подкоманд ("d","l","s","h")
# и пропускала `agentctl.sh restart` (находка M4) — теперь это регексп, а не список букв.
_CALLED = r"(?=\s+(?:--?[a-z]|[a-z][a-z0-9-]*(?:\s|$)))"
_PATHED = re.compile(r"[/~][\w./~-]*/$")


def bare_script_calls(text: str, scripts: tuple[str, ...] = tuple(SCRIPTS)) -> list[str]:
    """Строки, где наш скрипт ЗОВУТ, не назвав полный путь.

    sudo сверяет ровно тот путь, которым команду позвали, а whitelist §7.5 перечисляет
    абсолютные пути: короткое имя = «команда не найдена» либо запрос пароля.
    """
    out: list[str] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for script in scripts:
            for m in re.finditer(re.escape(script) + _CALLED, line):
                before = line[: m.start()]
                if not _PATHED.search(before):
                    out.append(f"{lineno}: {line.strip()[:90]}")
    return out


def test_bare_script_calls_detects_real_call_forms():
    """Мутационная проверка самой эвристики (M4): без неё тест ниже может быть пустым."""
    # позитив: именно эти формы ученику и ломались
    assert bare_script_calls("agentctl.sh restart unpacker")
    assert bare_script_calls("update.sh --ref v1.2.0")
    assert bare_script_calls("sudo deploy.sh --surface tg --name x")
    # негатив: полный путь и упоминание в прозе — не вызов
    assert not bare_script_calls("sudo /opt/unpacker/deploy/agentctl.sh doctor unpacker")
    assert not bare_script_calls("`/opt/unpacker/update.sh` --dry-run")
    assert not bare_script_calls("работаю только через `deploy.sh`, `update.sh`, `agentctl.sh`")
    assert not bare_script_calls("update.sh ставит последний тег-релиз")


def test_brain_calls_scripts_by_absolute_path():
    problems: list[str] = []
    for doc in _brain_docs():
        if doc.suffix != ".md":
            continue
        for hit in bare_script_calls(doc.read_text()):
            problems.append(f"{doc.relative_to(REPO)}:{hit}")
    assert not problems, "скрипты надо звать полным путём:\n" + "\n".join(sorted(set(problems)))


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
