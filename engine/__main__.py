"""Точка входа движка: `python -m engine`. Читает .env/окружение → поднимает бота."""

from __future__ import annotations

import asyncio
import logging

from engine.core.config import Settings
from engine.runtime import build_bot


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    settings = Settings()  # type: ignore[call-arg]  # поля приходят из окружения/.env
    bot = build_bot(settings)
    asyncio.run(bot.run())


if __name__ == "__main__":
    main()
