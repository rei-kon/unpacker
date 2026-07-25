"""Мост «мозг → инстанс»: `python -m engine.brainkit <команда>` (§4).

Зовёт этот CLI deploy.sh при провижининге инстанса. Разделение обязанностей то же, что у
engine/seed.py: bash отвечает за каталоги и systemd, Python — за разбор данных и валидацию
(§7.5 «LLM предлагает — код исполняет»; здесь ещё и «bash не парсит YAML регулярками»).

Команды:
  export-buttons --brain <папка-мозга> --out <инстансный buttons.yaml>
      Копирует шаблон кнопок из `.brain.yaml` мозга в инстансный `buttons.yaml`.
      **Идемпотентно и бережно**: если out уже есть — НЕ трогает его (exit 0, печатает
      «уже есть»). Причина ровно та же, что у .env: владелец правит инстансную копию руками,
      и повторный деплой не имеет права стереть его кнопки. Перезалить из мозга можно только
      явным --force.

Коды возврата: 0 — сделано или «уже есть»; 2 — отказ (битый паспорт, нет мозга).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from engine.core.brain import PassportError, dump_buttons_yaml, load_passport

_MODE_BUTTONS = 0o644  # не секрет (в отличие от .env 600): владелец правит файл руками


def export_buttons(*, brain: Path, out: Path, force: bool = False) -> str:
    """Сделать инстансный buttons.yaml из паспорта мозга.

    Возвращает 'exists' | 'created' | 'created-empty'. Бросает PassportError на битом
    паспорте — вызывающий печатает человеческую строку и выходит с кодом 2.
    """
    if not brain.is_dir():
        raise PassportError(f"нет папки-мозга: {brain}")
    if out.exists() and not force:
        return "exists"

    passport = load_passport(brain)  # нет паспорта → None, это норма (§4)
    buttons = passport.buttons if passport is not None else []

    # Пишем через временный файл в том же каталоге + rename: прерванный деплой не оставляет
    # огрызок, который на следующем прогоне сочтётся за «уже есть» и заморозит пустые кнопки.
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(dump_buttons_yaml(buttons), encoding="utf-8")
    tmp.chmod(_MODE_BUTTONS)
    tmp.replace(out)
    return "created" if buttons else "created-empty"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m engine.brainkit",
        description="Мост «папка-мозг → инстанс»: перенос данных паспорта .brain.yaml.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser(
        "export-buttons",
        help="скопировать шаблон кнопок мозга в инстансный buttons.yaml (идемпотентно)",
    )
    export.add_argument("--brain", required=True, help="путь к папке-мозгу")
    export.add_argument("--out", required=True, help="путь к инстансному buttons.yaml")
    export.add_argument(
        "--force",
        action="store_true",
        help="перезалить из мозга, затерев правки владельца (по умолчанию НЕ трогаем)",
    )
    args = parser.parse_args(argv)

    out = Path(args.out)
    try:
        result = export_buttons(brain=Path(args.brain), out=out, force=args.force)
    except PassportError as exc:
        print(f"brainkit: отказ — {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"brainkit: отказ — не записать {out}: {exc}", file=sys.stderr)
        return 2

    if result == "exists":
        print(f"brainkit: {out} уже есть — не трогаю (правки владельца целы)")
    elif result == "created-empty":
        print(f"brainkit: {out} создан пустым — в мозге нет кнопок, допиши руками")
    else:
        print(f"brainkit: {out} создан из паспорта мозга")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
