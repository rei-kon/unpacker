"""AgentCore §5.1 + §5.4 — фасад между поверхностями и пулом клиентов.

Одна точка, куда приходит сообщение из любой поверхности (ТГ, веб). Ядро смотрит в Store:
какой проект (папка-мозг) и какая сессия — берёт cwd из projects.brain_path (C2b), resume из
claude_session_id, продолжает диалог.

Два плана разведены (§5.1):
  • data-plane — `ask()` под per-session lock: чтение resume И запись claude_session_id —
    ВНУТРИ lock (иначе два окна форкают id: снимок resume до lock теряет контекст, запись
    вне lock даёт out-of-order — находки ревью B);
  • control-plane — `interrupt()` мимо lock по живой ссылке.
Порядок ресурсов: session_lock → lease (connect ВНЕ семафора) → semaphore (вокруг генерации).

Контракт ошибок (§5.4) — по структуре, не по regex:
  • ok           → отдать текст;
  • max_turns    → вернуть частичный текст + пометку outcome (не ошибка, D предложит продолжить);
  • exec_error   → перезапуск клиента (evict) + ОДНА повторная попытка; второй фейл → outcome;
  • auth_error   → health degraded + алерт владельцу (протух OAuth, риск #13), evict;
  • rate_limited / overloaded → ОДИН повтор, но с паузой: мгновенный влетает в тот же лимит;
  • resume_error → ОДИН повтор с resume=None + честная пометка, что диалог начат заново;
  • прочее исключение → drop клиента, вернуть outcome (D покажет сообщение, чат не залипает).

Два сквозных правила, которые дороже любой ветки выше:
  • ЛЮБОЙ финал с is_error означает, что SDK уже завершил CLI-подпроцесс, — клиента вон,
    иначе следующее сообщение человека уходит в труп;
  • session_id, который успел приехать (даже в оборванном потоке), сохраняется всегда:
    сессия CLI на диске уже создана, и потерять её handle = молча потерять кусок истории.
ask НЕ бросает на ошибках SDK — возвращает AskResult(text, outcome); бросает только LookupError
(программная ошибка — нет сессии). События verbose 1 отдаются через on_event; рендеринг — Срез D.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from engine.core.errors import Outcome, classify_exception
from engine.core.events import Event, Final, stream_events
from engine.core.health import HealthMarker
from engine.core.pool import ClientPool
from engine.core.store import Store

logger = logging.getLogger("unpacker.engine")


class OptionsBuilder(Protocol):
    """Сборщик ClaudeAgentOptions. Protocol, а не `Callable[..., Any]`: три ключевых
    аргумента — контракт между ядром и runtime, и опечатка в имени должна ловиться mypy,
    а не молчаливым TypeError на живом боте."""

    def __call__(self, *, cwd: str, resume: str | None, model: str | None) -> Any: ...


# on_alert и on_event ДОЛЖНЫ быть неблокирующими (fire-and-forget): вызываются синхронно в
# event loop под session_lock. Тяжёлую отправку (Telegram) адаптер уводит в create_task.
AlertFn = Callable[[str], Any]
EventFn = Callable[[Event], Any]
SleepFn = Callable[[float], Any]
_QUERY_TIMEOUT = 60.0
# Пауза перед повтором на лимите/перегрузе. Мгновенный повтор влетает в тот же лимит —
# он не «ретрай», а второй счёт за ту же ошибку.
_RETRY_BACKOFF = 3.0
_RESUME_LOST_NOTE = "ℹ️ Прошлый контекст не восстановился — продолжаю с чистого листа."
# Исходы, на которых повтор имеет смысл только с паузой (проблема на стороне провайдера).
_BACKOFF_KINDS = ("rate_limited", "overloaded")
# Исходы, которые заведомо НЕ про мёртвый resume: чужой лимит и протухший токен убивают
# заход одинаково рано, но история диалога здесь ни при чём (см. _resume_looks_dead).
_NOT_RESUME_FAULT = ("auth_error", *_BACKOFF_KINDS)


def detect_ram_bytes() -> int:
    """Физическая память машины (Linux и macOS). Потолок пула считается отсюда (§5.1)."""
    import os

    try:
        return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError, AttributeError):
        return 4 * 1024**3


class _GenerationFailed(Exception):
    """Внутренняя обёртка сбоя генерации: причина + улики потока.

    Нужна затем, что «упало» — слишком мало для решения. `session_id` (успел ли SDK назвать
    сессию) и `progressed` (были ли события до падения) отличают мёртвый resume от обрыва
    посреди живого ответа, а это ровно те два случая, где поведение обязано быть разным.
    """

    def __init__(self, cause: BaseException, *, session_id: str | None, progressed: bool):
        super().__init__(str(cause))
        self.cause = cause
        self.session_id = session_id
        self.progressed = progressed


def _resume_looks_dead(
    outcome: Outcome, resume: str | None, progressed: bool, cause: BaseException | None = None
) -> bool:
    """Похоже ли, что заход упал именно из-за непонятого `--resume`.

    Два признака. Явный: CLI прямым текстом сказал «No conversation found …» (outcome
    resume_error). Косвенный: заход с непустым resume умер, не отдав НИ ОДНОГО события —
    так выглядит клиент, который не поднялся вовсе. Если поток успел что-то отдать, resume
    заведомо жив, и сбрасывать его нельзя: это стоило бы человеку истории диалога.

    Косвенный признак — улика, а не приговор, и у него есть двойники: перегруз провайдера
    (429/529 прилетает и исключением — баг SDK #812) и таймаут ответа тоже убивают заход
    до первого события. Форма одна, причина разная, а цена ошибки несимметрична: за чужой
    лимит человек заплатил бы всей историей диалога. Поэтому такие исходы из эвристики
    исключены — по ним чинят паузой, а не забвением.
    """
    if not resume or outcome.kind in _NOT_RESUME_FAULT:
        return False
    if isinstance(cause, TimeoutError):
        return False
    return outcome.kind == "resume_error" or not progressed


@dataclass(frozen=True)
class AskResult:
    text: str
    outcome: Outcome
    # Пометка о том, что случилось по дороге к ответу (например «прошлый контекст не
    # восстановился»). Ответ при этом штатный — врать про ошибку нельзя, но и молчать о
    # потерянной истории тоже: человек должен понимать, почему агент «забыл» разговор.
    note: str = ""


class AgentCore:
    def __init__(
        self,
        *,
        store: Store,
        pool: ClientPool,
        options_builder: OptionsBuilder,
        response_timeout: float,
        query_timeout: float = _QUERY_TIMEOUT,
        health: HealthMarker | None = None,
        on_alert: AlertFn | None = None,
        retry_backoff: float = _RETRY_BACKOFF,
        sleep: SleepFn = asyncio.sleep,
    ):
        self._store = store
        self._pool = pool
        self._build_options = options_builder
        self._response_timeout = response_timeout
        self._query_timeout = query_timeout
        self._health = health
        self._on_alert = on_alert
        self._retry_backoff = retry_backoff
        self._sleep = sleep  # инъекция ради тестов: пауза не должна стоить тесту секунд

    async def ask(self, session_id: str, prompt: str, on_event: EventFn | None = None) -> AskResult:
        """Обработать сообщение сессии. Возвращает AskResult(text, outcome) — не бросает на
        ошибках SDK (чат не должен залипать), только на отсутствии сессии (LookupError)."""
        async with self._pool.session_lock(session_id):
            session = self._store.sessions.get(session_id)
            if session is None:
                raise LookupError(f"нет сессии: {session_id!r}")
            project = self._store.projects.get(session.project_slug)
            if project is None:
                raise LookupError(f"нет проекта сессии: {session.project_slug!r}")

            resume = session.claude_session_id
            options = self._build_options(
                cwd=project.brain_path,
                resume=resume,
                model=session.model,
            )

            note = ""
            last: Final | None = None
            for attempt in (0, 1):
                try:
                    last = await self._generate(
                        session_id, options, prompt, on_event, fingerprint=session.model
                    )
                except asyncio.CancelledError:
                    # отмена (shutdown/таймаут сверху) — НЕ глотаем: evict и пробрасываем,
                    # иначе graceful shutdown ломается (отмена деградирует в other_error)
                    await self._pool.evict(session_id)
                    raise
                except _GenerationFailed as failed:
                    outcome = classify_exception(failed.cause)
                    await self._pool.evict(session_id)
                    # поток мог успеть отдать session_id до падения: сессия CLI на диске уже
                    # есть, и терять её handle нельзя — иначе следующий resume уедет на
                    # предыдущий турн, незаметно съев кусок истории. Обратная сторона: если
                    # мы уже сбросили resume (note), а новый id так и не приехал — старый в
                    # базе оставлять нельзя, иначе «чистый лист» обещан только на словах
                    self._remember_session(session_id, failed.session_id, cleared=note != "")
                    if attempt == 0 and outcome.kind in _BACKOFF_KINDS:
                        # порядок веток тут несущий: перегруз провайдера внешне выглядит как
                        # мёртвый resume, и решить это первым — значит сжечь историю зря
                        await self._sleep(self._retry_backoff)
                        continue
                    if attempt == 0 and _resume_looks_dead(
                        outcome, resume, failed.progressed, failed.cause
                    ):
                        options = self._build_options(
                            cwd=project.brain_path, resume=None, model=session.model
                        )
                        resume, note = None, _RESUME_LOST_NOTE
                        continue
                    if outcome.kind == "auth_error":
                        self._flag_unhealthy(outcome)
                    return AskResult("", outcome, note=note)

                outcome = last.outcome
                if last.is_error:
                    # SDK после ЛЮБОГО результата с is_error намеренно завершает CLI ненулевым
                    # кодом — тёплый клиент в пуле уже мёртв. Не выселить его значит съесть
                    # следующее сообщение человека впустую (ровно после «напиши "продолжи"»).
                    await self._pool.evict(session_id)
                if outcome.kind == "auth_error":
                    await self._pool.evict(session_id)
                    self._flag_unhealthy(outcome)
                    return AskResult("", outcome, note=note)
                if attempt == 0:
                    if outcome.kind == "resume_error" and resume:
                        options = self._build_options(
                            cwd=project.brain_path, resume=None, model=session.model
                        )
                        resume, note = None, _RESUME_LOST_NOTE
                        continue
                    if outcome.kind == "exec_error":
                        await self._pool.evict(session_id)  # перезапуск клиента + повтор (§5.4)
                        continue
                    if outcome.kind in _BACKOFF_KINDS:
                        # лимит/перегруз провайдера: без паузы повтор упрётся в тот же отказ
                        await self._sleep(self._retry_backoff)
                        continue
                break

            assert last is not None
            outcome = last.outcome
            # session_id пишем при ЛЮБОМ исходе, где он есть: сессия CLI создана, и следующий
            # resume должен вести на неё, а не на предыдущий турн (ревью SDK-ядра, §2).
            self._remember_session(session_id, last.session_id, cleared=note != "")
            self._record_usage(session_id, session.model, last)
            if outcome.kind in ("ok", "max_turns"):
                # обе ветки означают живой токен: health ok. health.ok() пишет файл только на
                # переходе degraded→ok (дедуп в HealthMarker).
                self._mark_ok()
            elif outcome.kind == "exec_error":
                # повторный exec_error — устойчиво сломан мозг/MCP; делаем видимым в health,
                # иначе тихо жжёт двойной бюджет (ревью C: exec_error не поднимал health)
                self._flag_unhealthy(Outcome("exec_error", "повторный сбой выполнения (мозг/MCP?)"))
            return AskResult(last.text, outcome, note=note)

    def _remember_session(
        self, session_id: str, claude_session_id: str | None, *, cleared: bool = False
    ) -> None:
        """Запомнить handle сессии CLI. `cleared=True` — мы уже сбросили resume: если новый
        id так и не приехал, старый (битый) в базе оставлять нельзя, иначе следующее
        сообщение снова упрётся в него."""
        if claude_session_id:
            self._store.sessions.set_claude_session_id(session_id, claude_session_id)
        elif cleared:
            self._store.sessions.clear_claude_session_id(session_id)

    def _record_usage(self, session_id: str, model: str | None, final: Final) -> None:
        """Записать расход турна. Пишем и на ошибочных исходах: токены сгорели по-настоящему.

        Цифра SDK — клиентская оценка, а не биллинг (так сказано в доках), поэтому она про
        «сколько я нажёг», а не про счета. Падение учёта не должно стоить человеку ответа.
        """
        if final.total_cost_usd is None and not final.usage:
            return
        usage = final.usage or {}
        try:
            self._store.usage.add(
                session_id=session_id,
                model=model,
                input_tokens=int(usage.get("input_tokens") or 0),
                output_tokens=int(usage.get("output_tokens") or 0),
                cost_usd=float(final.total_cost_usd or 0.0),
            )
        except Exception:  # noqa: BLE001 — учёт расхода не стоит ответа человеку
            logger.warning("не записал usage сессии %s", session_id, exc_info=True)

    async def _generate(
        self,
        session_id: str,
        options: Any,
        prompt: str,
        on_event: EventFn | None,
        fingerprint: Any = None,
    ) -> Final:
        """Один заход генерации: lease + семафор + событийный сбор до Final.

        Любой сбой заворачивается в _GenerationFailed вместе с уликами потока (что успел
        сказать SDK) — по ним `ask` отличает «не поднялась прошлая сессия» от «сломалось
        посреди живого ответа», где сбрасывать resume нельзя.

        «Любой» включает и подъём клиента: connect живёт внутри lease и падает не реже
        генерации (протух токен, не стартовал CLI, wedged-коннект добрал таймаут). Оставить
        его снаружи значит пустить сырое исключение сквозь `ask` — и обещание «фасад не
        бросает» держалось бы ровно до первого неудачного старта.
        """
        seen_session_id: str | None = None
        progressed = False
        try:
            async with self._pool.lease(session_id, options, fingerprint) as client:
                async with self._pool.semaphore:
                    final: Final | None = None

                    async def _watch(messages: AsyncIterator[Any]) -> AsyncIterator[Any]:
                        """Улики снимаем с СЫРЫХ сообщений, а не с событий: целое
                        assistant-сообщение событий не порождает вовсе, а session_id нужен даже
                        когда поток оборвался посреди итерации (тогда до конца stream_events
                        дело не доходит)."""
                        nonlocal seen_session_id, progressed
                        async for message in messages:
                            progressed = True
                            sid = getattr(message, "session_id", None)
                            if isinstance(sid, str) and sid:
                                seen_session_id = sid
                            yield message

                    async def _drain() -> None:
                        nonlocal final
                        async for event in stream_events(_watch(client.receive_response())):
                            if isinstance(event, Final):
                                final = event  # ДО callback: рендер не должен терять ответ
                            if on_event is not None:
                                try:
                                    on_event(event)
                                except Exception:  # noqa: BLE001 — сбой рендера не роняет ответ
                                    logger.warning("on_event упал", exc_info=True)

                    await asyncio.wait_for(client.query(prompt), timeout=self._query_timeout)
                    await asyncio.wait_for(_drain(), timeout=self._response_timeout)
                    if final is None:
                        raise _GenerationFailed(
                            RuntimeError("поток завершился без Final"),
                            session_id=seen_session_id,
                            progressed=progressed,
                        )
                    return final
        except _GenerationFailed:
            raise  # уже с уликами — заворачивать второй раз незачем
        except Exception as exc:  # noqa: BLE001 — заворачиваем с уликами, не глушим
            raise _GenerationFailed(
                exc, session_id=seen_session_id, progressed=progressed
            ) from exc

    def _flag_unhealthy(self, outcome: Outcome) -> None:
        """Пометить degraded и алертить владельца — ТОЛЬКО на переходе (дедуп) и изолированно.

        Флуд алертов (один на каждое сообщение при протухшем токене) убирает дедуп: health
        пишется/алерт шлётся лишь при смене статуса. I/O и alert обёрнуты — их падение не
        должно пробить контракт «ask не бросает».
        """
        changed = True
        if self._health is not None:
            try:
                changed = self._health.degraded(outcome.detail)
            except Exception:  # noqa: BLE001
                logger.warning("health.degraded упал", exc_info=True)
        if changed and self._on_alert is not None:
            try:
                self._on_alert(outcome.detail)
            except Exception:  # noqa: BLE001 — падение алерта не роняет обработку сообщения
                logger.warning("on_alert упал", exc_info=True)

    def _mark_ok(self) -> None:
        if self._health is not None:
            try:
                self._health.ok()
            except Exception:  # noqa: BLE001
                logger.warning("health.ok упал", exc_info=True)

    async def interrupt(self, session_id: str) -> None:
        """Control-plane: прервать идущую генерацию сессии. МИМО session-lock. Безопасный no-op."""
        client = self._pool.get_live(session_id)
        if client is None:
            return
        try:
            await client.interrupt()
        except Exception:  # noqa: BLE001 — interrupt по вытесняемому клиенту = no-op
            pass
