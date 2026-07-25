"""ButtonRegistry — инстансные кнопки-триггеры в рантайме (§9).

Источник правды — `<instance_dir>/buttons.yaml` (контракт срезов). Реестр перечитывает файл,
когда тот поменялся: «наращивать штуки» (§9) должно работать дописыванием строки в yaml, а не
рестартом бота — ученику иначе придётся лезть в systemctl из-за одной кнопки.

Живучесть важнее свежести: если после правки файл стал битым (или его снесли), держим ПОСЛЕДНИЙ
рабочий набор и пишем warning в лог. Опечатка владельца не должна обезоруживать живого бота.
"""

from __future__ import annotations

import logging
from pathlib import Path

from engine.core.brain import ButtonSpec, PassportError, load_instance_buttons

logger = logging.getLogger("unpacker.engine")


class ButtonRegistry:
    def __init__(self, path: str | Path, *, enabled: bool = True):
        self._path = Path(path)
        self._enabled = enabled
        self._buttons: list[ButtonSpec] = []
        # Отпечаток файла: (mtime_ns, size, inode). Только mtime мало — правка «в ту же
        # наносекунду» бывает при копировании файла целиком (cp сохраняет mtime).
        self._stamp: tuple[int, int, int] | None = None

    @property
    def enabled(self) -> bool:
        """Флаг .env живёт здесь одним местом: адаптер спрашивает, а не хранит копию флага.

        Разница между «выключено» и «в файле нет кнопок» важна для UX: выключено — ряда нет
        совсем; кнопок нет — системный ряд (Проекты/Статус/Стоп) всё равно полезен.
        """
        return self._enabled

    def get(self) -> list[ButtonSpec]:
        """Актуальные кнопки. Перечитывает файл при изменении, ошибки не пробрасывает."""
        if not self._enabled:
            return []
        stamp = self._read_stamp()
        if stamp is None:
            # файла нет: на первом вызове это честный «кнопок нет», после — «его снесли,
            # держим прошлый набор» (нельзя молча раздеть работающий бот)
            return self._buttons
        if stamp != self._stamp:
            self._stamp = stamp
            try:
                self._buttons = load_instance_buttons(self._path)
            except PassportError as exc:
                logger.warning("buttons.yaml битый, держу прошлый набор: %s", exc)
        return self._buttons

    def resolve(self, index: int) -> ButtonSpec | None:
        """Кнопка по индексу из callback. Вне диапазона → None (кнопка на старом сообщении)."""
        buttons = self.get()
        if 0 <= index < len(buttons):
            return buttons[index]
        return None

    def _read_stamp(self) -> tuple[int, int, int] | None:
        try:
            st = self._path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size, st.st_ino)
