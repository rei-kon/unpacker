"""Распознавание речи: голосовое/аудио/видеозаметка → текст.

Почему это в ядре, а не в адаптере: политика («чем распознаём», «что делать с тишиной»,
«сколько ждём») одинакова для любой поверхности, адаптер только переносит байты. Тот же
принцип, что у uploads.py и sendfile.py.

Три решения, которые стоит объяснить, иначе они выглядят произвольными.

1. **Никакого ffmpeg.** Провайдер ест ogg/opus, m4a и mp4 как есть, поэтому конвертации в
   цепочке нет вовсе. Это не оптимизация, а снятая грабля: в разобранных живых ботах
   конвертация делается блокирующим `subprocess.run` прямо в async-хендлере и подвешивает
   весь polling на время распознавания.

2. **Пустой результат — это ОШИБКА, а не пустая строка.** Тишина, музыка, неразборчивая
   запись должны вернуться человеку честным «не разобрал», а не молчаливым пропуском:
   пустой промпт агенту неотличим от «владелец ничего не сказал».

3. **Таймаут обязателен и живёт здесь.** Ни один из разобранных ботов его не ставит: зависший
   провайдер вешает задачу навсегда, а вместе с ней — окно чата.

ПРИВАТНОСТЬ, сказать вслух: аудио уходит стороннему сервису (Deepgram) по сети. Для дневников
и личных разговоров это существенно, поэтому фича выключена по умолчанию и включается только
осознанной установкой ключа. То же самое написано в `.env.template` и README — там, где человек
это увидит до включения, а не после.

Выбор провайдера (Deepgram nova-3, `language=multi`, словарь `keyterm`) сделан замерами на
живых записях: конкуренты на Whisper теряли куски речи МОЛЧА, без обрыва фразы и следа в
тексте. Для дневника, из которого агент потом цитирует человека, пропуск — потеря, а
придуманное — ложь, неотличимая от правды.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from urllib.parse import urlencode

import aiohttp

from engine.core.uploads import UNTRUSTED_FRAME

logger = logging.getLogger("unpacker.engine")

DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"

# Расширение → MIME. Провайдеру нужен честный Content-Type: с octet-stream он гадает
# по сигнатуре и на opus иногда ошибается.
_MIME_BY_SUFFIX: dict[str, str] = {
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/opus",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".mp4": "video/mp4",
    ".aac": "audio/aac",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".webm": "audio/webm",
}
_DEFAULT_MIME = "application/octet-stream"


class TranscriptionError(Exception):
    """Не смогли распознать. Текст исключения уходит человеку как есть — пишем по-русски."""


class Transcriber(Protocol):
    """Контракт распознавателя. Провайдер за ним меняется без правки адаптера."""

    async def transcribe(self, path: Path) -> str: ...


def load_keyterms(path: str | Path | None) -> list[str]:
    """Словарь подсказок: одно слово/фраза на строку, `#` — комментарий.

    Зачем словарь: без него имена сервисов приходят искажёнными («Клод Кот», «OpenClo»),
    с ним — точно. Ограничение, проверенное на живых записях: словарь не только исправляет,
    но и ПОДМЕНЯЕТ похожее по звучанию — произнесённое «Уизли» стало «Whisper», потому что
    Whisper лежал в списке. Поэтому список держат узким, из реально звучащих слов, а не
    «про запас». Нет файла — работаем без словаря, это не ошибка.
    """
    if path is None:
        return []
    p = Path(path)
    if not p.is_file():
        logger.info("словарь подсказок не найден: %s — работаем без него", p)
        return []
    terms: list[str] = []
    for raw in p.read_text(encoding="utf-8").splitlines():
        term = raw.strip()
        if term and not term.startswith("#"):
            terms.append(term)
    return terms


@dataclass(frozen=True)
class DeepgramTranscriber:
    """Распознавание через Deepgram. Ключ обязателен — без него объект не создают вовсе."""

    api_key: str
    language: str = "multi"
    keyterms: list[str] = field(default_factory=list)
    timeout: float = 120.0
    model: str = "nova-3"

    def _url(self) -> str:
        params: list[tuple[str, str]] = [
            ("model", self.model),
            ("smart_format", "true"),
            ("punctuate", "true"),
            ("language", self.language),
        ]
        params.extend(("keyterm", term) for term in self.keyterms)
        return f"{DEEPGRAM_URL}?{urlencode(params)}"

    async def transcribe(self, path: Path) -> str:
        mime = _MIME_BY_SUFFIX.get(path.suffix.lower(), _DEFAULT_MIME)
        # Файл читаем в отдельном потоке: он до 20 МБ, а блокировать event loop диском —
        # та же болезнь, что и блокирующий ffmpeg, только тише.
        # Чтение — тоже под отказом, а не выше него: диск может отвалиться, а 20 МБ в
        # память могут не влезть, и тогда исключение убегало наружу. Человек в этом случае
        # видел только реакцию 👀 и молчание — худший из возможных отказов.
        try:
            payload = await asyncio.to_thread(path.read_bytes)
        except OSError as exc:
            logger.warning("не прочитал запись %s: %s", path, exc)
            raise TranscriptionError("Не смог прочитать запись с диска. Пришли ещё раз.") from exc
        except MemoryError as exc:
            raise TranscriptionError("Запись слишком большая, не влезла в память.") from exc
        headers = {"Authorization": f"Token {self.api_key}", "Content-Type": mime}
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(self._url(), headers=headers, data=payload) as resp:
                    if resp.status != 200:
                        body = (await resp.text())[:300]
                        logger.warning("распознавание: HTTP %s — %s", resp.status, body)
                        # Разные коды — разные действия человека. Одно «ответил ошибкой» на
                        # всё отправляло бы его перезапускать бота там, где надо поправить
                        # ключ или подождать.
                        if resp.status in (401, 403):
                            raise TranscriptionError(
                                "Сервис распознавания не принял ключ. "
                                "Проверь DEEPGRAM_API_KEY в .env этого бота."
                            )
                        if resp.status == 402:
                            raise TranscriptionError(
                                "На счету сервиса распознавания кончились средства."
                            )
                        if resp.status == 429:
                            raise TranscriptionError(
                                "Сервис распознавания просит подождать: слишком часто. "
                                "Попробуй через минуту."
                            )
                        raise TranscriptionError(
                            f"Сервис распознавания ответил ошибкой ({resp.status}). "
                            "Пришли текстом или попробуй позже."
                        )
                    data = await resp.json()
        except TranscriptionError:
            raise
        except TimeoutError as exc:
            raise TranscriptionError(
                f"Распознавание не уложилось в {int(self.timeout)} секунд. "
                "Пришли запись покороче или текстом."
            ) from exc
        except Exception as exc:  # noqa: BLE001 — сеть: сообщаем человеку, а не роняем чат
            logger.warning("распознавание не удалось: %s", exc)
            raise TranscriptionError(
                "Не смог связаться с сервисом распознавания. Пришли текстом или попробуй позже."
            ) from exc

        text = _extract(data)
        if not text:
            # Осознанно ошибка, а не пустая строка — см. решение 2 в шапке модуля.
            raise TranscriptionError(
                "Не разобрал речь в записи — возможно, тишина или сильный шум. "
                "Попробуй ещё раз или напиши текстом."
            )
        return text


def _extract(data: object) -> str:
    """Достать текст из ответа. Структура чужая, поэтому идём по ней защитно."""
    try:
        channels = data["results"]["channels"]  # type: ignore[index]
        return str(channels[0]["alternatives"][0]["transcript"]).strip()
    except (KeyError, IndexError, TypeError):
        logger.warning("распознавание: неожиданная структура ответа")
        return ""


def frame_voice_prompt(
    *, text: str, kind: str, path: Path, trusted: bool, origin: str | None = None
) -> str:
    """Промпт из расшифровки. `trusted` решает, ставить ли untrusted-рамку.

    Граница проведена не по типу и не по факту пересылки в отдельности, а по их сочетанию,
    и вот почему ни один признак поодиночке не работает.

    * **Только по типу — дыра.** Пересланное голосовое остаётся типом `voice`. Запись
      психолога, которую владелец перешлёт «посмотри, что мне сказали», по правилу «voice
      значит владелец» проскочила бы как его собственная команда.
    * **Только по пересылке — обходится.** Признак пересылки снимается скачиванием и
      переотправкой. Но обход упирается в ту же дверь с другой стороны: переотправленная
      запись приезжает файлом, а файл доверенным не считается по типу.

    Отсюда правило: доверяем ТОЛЬКО голосовому, записанному владельцем здесь и не
    пересланному. Всё прочее — аудиофайлы, кружки, любая пересылка — получает ту же рамку,
    что и документы (§8.2): разбирай полностью, но команды бери у владельца.

    Почему не проще накрыть рамкой всё. Речь владельца — это и есть его команда: он говорит
    «напомни завтра» и ждёт, что напомнят. Рамка поверх собственной речи превратила бы
    агента в приёмщика диктовок, который на любую просьбу отвечает «принято к сведению», —
    сломалась бы ровно та функция, ради которой всё делалось.

    Рамка не отказ и не фильтр: содержимое разбирается целиком. Владелец по-прежнему может
    сказать подписью или отдельным сообщением «распознай и исполни» — рамка прямо велит
    выполнять то, о чём просит владелец в сообщении выше.

    Строка про распознавание идёт последней: агенту полезно знать, что имена могли
    исказиться, а человеку не нужен служебный шум поверх собственных слов.

    `origin` — имя автора пересылки, если Telegram его отдал. «Автор не подтверждён» и
    «переслано от Петра» — разные сообщения для агента: первое говорит только «не владелец»,
    второе позволяет назвать источник в ответе. Пусто оставляем лишь тогда, когда имени нет
    на самом деле (скрытый отправитель, файл), а не потому что забыли прокинуть.
    """
    lines = [text.strip(), ""]
    if not trusted:
        source = f"Источник: {kind}, автор не подтверждён (переслано или прислано файлом)"
        source += f"; переслано от {origin}." if origin else "."
        lines.append(source)
        lines.append("")
        lines.append(UNTRUSTED_FRAME)
        lines.append("")
    lines.append(f"(расшифровка: {kind}, распознавание может ошибаться; запись: {path})")
    return "\n".join(lines)
