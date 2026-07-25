"""SessionRouter — логика роутинга и команд слоя 1, без aiogram.

Топик Telegram = окно поверхности в сессию (§5.2, биндинг). surface_key = "chat_id:thread_id".
Первое сообщение в новый топик заводит сессию дефолтного проекта и биндит окно; дальше окно
резолвится в ту же сессию. /projects переключает окно на новую сессию другого проекта.

Отделено от aiogram намеренно: вся логика тестируется юнитом с фейковым core, а bot.py — тонкая
склейка с Telegram. Команды слоя 1 (§5.5): /projects /status /stop /close /clear /model.
"""

from __future__ import annotations

from typing import Any, Protocol

from engine.core.models import Surface
from engine.core.store import Store

SURFACE: Surface = "tg"
VALID_MODELS = ("opus", "sonnet", "haiku")


class NoProjectError(Exception):
    """В сторе нет активного проекта — разворачивать сессию не на что."""


class _Core(Protocol):
    async def ask(self, session_id: str, prompt: str, on_event: Any = None) -> Any: ...
    async def interrupt(self, session_id: str) -> None: ...


class SessionRouter:
    def __init__(self, *, store: Store, core: _Core, default_project_slug: str | None = None):
        self._store = store
        self._core = core
        self._default_slug = default_project_slug

    def surface_key(self, chat_id: int, thread_id: int | None) -> str:
        return f"{chat_id}:{thread_id or 0}"

    async def on_message(
        self,
        *,
        chat_id: int,
        thread_id: int | None,
        user_id: int,
        text: str,
        on_event: Any = None,
    ) -> Any:
        """Обработать сообщение: резолвить/создать сессию окна и передать в core.ask."""
        sid = self.ensure_session(chat_id=chat_id, thread_id=thread_id, user_id=user_id)
        return await self._core.ask(sid, text, on_event)

    def ensure_session(self, *, chat_id: int, thread_id: int | None, user_id: int) -> str:
        """Вернуть сессию окна, создав её при необходимости.

        Отдельно от on_message, потому что приём файла (§5.5) должен знать session_id ДО
        отправки промпта: файл кладётся в `state/uploads/<session_id>/`, и каталог не из чего
        назвать, пока сессии нет. Бросает NoProjectError — как и on_message, ловит хендлер.
        """
        sk = self.surface_key(chat_id, thread_id)
        sid = self._store.bindings.resolve(SURFACE, sk)
        if sid is None:
            sid = self._open_session(user_id, sk, self._default_project_slug())
        return sid

    def brain_path(self, chat_id: int, thread_id: int | None) -> str | None:
        """Папка-мозг текущей сессии окна — корень песочницы `[SEND_FILE:]` (§8.2).

        Нет сессии/проекта → None: вызывающий останется с пустой песочницей и не отдаст
        ничего (fail-closed), вместо «возьму какой-нибудь мозг».
        """
        sid = self._store.bindings.resolve(SURFACE, self.surface_key(chat_id, thread_id))
        if sid is None:
            return None
        session = self._store.sessions.get(sid)
        if session is None:
            return None
        project = self._store.projects.get(session.project_slug)
        return project.brain_path if project is not None else None

    # ── команды ──────────────────────────────────────────────────────────────

    def list_projects_text(self) -> str:
        projects = self._store.projects.list_active()
        if not projects:
            return "Пока нет ни одного проекта."
        lines = ["Проекты (пиши /switch <slug> для переключения):"]
        lines += [f"• {p.slug} — {p.name}" for p in projects]
        return "\n".join(lines)

    async def switch_project(
        self, *, chat_id: int, thread_id: int | None, user_id: int, slug: str
    ) -> str:
        project = self._store.projects.get(slug)
        if project is None or project.status != "active":
            return f"Нет активного проекта «{slug}». Список — /projects."
        sk = self.surface_key(chat_id, thread_id)
        self._open_session(user_id, sk, slug)
        return f"Переключился на проект «{project.name}» ({slug}). Новая сессия."

    def status_text(self, chat_id: int, thread_id: int | None) -> str:
        sk = self.surface_key(chat_id, thread_id)
        sid = self._store.bindings.resolve(SURFACE, sk)
        if sid is None:
            return "В этом топике пока нет активной сессии. Напиши что-нибудь или /projects."
        s = self._store.sessions.get(sid)
        assert s is not None
        return (
            f"Проект: {s.project_slug}\n"
            f"Сессия: {s.id}\n"
            f"Модель: {s.model or 'по умолчанию'}\n"
            f"Verbose: {s.verbose}\n"
            f"Статус: {s.status}"
        )

    async def stop(self, chat_id: int, thread_id: int | None) -> None:
        sk = self.surface_key(chat_id, thread_id)
        sid = self._store.bindings.resolve(SURFACE, sk)
        if sid is not None:
            await self._core.interrupt(sid)

    def close(self, chat_id: int, thread_id: int | None) -> str:
        sk = self.surface_key(chat_id, thread_id)
        sid = self._store.bindings.resolve(SURFACE, sk)
        if sid is None:
            return "Закрывать нечего — активной сессии нет."
        self._store.sessions.close(sid)
        return "Сессия закрыта. Новое сообщение начнёт свежую."

    async def clear(self, *, chat_id: int, thread_id: int | None, user_id: int) -> str:
        """Новая сессия того же проекта (сброс контекста), окно перевязывается.

        slug вычисляем ПОСЛЕ проверки сессии (не безусловно — иначе _default_project_slug
        бросал бы раньше времени). Если проект текущей сессии уже disabled — фолбэк на
        дефолтный активный, иначе create отверг бы и хендлер упал (находка ревью D).
        """
        sk = self.surface_key(chat_id, thread_id)
        sid = self._store.bindings.resolve(SURFACE, sk)
        slug: str | None = None
        if sid is not None:
            s = self._store.sessions.get(sid)
            if s is not None:
                proj = self._store.projects.get(s.project_slug)
                if proj is not None and proj.status == "active":
                    slug = s.project_slug
        if slug is None:
            slug = self._default_project_slug()  # может бросить NoProjectError — ловит хендлер
        self._open_session(user_id, sk, slug)
        return "Контекст сброшен. Новый диалог."

    def set_model(self, chat_id: int, thread_id: int | None, model: str) -> str:
        if model not in VALID_MODELS:
            return f"Не знаю модель «{model}». Доступны: {', '.join(VALID_MODELS)}."
        sk = self.surface_key(chat_id, thread_id)
        sid = self._store.bindings.resolve(SURFACE, sk)
        if sid is None:
            return "Сначала начни диалог — активной сессии нет."
        self._store.sessions.set_model(sid, model)
        return f"Модель переключена на {model}. Применится к следующему сообщению."

    def set_verbose(self, chat_id: int, thread_id: int | None, level: int) -> str:
        if level not in (0, 1):
            return "Verbose: 0 (тихо) или 1 (показывать шаги)."
        sk = self.surface_key(chat_id, thread_id)
        sid = self._store.bindings.resolve(SURFACE, sk)
        if sid is None:
            return "Сначала начни диалог — активной сессии нет."
        self._store.sessions.set_verbose(sid, level)
        return f"Verbose = {level}."

    # ── внутреннее ───────────────────────────────────────────────────────────

    def _default_project_slug(self) -> str:
        if self._default_slug:
            proj = self._store.projects.get(self._default_slug)
            if proj is not None and proj.status == "active":
                return self._default_slug
        active = self._store.projects.list_active()
        if not active:
            raise NoProjectError("нет активного проекта")
        return active[0].slug

    def _open_session(self, user_id: int, surface_key: str, project_slug: str) -> str:
        session = self._store.sessions.create(owner_user_id=user_id, project_slug=project_slug)
        self._store.bindings.bind(SURFACE, surface_key, session.id)
        return session.id
