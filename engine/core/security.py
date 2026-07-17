"""Гейт доступа: бот отвечает только разрешённым user ID и только в личке.

Fail-closed: неизвестный/пустой/None/не-личный чат → запрет.
"""

from __future__ import annotations


def is_private_chat(chat_type: str | None) -> bool:
    """Только личка. Группа/супергруппа/канал → запрет (иначе вывод офиса утечёт другим)."""
    return chat_type == "private"


class AllowList:
    def __init__(self, allowed_ids: list[int]):
        # храним только int — строки/None/bool не должны случайно совпасть
        self._allowed = {i for i in allowed_ids if isinstance(i, int) and not isinstance(i, bool)}

    def is_allowed(self, user_id: int | None) -> bool:
        if not isinstance(user_id, int) or isinstance(user_id, bool):
            return False
        return user_id in self._allowed

    def should_handle(self, user_id: int | None, chat_type: str | None) -> bool:
        """Полный гейт: разрешённый пользователь И личный чат."""
        return self.is_allowed(user_id) and is_private_chat(chat_type)
