"""Велс-трюк §9: черновик «печатает…» — псевдо-стриминг текста в Telegram.

Настоящего токен-стрима в Bot API нет; индустриальный приём (Vels Claude, claudeclaw) —
одно сообщение-черновик, которое редактируется по мере генерации, не чаще interval
(флуд-лимиты edit, §9 конституции).

Финал — это ПОСЛЕДНИЙ edit того же черновика (`finalize`), а не новое сообщение. Так одно
сообщение живёт от первой буквы до готового ответа, и — важнее косметики — ответ физически
не может пропасть: раньше `finish()` удалял черновик в `finally`, а финал мог не прийти
(упал SDK, протух токен, поток кончился без Final, отправка провалилась дважды) — человек
читал текст своими глазами, а в чате не оставалось ни строчки. Не-ok исход дописывает
пометку к показанному тексту (`abort`), а не стирает его.

Приём дельт — синхронный и дешёвый (on_event зовётся из ядра под session_lock);
вся сеть — в фоновой задаче.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup

from engine.core.formatting import to_telegram_markdown
from engine.core.sendfile import hide_partial_marker
from engine.core.streaming import TELEGRAM_LIMIT, split_message, tail_by_units

logger = logging.getLogger("unpacker.engine")

_CURSOR = "▌"
_ELLIPSIS = "…"
# Черновик держим заметно ниже лимита 4096: финал всё равно придёт нарезкой,
# а «печатание» простыни целиком не нужно — при переполнении показываем свежий хвост.
_DEFAULT_MAX_UNITS = 3600
# Telegram на повторный edit тем же текстом отвечает 400 — это УСПЕХ (нужный текст уже
# висит), а не повод удалять сообщение и слать заново.
_NOT_MODIFIED = "message is not modified"


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

    async def _stop(self) -> None:
        """Остановить цикл и дождаться сетевой операции в полёте.

        Общее начало для всех трёх финалов. Дождаться `_inflight` обязательно: send мог
        уйти, а `_message_id` ещё не записаться — тогда черновик остался бы сиротой, и
        финальный edit ушёл бы в никуда.
        """
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            # CancelledError — BaseException: глушим и её (штатная отмена), и Exception
            # (задача умерла своим исключением) — остановка не смеет ронять доставку финала.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        if self._inflight is not None and not self._inflight.done():
            with contextlib.suppress(Exception):  # дождаться send в полёте → узнать message_id
                await self._inflight

    async def finish(self) -> None:
        """Остановить цикл и убрать черновик.

        Путь «доставлять через черновик нечего» (например, в окне нет проекта): текста
        человеку не будет вовсе, и висящий обрубок хуже пустоты. На обычном пути ответа
        зовётся `finalize`/`abort` — там черновик остаётся жить.

        Никогда не бросает: зовётся перед отправкой ответа — исключение отсюда потеряло бы
        уже сгенерированный текст.
        """
        await self._stop()
        if self._message_id is not None:
            try:
                await self._bot.delete_message(self._chat_id, self._message_id)
            except Exception:  # noqa: BLE001 — не удалился — не страшно, ответ его перекроет
                logger.debug("черновик не удалился", exc_info=True)
            self._message_id = None

    async def finalize(self, text: str, *, markup: InlineKeyboardMarkup | None = None) -> bool:
        """Финал = последний edit черновика. True — первый кусок ответа доставлен.

        Каскад: MarkdownV2 → плейн → delete+send (честный откат на случай, когда человек
        удалил черновик руками). False — доставлять нечем (дельт не было) или не смогли
        совсем: тогда бот отправляет ответ обычным путём, ничего не потеряв.

        `text` приходит уже нарезанным и очищенным от `[SEND_FILE:]`: вырезать маркер ПОСЛЕ
        финального edit поздно — в отредактированном черновике остался бы путь на сервере.
        """
        await self._stop()
        if self._message_id is None:
            return False
        md = to_telegram_markdown(text)
        if md is not None and await self._try_edit(md, parse_mode="MarkdownV2", markup=markup):
            return True
        if await self._try_edit(text, markup=markup):
            return True
        return await self._resend(text, markup)

    async def abort(self, note: str, *, markup: InlineKeyboardMarkup | None = None) -> bool:
        """Не-ok исход: снять курсор и ДОПИСАТЬ пометку к показанному тексту.

        True — пометка уехала в черновик, боту слать нечего. False — черновика нет или edit
        не прошёл: пусть бот скажет то же самое обычным сообщением.

        Почему не удалять: человек уже прочитал часть ответа. Стереть её и прислать сухое
        «⚠️ сбой» — значит выбросить единственное, что от ответа осталось.
        """
        await self._stop()
        if self._message_id is None:
            return False
        body = _strip_tail(self._shown or hide_partial_marker("".join(self._buf)))
        text = f"{body}\n\n{note}" if body else note
        return await self._try_edit(_fit_limit(text), markup=markup)

    async def _try_edit(
        self,
        text: str,
        *,
        parse_mode: str | None = None,
        markup: InlineKeyboardMarkup | None = None,
    ) -> bool:
        try:
            await self._bot.edit_message_text(
                text,
                chat_id=self._chat_id,
                message_id=self._message_id,
                parse_mode=parse_mode,
                reply_markup=markup,
            )
        except TelegramBadRequest as exc:
            if _NOT_MODIFIED in str(exc):
                self._shown = text
                return True
            logger.warning("финальный edit отвергнут Telegram (%s)", parse_mode or "плейн")
            return False
        except Exception:  # noqa: BLE001 — сеть моргнула: пробуем следующую ступень каскада
            logger.warning("финальный edit не прошёл (%s)", parse_mode or "плейн", exc_info=True)
            return False
        self._shown = text
        return True

    async def _resend(self, text: str, markup: InlineKeyboardMarkup | None) -> bool:
        """Последняя ступень каскада: черновика в чате больше нет (удалён руками) — шлём заново."""
        try:
            await self._bot.delete_message(self._chat_id, self._message_id)
        except Exception:  # noqa: BLE001 — скорее всего его и нет; не мешает отправке
            logger.debug("черновик не удалился перед пересылкой финала", exc_info=True)
        self._message_id = None
        try:
            await self._bot.send_message(
                self._chat_id, text, message_thread_id=self._thread_id, reply_markup=markup
            )
        except Exception:  # noqa: BLE001 — не смогли: пусть бот доставит своим путём
            logger.warning("финал не ушёл новым сообщением", exc_info=True)
            return False
        return True

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


def _strip_tail(shown: str) -> str:
    """Убрать служебный хвост черновика — курсор или «…» переполнения."""
    return shown.removesuffix(_CURSOR).removesuffix(_ELLIPSIS).rstrip()


def _fit_limit(text: str) -> str:
    """Ужать под лимит Telegram, сохранив ХВОСТ текста.

    Режем начало, а не конец: в хвосте стоит пометка, объясняющая, что случилось. Потерять
    её — значит вернуть человека к молчанию, от которого мы и уходим.
    """
    if len(split_message(text, TELEGRAM_LIMIT)) == 1:
        return text
    return _ELLIPSIS + tail_by_units(text, TELEGRAM_LIMIT - len(_ELLIPSIS))
