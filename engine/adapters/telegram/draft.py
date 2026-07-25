"""Велс-трюк §9: черновик «печатает…» — псевдо-стриминг текста в Telegram.

Настоящего токен-стрима в Bot API нет; индустриальный приём (Vels Claude, claudeclaw) —
одно сообщение-черновик, которое редактируется по мере генерации, не чаще interval
(флуд-лимиты edit ≈ раз в 3с, §9 конституции). Финал черновиком НЕ является: finish()
удаляет черновик, финальный ответ уходит отдельным аккуратным сообщением (§5.3).

Приём дельт — синхронный и дешёвый (on_event зовётся из ядра под session_lock);
вся сеть — в фоновой задаче.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from engine.core.sendfile import hide_partial_marker
from engine.core.streaming import split_message

logger = logging.getLogger("unpacker.engine")

_CURSOR = "▌"
_ELLIPSIS = "…"
# Черновик держим заметно ниже лимита 4096: финал всё равно придёт нарезкой,
# а «печатание» простыни целиком не нужно — при переполнении замораживаемся.
_DEFAULT_MAX_UNITS = 3600


class DraftStreamer:
    def __init__(
        self,
        bot: Any,
        *,
        chat_id: int,
        thread_id: int | None,
        interval: float = 3.0,
        max_units: int = _DEFAULT_MAX_UNITS,
    ):
        self._bot = bot
        self._chat_id = chat_id
        self._thread_id = thread_id
        self._interval = interval
        self._max = max_units
        self._buf: list[str] = []
        self._dirty = False
        self._frozen = False  # текст перерос max_units — едиты прекращаем до reset
        self._shown: str | None = None  # что реально висит в TG (дедуп «not modified»)
        self._message_id: int | None = None
        self._task: asyncio.Task[None] | None = None
        # In-flight сетевая операция: cancel цикла НЕ должен ронять её посреди send —
        # иначе сообщение уже создано в TG, а _message_id не записан → черновик-сирота.
        self._inflight: asyncio.Task[None] | None = None
        self._stopped = False

    # ── sync-приём из on_event (лёгкий, без сети) ────────────────────────────

    def on_delta(self, text: str) -> None:
        if self._stopped or self._frozen:
            return
        self._buf.append(text)
        self._dirty = True
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    def on_reset(self) -> None:
        """Новое assistant-сообщение (TextStart): черновик начинается заново."""
        if self._stopped:
            return
        self._buf.clear()
        self._frozen = False
        self._dirty = True

    # ── завершение ───────────────────────────────────────────────────────────

    async def finish(self) -> None:
        """Остановить цикл и убрать черновик — финал придёт отдельным сообщением.

        Никогда не бросает: зовётся из finally перед отправкой финала (bot.py) —
        любое исключение отсюда потеряло бы успешно сгенерированный ответ.
        """
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            # CancelledError — BaseException: глушим и её (штатная отмена), и Exception
            # (задача умерла своим исключением) — finish не смеет ронять доставку финала.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        if self._inflight is not None and not self._inflight.done():
            with contextlib.suppress(Exception):  # дождаться send в полёте → узнать message_id
                await self._inflight
        if self._message_id is not None:
            try:
                await self._bot.delete_message(self._chat_id, self._message_id)
            except Exception:  # noqa: BLE001 — не удалился — не страшно, финал его перекроет
                logger.debug("черновик не удалился", exc_info=True)
            self._message_id = None

    # ── фоновая задача ───────────────────────────────────────────────────────

    async def _loop(self) -> None:
        while not self._stopped:
            if self._dirty:
                self._dirty = False
                await self._flush()
            await asyncio.sleep(self._interval)

    async def _flush(self) -> None:
        # Маркер `[SEND_FILE:]` из черновика убираем ВСЕГДА, включая недописанный хвост:
        # финал его вырежет, а до финала человек видел служебную строку с абсолютным путём
        # на сервере внутри — и через секунду она исчезала (residual-находка ревью).
        text = hide_partial_marker("".join(self._buf))
        if not text.strip():
            return
        first = split_message(text, self._max)[0]
        if len(first) < len(text):  # перерос лимит черновика — замораживаемся
            self._frozen = True
            shown = first + _ELLIPSIS
        else:
            shown = first + _CURSOR
        if shown == self._shown:
            return
        # Сеть — отдельной shield-задачей: cancel цикла (finish) не убьёт её посреди
        # send_message, поэтому _message_id гарантированно запишется и finish его удалит.
        self._inflight = asyncio.ensure_future(self._send_or_edit(shown))
        await asyncio.shield(self._inflight)

    async def _send_or_edit(self, shown: str) -> None:
        try:
            if self._message_id is None:
                msg = await self._bot.send_message(
                    self._chat_id, shown, message_thread_id=self._thread_id
                )
                self._message_id = msg.message_id
            else:
                await self._bot.edit_message_text(
                    shown, chat_id=self._chat_id, message_id=self._message_id
                )
            self._shown = shown
        except Exception:  # noqa: BLE001 — черновик косметический: сбой сети не рвёт генерацию
            self._dirty = True  # не терять кусок: следующий тик цикла ретрайнет (троттлинг тот же)
            logger.debug("черновик не отправился/не отредактировался", exc_info=True)
