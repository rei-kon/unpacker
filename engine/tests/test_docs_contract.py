"""Контракт-тесты документов: документ не имеет права обещать то, чего нет в коде.

Ревью Фазы 1b нашло целый класс дефектов одной природы: скилл обещает `/usage`, README
описывает команды, которых в боте нет, шаблон паспорта продаёт поля без потребителя. Ученику
это стоит дороже любого бага в коде — он делает ровно то, что написано, и получает тишину.

Поэтому здесь не грепы «в тексте есть нужная фраза» (так проверяется настроение автора,
а не продукт), а сверки документа с ИСТОЧНИКОМ ПРАВДЫ:

- каждая `/команда` из README и документов мозга зарегистрирована в `bot.py`;
- каждое поле `Settings` (кроме секретов) есть в шаблоне `.env` — читаем сам шаблон;
- поставляемые паспорта разбираются ПРОДАКШН-парсером `engine.core.brain`;
- поля паспорта без потребителя в коде помечены в шаблоне честно;
- пути и файлы, названные в README, существуют; команды напечатаны в форме, которую
  разрешает whitelist sudoers (§7.5).
"""

from __future__ import annotations

import re
from pathlib import Path

from engine.core.brain import BrainPassport, PassportError, load_passport
from engine.core.config import Settings

REPO = Path(__file__).resolve().parents[2]
README = REPO / "README.md"
BOT_PY = REPO / "engine" / "adapters" / "telegram" / "bot.py"
BRAINS = REPO / "brains"
EXAMPLES = REPO / "examples"
ENV_TEMPLATE = REPO / "deploy" / "templates" / ".env.template"
TEMPLATE_PASSPORT = BRAINS / "_template" / ".brain.yaml"

# Паспорта, которые мы ВЫДАЁМ ученику: шаблон, мозг Распаковщика и рабочий пример.
SHIPPED_PASSPORTS = (BRAINS / "_template", BRAINS / "unpacker", EXAMPLES / "assistant")

# Каталог движка на VPS: команды в документах печатаются полным путём, и каждый такой путь
# обязан существовать в репо (иначе ученик копирует команду в пустоту).
RUNTIME_PREFIX = "/opt/unpacker/"

# Первые сегменты путей, которые ЕСТЬ в репо: только их и проверяем на существование.
# `state/uploads` и `sudoers.d/…` живут на сервере, а не в дереве репо.
REPO_DIRS = frozenset({"brains", "examples", "docs", "engine", "deploy", "canaries"})


def _shipped_docs() -> list[Path]:
    """Документы, которые читает ученик: README + всё, что едет внутри мозгов и примеров."""
    docs = [README]
    for root in (BRAINS, EXAMPLES):
        docs += [p for p in root.rglob("*.md") if p.is_file()]
    return sorted(docs)


def _shipped_texts() -> list[Path]:
    """То же плюс паспорта: комментарий в `.brain.yaml` ученик читает так же, как README."""
    docs = _shipped_docs()
    for root in (BRAINS, EXAMPLES):
        docs += [p for p in root.rglob("*.yaml") if p.is_file()]
    return sorted(docs)


# ── команды бота: обещание = регистрация в bot.py ────────────────────────────

# Команда в тексте — это `/слово`, а не часть пути. Отсекаем три вида ложных срабатываний:
# путь перед слэшем (`~/agents/…`, `<имя>/…`), продолжение пути после слова (`/opt/unpacker`)
# и файл с расширением в конце пути (`<имя>/buttons.yaml`).
_CMD_IN_TEXT = re.compile(
    r"(?<![\w/.~>-])/([a-z][a-z0-9_]{1,20})(?![\w/])(?!\.[a-z]{2,8}\b)",
)
_CMD_REGISTERED = re.compile(r"""Command\(\s*["']([a-z_]+)["']""")

# Команды НЕ нашего бота: их обрабатывает @BotFather, к движку они отношения не имеют.
FOREIGN_COMMANDS = frozenset({"newbot", "revoke", "mybots", "setcommands", "token"})


