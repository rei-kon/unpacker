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

from engine.core.brain import PassportError, load_passport

REPO = Path(__file__).resolve().parents[2]
README = REPO / "README.md"
BOT_PY = REPO / "engine" / "adapters" / "telegram" / "bot.py"
BRAINS = REPO / "brains"
EXAMPLES = REPO / "examples"

# Паспорта, которые мы ВЫДАЁМ ученику: шаблон, мозг Распаковщика и рабочий пример.
SHIPPED_PASSPORTS = (BRAINS / "_template", BRAINS / "unpacker", EXAMPLES / "assistant")


def _shipped_docs() -> list[Path]:
    """Документы, которые читает ученик: README + всё, что едет внутри мозгов и примеров."""
    docs = [README]
    for root in (BRAINS, EXAMPLES):
        docs += [p for p in root.rglob("*.md") if p.is_file()]
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
