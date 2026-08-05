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
  • прочее исключение → drop клиента, вернуть outcome (D покажет сообщение, чат не залипает).
ask НЕ бросает на ошибках SDK — возвращает AskResult(text, outcome); бросает только LookupError
(программная ошибка — нет сессии). События verbose 1 отдаются через on_event; рендеринг — Срез D.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
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
_QUERY_TIMEOUT = 60.0


def detect_ram_bytes() -> int:
    """Физическая память машины (Linux и macOS). Потолок пула считается отсюда (§5.1)."""
    import os

    try:
        return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError, AttributeError):
        return 4 * 1024**3


@dataclass(frozen=True)
class AskResult:
    text: str
    outcome: Outcome


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
    ):
        self._store = store
        self._pool = pool
        self._build_options = options_builder
        self._response_timeout = response_timeout
        self._query_timeout = query_timeout
        self._health = health
        self._on_alert = on_alert

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

            options = self._build_options(
                cwd=project.brain_path,
                resume=session.claude_session_id,
                model=session.model,
            )

            last: Final | None = None
            for attempt in (0, 1):
                try:
                    last = await self._generate(session_id, options, prompt, on_event)
                except asyncio.CancelledError:
                    # отмена (shutdown/таймаут сверху) — НЕ глотаем: evict и пробрасываем,
                    # иначе graceful shutdown ломается (отмена деградирует в other_error)
                    await self._pool.evict(session_id)
                    raise
                except Exception as exc:  # noqa: BLE001 — ошибку классифицируем, чат не роняем
                    outcome = classify_exception(exc)
                    await self._pool.evict(session_id)
                    if outcome.kind == "auth_error":
                        self._flag_unhealthy(outcome)
                    return AskResult("", outcome)

                outcome = last.outcome
                if last.is_error:
                    # SDK после ЛЮБОГО результата с is_error намеренно завершает CLI ненулевым
                    # кодом — тёплый клиент в пуле уже мёртв. Не выселить его значит съесть
                    # следующее сообщение человека впустую (ровно после «напиши "продолжи"»).
                    await self._pool.evict(session_id)
                if outcome.kind == "auth_error":
                    await self._pool.evict(session_id)
                    self._flag_unhealthy(outcome)
                    return AskResult("", outcome)
                if outcome.kind == "exec_error" and attempt == 0:
                    await self._pool.evict(session_id)  # перезапуск клиента + повтор (§5.4)
                    continue
                break

            assert last is not None
            outcome = last.outcome
            if outcome.kind in ("ok", "max_turns"):
                # обе ветки означают живой токен: пишем claude_session_id (C2a) + health ok.
                # health.ok() пишет файл только на переходе degraded→ok (дедуп в HealthMarker).
                if last.session_id:
                    self._store.sessions.set_claude_session_id(session_id, last.session_id)
                self._mark_ok()
            elif outcome.kind == "exec_error":
                # повторный exec_error — устойчиво сломан мозг/MCP; делаем видимым в health,
                # иначе тихо жжёт двойной бюджет (ревью C: exec_error не поднимал health)
                self._flag_unhealthy(Outcome("exec_error", "повторный сбой выполнения (мозг/MCP?)"))
            return AskResult(last.text, outcome)

    async def _generate(
        self, session_id: str, options: Any, prompt: str, on_event: EventFn | None
    ) -> Final:
        """Один заход генерации: lease + семафор + событийный сбор до Final."""
        async with self._pool.lease(session_id, options) as client:
            async with self._pool.semaphore:
                await asyncio.wait_for(client.query(prompt), timeout=self._query_timeout)
                final: Final | None = None

                async def _drain() -> None:
                    nonlocal final
                    async for event in stream_events(client.receive_response()):
                        if isinstance(event, Final):
                            final = event  # присваиваем ДО callback: рендер не должен терять ответ
                        if on_event is not None:
                            try:
                                on_event(event)
                            except Exception:  # noqa: BLE001 — сбой рендера не роняет генерацию
                                logger.warning("on_event упал", exc_info=True)

                await asyncio.wait_for(_drain(), timeout=self._response_timeout)
                if final is None:
                    raise RuntimeError("поток завершился без Final")
                return final

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