def registered_commands() -> set[str]:
    found = set(_CMD_REGISTERED.findall(BOT_PY.read_text()))
    assert found, "не смог вычитать регистрацию команд из bot.py — тест бесполезен, поправь разбор"
    return found


def commands_mentioned(text: str) -> set[str]:
    return set(_CMD_IN_TEXT.findall(text)) - FOREIGN_COMMANDS


def test_command_extraction_ignores_paths_and_catches_commands():
    """Мутационная проверка самого разбора: без неё тест ниже может быть тихо пустым."""
    assert commands_mentioned("напиши `/usage` боту") == {"usage"}
    assert commands_mentioned("/opt/unpacker/deploy/deploy.sh --dry-run") == set()
    assert commands_mentioned("файлы в ~/agents/<имя>/state/uploads/") == set()
    assert commands_mentioned("правь `~/agents/<имя>/buttons.yaml`") == set()
    assert commands_mentioned("маркер в state/health.json") == set()
    assert commands_mentioned("`/newbot` у @BotFather") == set()
    assert commands_mentioned("| `/model` | сменить модель |") == {"model"}


def test_docs_promise_only_registered_commands():
    """Обещанная в документе команда обязана быть зарегистрирована в боте.

    Ровно этот тест поймал бы `/usage` в скилле operate: ученик нажимал кнопку «Сколько
    потратили», агент отправлял боту команду, которой нет, — и выдумывал цифры.
    """
    real = registered_commands()
    problems: list[str] = []
    for doc in _shipped_docs():
        for lineno, line in enumerate(doc.read_text().splitlines(), 1):
            for cmd in sorted(commands_mentioned(line)):
                if cmd not in real:
                    problems.append(f"{doc.relative_to(REPO)}:{lineno} обещает /{cmd}")
    assert not problems, "документ обещает команду, которой в боте нет:\n" + "\n".join(problems)


def test_readme_command_table_matches_bot():
    """README — единственная карта команд для владельца: расхождение с ботом = ложь.

    В обе стороны: описали несуществующее — ученик стучится в пустоту; не описали живое —
    ученик про фичу не узнает (так и случилось с `/switch` и `/verbose`).
    """
    real = registered_commands()
    documented = commands_mentioned(README.read_text())
    assert not documented - real, f"README описывает несуществующие команды: {documented - real}"
    assert not real - documented, f"README не описывает живые команды: {sorted(real - documented)}"


# ── поставляемые паспорта: разбор ПРОДАКШН-парсером (M1) ─────────────────────


def test_shipped_passports_parse_with_production_parser():
    """Паспорт, который мы выдаём, обязан пройти тот же парсер, что стоит в бою.

    До этого оба теста репо разбирали `.brain.yaml` самописными регекспами: они не знали
    ни `extra="forbid"`, ни лимитов длины, ни чистки label — поэтому паспорт с лишним ключом
    был «зелёным» в тестах и падал у ученика на деплое (`PassportError`).
    """
    for brain in SHIPPED_PASSPORTS:
        try:
            passport = load_passport(brain)
        except PassportError as exc:  # pragma: no cover — красный тест и есть смысл проверки
            raise AssertionError(
                f"{brain.relative_to(REPO)}: паспорт не проходит парсер — {exc}"
            ) from exc
        assert passport is not None, f"{brain.relative_to(REPO)}: паспорт обязателен в поставке"
        assert passport.buttons, f"{brain.relative_to(REPO)}: без кнопок шаблон не учит формату"


# ── поля паспорта: обещание «работает» ↔ наличие потребителя (M-14) ──────────


def passport_fields_with_consumer() -> set[str]:
    """Какие поля паспорта КТО-ТО в движке действительно читает."""
    used: set[str] = set()
    for src in (REPO / "engine").rglob("*.py"):
        if src.name == "brain.py" or "tests" in src.parts:
            continue  # brain.py объявляет поля, тесты не потребители
        text = src.read_text()
        for field in BrainPassport.model_fields:
            if re.search(rf"passport\.{field}\b", text):
                used.add(field)
    return used


