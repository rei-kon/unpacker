"""Идемпотентный сид дефолтного проекта→мозг: `python -m engine.seed`.

deploy.sh зовёт это перед стартом бота. Роутер (adapters/telegram/router.py) требует
активный проект в БД — без него первое сообщение падает NoProjectError. Сид создаёт
строку project(slug→brain_path) один раз; повторный деплой не дублирует и не роняет.

Права/валидация — в коде (§7.5 «LLM предлагает — код исполняет»): slug проверяет
store.ProjectStore.create (^[a-z0-9-]+$), brain_path приходит абсолютным от deploy.sh.
"""

from __future__ import annotations

import argparse
import sys

from engine.core.store import Store


def ensure_project(
    store: Store, *, slug: str, name: str, brain_path: str, model: str | None = None
) -> str:
    """Гарантировать активный проект. Возвращает 'created' | 'exists' | 'exists-divergent'.

    Идемпотентно: строку создаём только если её нет. store.create — INSERT-only и НЕ обновляет
    brain_path существующего проекта (это cwd резюма — молча двигать нельзя). Поэтому если проект
    уже есть, но с ДРУГИМ brain_path, возвращаем 'exists-divergent' — вызывающий громко
    предупреждает, иначе оператор чинит --brain, видит «готово», а бот держит старый мозг.

    slug валидируется внутри store.projects.create (ValueError на мусоре) — здесь не дублируем.
    """
    existing = store.projects.get(slug)
    if existing is not None:
        return "exists" if existing.brain_path == brain_path else "exists-divergent"
    store.projects.create(slug=slug, name=name, brain_path=brain_path, default_model=model)
    return "created"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m engine.seed",
        description="Идемпотентно завести дефолтный проект→мозг в state.db движка.",
    )
    parser.add_argument("--db", required=True, help="путь к state.db инстанса")
    parser.add_argument("--slug", required=True, help="slug проекта (^[a-z0-9-]+$)")
    parser.add_argument("--name", required=True, help="человеческое имя проекта")
    parser.add_argument("--brain", required=True, help="абсолютный путь к папке-мозгу")
    parser.add_argument("--model", default=None, help="дефолтная модель (опц)")
    args = parser.parse_args(argv)

    store = Store(args.db)
    try:
        result = ensure_project(
            store, slug=args.slug, name=args.name, brain_path=args.brain, model=args.model
        )
    except ValueError as exc:
        print(f"seed: отказ — {exc}", file=sys.stderr)
        return 2
    finally:
        store.close()
    print(f"seed: project {args.slug!r} -> {result} (brain={args.brain})")
    if result == "exists-divergent":
        print(
            f"seed: ⚠ проект {args.slug!r} уже существует с ДРУГИМ brain_path — "
            f"запрошенный {args.brain!r} НЕ применён (store не двигает cwd резюма). "
            f"Смени slug или почисти проект вручную.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
