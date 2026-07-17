"""C8 — видны ли топики личного чата aiogram'у. ПРОБА, не тест.

Почему проба, а не pytest: топик в личке нельзя создать программно. Нужен живой бот, у
которого в @BotFather включены «Topics in private chats», и живой человек, который напишет
ему в топик. Автотест, который зависит от человека с телефоном, — не автотест, и притворяться
не будем.

Проба печатает СЫРУЮ структуру апдейта. Нас интересует `message_thread_id`: на нём стоит
модель «топик = окно в сессию» (§5.2, биндинги) — главный способ переключать проекты в MVP.

Запуск (на VPS или на маке — Bot API одинаков):
    CANARY_BOT_TOKEN=<токен тестового бота> uv run python canaries/probe_c8_topics.py

Токен — только тестового бота. Живых ботов (director, icoach, gefest…) не трогаем.
"""
from __future__ import annotations

import asyncio
import os
import sys

from aiogram import Bot

WAIT_SECONDS = 60


async def main() -> int:
    token = os.environ.get("CANARY_BOT_TOKEN")
    if not token:
        print(
            "Нет CANARY_BOT_TOKEN.\n"
            "Нужен ТЕСТОВЫЙ бот от @BotFather с включёнными «Topics in private chats».\n"
            "Запуск: CANARY_BOT_TOKEN=<токен> uv run python canaries/probe_c8_topics.py",
            file=sys.stderr,
        )
        return 2

    bot = Bot(token=token)
    try:
        me = await bot.get_me()
        print(f"Бот: @{me.username} (id={me.id})")
        print(
            f"Напиши ему в личку — желательно в ТОПИК. Слушаю {WAIT_SECONDS}с…\n"
            "(нет топиков в личке → напиши обычным сообщением, сравним структуру)\n"
        )

        offset: int | None = None
        seen = 0
        deadline = asyncio.get_running_loop().time() + WAIT_SECONDS

        while asyncio.get_running_loop().time() < deadline:
            try:
                updates = await bot.get_updates(offset=offset, timeout=10)
            except Exception as exc:  # noqa: BLE001 — сеть моргнула, не повод падать
                print(f"  get_updates не сработал: {exc!r}")
                await asyncio.sleep(2)
                continue

            for update in updates:
                offset = update.update_id + 1
                message = update.message
                if message is None:
                    continue
                seen += 1
                print(
                    f"[апдейт {seen}] chat.type={message.chat.type!r} "
                    f"chat.id={message.chat.id} "
                    f"chat.is_forum={getattr(message.chat, 'is_forum', None)!r} "
                    f"message_thread_id={message.message_thread_id!r} "
                    f"is_topic_message={getattr(message, 'is_topic_message', None)!r} "
                    f"text={message.text!r}"
                )

        if seen == 0:
            print(
                "\nФАКТ: за отведённое время не пришло ни одного сообщения — "
                "проба не состоялась (это не «топиков нет», это «никто не написал»)."
            )
        else:
            print(
                f"\nФАКТ: получено апдейтов — {seen}. Смотри message_thread_id выше: "
                "непустой в личке → модель «топик = биндинг» (§5.2) работает."
            )
        return 0
    finally:
        await bot.session.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