def _comment_block_above(text: str, key: str) -> str:
    """Комментарии, стоящие непосредственно над ключом `key:` — там и живёт пояснение."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.match(rf"^{re.escape(key)}:", line):
            block = []
            j = i - 1
            while j >= 0 and lines[j].lstrip().startswith("#"):
                block.append(lines[j])
                j -= 1
            return "\n".join(block).lower()
    raise AssertionError(f"в {TEMPLATE_PASSPORT.name} нет ключа {key}")


def test_passport_fields_without_consumer_are_marked_honestly():
    """M-14: поле паспорта без потребителя описано как «пока не читается», и наоборот.

    Ученик заполняет `name`/`slug`/`default_model`, ждёт эффекта и не получает его: имя,
    slug и модель приезжают в инстанс флагами `deploy.sh`, а из паспорта движок сегодня
    читает только `buttons`. Проверка симметричная: появился потребитель — честную пометку
    надо снять, иначе она станет новой ложью.
    """
    text = TEMPLATE_PASSPORT.read_text()
    consumed = passport_fields_with_consumer()
    assert "buttons" in consumed, "потребителя кнопок не нашлось — тест бесполезен, поправь разбор"
    for field in BrainPassport.model_fields:
        note = _comment_block_above(text, field)
        marked = "пока не читает" in note or "фаза 2" in note
        if field in consumed:
            assert not marked, (
                f"{field}: потребитель уже есть, а пометка «пока не читается» осталась"
            )
        else:
            assert marked, (
                f"{field}: потребителя в движке нет — так и напиши в шаблоне паспорта, "
                "иначе ученик правит поле и ждёт эффекта"
            )


# ── поля Settings ↔ шаблон .env (M-13, читаем сам шаблон) ────────────────────

# Секреты в шаблон не пишутся значением: их дописывает deploy.sh через printf, поэтому
# в карте настроек они присутствуют только как закомментированные имена.
SECRET_FIELDS = frozenset({"telegram_bot_token", "claude_code_oauth_token"})

# Ручки, которых в шаблоне пока нет. Их вписывает параллельная зона deploy (M-13 + Р8).
# Список СТРОГИЙ: появился ключ в шаблоне — тест краснеет и требует убрать его отсюда.
PENDING_ENV_KEYS = frozenset(
    {
        "SYSTEM_PROMPT",
        "SYSTEM_PROMPT_APPEND",
        "MAX_TURNS",
        "BUTTONS_ENABLED",
        "BUTTONS_PATH",
        "UPLOADS_ENABLED",
        "UPLOADS_DIR",
        "MAX_UPLOAD_BYTES",
        "SEND_FILE_ENABLED",
    }
)


def _env_template_keys() -> set[str]:
    """Ключи из шаблона — включая закомментированные: это карта настроек, а не только дефолты."""
    body = ENV_TEMPLATE.read_text()
    keys = set(re.findall(r"^\s*#?\s*([A-Z][A-Z0-9_]+)=", body, re.M))
    assert keys, "не смог вычитать ключи из шаблона .env — тест бесполезен, поправь разбор"
    return keys


def test_env_template_documents_every_settings_field():
    """`.env.template` объявляет себя картой настроек — значит карта обязана быть полной.

    Читаем ШАБЛОН, а не литерал в тесте: иначе тест проверял бы сам себя. Новая ручка в
    Settings без строки в шаблоне = ручка, о которой ученик никогда не узнает.
    """
    keys = _env_template_keys()
    missing = sorted(
        f.upper()
        for f in Settings.model_fields
        if f not in SECRET_FIELDS and f.upper() not in keys and f.upper() not in PENDING_ENV_KEYS
    )
    assert not missing, (
        "поля Settings без строки в deploy/templates/.env.template: "
        f"{missing} — либо вписать в шаблон, либо (если это чужая зона) в PENDING_ENV_KEYS"
    )


def test_pending_env_keys_are_still_missing():
    """Страховка от вечного исключения: ключ появился в шаблоне — убери его из PENDING."""
    arrived = sorted(PENDING_ENV_KEYS & _env_template_keys())
    assert not arrived, (
        f"шаблон .env уже знает {arrived} — убери их из PENDING_ENV_KEYS, "
        "иначе полнота карты настроек больше ничем не держится"
    )


# ── форма команд: то, что напечатано, ученик выполняет буквально ─────────────


def test_docs_do_not_run_our_scripts_through_a_shell_under_sudo():
    """`sudo bash update.sh` = «user is not allowed to execute /bin/bash» (M-18).

    Whitelist §7.5 сознательно не содержит оболочек: с `bash` в списке whitelist
    бессмысленен. Значит и в документах команда должна печататься в той форме, которую
    sudo пропустит — сам скрипт полным путём.
    """
    problems: list[str] = []
    for doc in _shipped_docs():
        for lineno, line in enumerate(doc.read_text().splitlines(), 1):
            if re.search(r"sudo\s+(bash|sh|env)\s+\S*\.sh", line):
                problems.append(f"{doc.relative_to(REPO)}:{lineno} → {line.strip()[:90]}")
    assert not problems, "sudo не пустит оболочку — печатай сам скрипт:\n" + "\n".join(problems)


def test_readme_paths_are_absolute_because_its_terminal_belongs_to_root():
    """ADV-08: `~` в README — это `/root` (mode 700), куда юзеру движка хода нет.

    README читает человек, который вошёл `ssh root@IP`. Мозг, положенный в `~/brains`,
    гейты (их проверяет root) увидят, а материализация мозга идёт от юзера движка — и
    падает `Permission denied` посреди провизии. Поэтому в README пути только абсолютные.
    """
    hits = [
        f"{lineno}: {line.strip()[:90]}"
        for lineno, line in enumerate(README.read_text().splitlines(), 1)
        if "~/" in line
    ]
    assert not hits, "в README путь с `~` читается как /root — пиши абсолютный:\n" + "\n".join(hits)


def test_docs_deploy_brains_by_absolute_path():
    """M-15: относительный путь мозга гейт отвергнет — cwd бота это папка-мозг.

    Фраза «разверни мозг examples/assistant» учит ровно тому, что не работает: Распаковщик
    получит относительный путь и не найдёт папку (его рабочий каталог — свой собственный мозг).
    """
    problems: list[str] = []
    for doc in _shipped_texts():
        for lineno, line in enumerate(doc.read_text().splitlines(), 1):
            for path in re.findall(r"разверни мозг\s+([^\s,]+)", line):
                if not path.startswith("/"):
                    problems.append(f"{doc.relative_to(REPO)}:{lineno} → {path}")
    assert not problems, "путь мозга в примере обязан быть абсолютным:\n" + "\n".join(problems)


# ── README: якоря-заголовки и существование того, на что он ссылается (S4) ───


def _readme_headings() -> list[str]:
    return [
        m.group(1).strip().lower()
        for m in re.finditer(r"^#{2,3}\s+(.+)$", README.read_text(), re.M)
    ]


def test_readme_newbie_path_sections_exist_as_headings():
    """§11: путь новичка — это СТРУКТУРА README, а не набор слов где-то в тексте.

    Прежний lint искал подстроки по всему файлу: упоминание «стоимость» в случайном
    предложении закрывало требование «раздел про стоимость владения». Теперь якорь —
    заголовок, то есть то, что ученик реально видит в оглавлении.
    """
    headings = _readme_headings()
    required = {
        "что это": "что это и зачем",
        "стои": "стоимость владения",
        "пре-флайт": "шаг −1: что нельзя сделать скриптом",
        "быстрый старт": "установка одной командой",
        "папка-мозг": "модель мозга и шаблон",
        "добавить агента": "как добавить второго агента",
        "команды": "команды и кнопки",
        "безопасность": "два контура и allow-list",
        "диагностика": "бот молчит",
        "обновление": "обновление и откат",
        "персональные данные": "152-ФЗ и retention",
    }
    missing = [why for anchor, why in required.items() if not any(anchor in h for h in headings)]
    assert not missing, f"в README нет разделов-заголовков: {missing}"


def test_docs_reference_only_files_that_exist():
    """S4: файл или скрипт, названный в документе, обязан существовать в репо.

    Мёртвая ссылка в инструкции стоит ученику того же, что выдуманный флаг: он идёт по
    указанному пути и не находит ничего.
    """
    problems: list[str] = []
    for doc in _shipped_docs():
        for lineno, line in enumerate(doc.read_text().splitlines(), 1):
            candidates = [m.group(1) for m in re.finditer(rf"{RUNTIME_PREFIX}([\w./-]+)", line)]
            candidates += [
                m.group(1)
                for m in re.finditer(r"`([\w-]+/[\w./-]*)`", line)
                if m.group(1).split("/")[0] in REPO_DIRS
            ]
            for rel in candidates:
                rel = rel.rstrip("./")
                if not (REPO / rel).exists():
                    problems.append(f"{doc.relative_to(REPO)}:{lineno} → {rel}")
    assert not problems, "документ ссылается на то, чего в репо нет:\n" + "\n".join(problems)


def test_readme_installs_gh_only_after_adding_githubs_repository():
    """ADV-02: `apt-get install -y gh` в вакууме невыполним — в репозиториях Ubuntu нет `gh`.

    Ученик выполняет команду буквально, получает `Unable to locate package` и остаётся без
    движка. Значит установке `gh` обязан предшествовать официальный репозиторий GitHub
    (keyring + sources.list.d) — проверяем именно порядок, а не наличие слов.
    """
    text = README.read_text()
    hits = list(re.finditer(r"apt(?:-get)?\s+install\s+(?:-y\s+)?gh\b", text))
    for m in hits:
        assert "cli.github.com/packages" in text[: m.start()], (
            "перед `apt-get install gh` в README должен быть подключён репозиторий GitHub: "
            "иначе apt честно скажет «Unable to locate package gh»"
        )


def test_readme_make_targets_exist():
    """S4: `make test` из README обязан существовать в Makefile."""
    makefile = (REPO / "Makefile").read_text()
    targets = set(re.findall(r"^([a-z][a-z-]*):", makefile, re.M))
    assert targets, "не смог вычитать цели Makefile — тест бесполезен, поправь разбор"
    used = set(re.findall(r"make ([a-z][a-z-]*)", README.read_text()))
    assert used <= targets, f"README зовёт несуществующие цели make: {sorted(used - targets)}"


def test_readme_authorizes_github_before_cloning():
    """ADV-02: курица-яйцо — `git clone` приватного репо стоит ПЕРЕД входом в GitHub.

    Помощь со входом живёт внутри установщика, которого на диске ещё нет: ученик стоит на
    первой же команде README. Значит порядок в README обязан быть обратным: сначала
    авторизация, потом клон.
    """
    lines = README.read_text().splitlines()
    clone = next((i for i, ln in enumerate(lines) if "git clone" in ln), None)
    assert clone is not None, "быстрый старт без git clone — проверь тест"
    auth = next(
        (
            i
            for i, ln in enumerate(lines)
            if re.search(r"gh auth login|fine-grained|Personal access", ln)
        ),
        None,
    )
    assert auth is not None, "README обязан объяснить вход в приватный репо"
    assert auth < clone, "авторизация в GitHub описана ПОСЛЕ клона — ученик встанет на клоне"


def test_docs_do_not_claim_that_shipped_files_are_missing():
    """M-09, обратная сторона мёртвой ссылки: «этого ещё нет» про файл, который уже в репо.

    Ученик читает «update.sh появится позже» — и не обновляется, хотя скрипт лежит рядом.
    Такие фразы живут ровно до того релиза, в котором файл появился, и потом становятся ложью.
    """
    stale = r"(ещё нет|пока нет|его нет|ещё не появил|появится (?:позже|на этапе))"
    problems: list[str] = []
    for doc in _shipped_docs():
        for lineno, line in enumerate(doc.read_text().splitlines(), 1):
            for m in re.finditer(rf"([\w./-]+\.(?:sh|py|md|yaml))[^.!?\n]{{0,60}}?{stale}", line):
                name = m.group(1).lstrip("/").removeprefix("opt/unpacker/")
                if (REPO / name).exists() or (REPO / "deploy" / name).exists():
                    problems.append(f"{doc.relative_to(REPO)}:{lineno} → {m.group(0)[:80]}")
    assert not problems, "документ объявляет отсутствующим то, что уже в репо:\n" + "\n".join(
        problems
    )
