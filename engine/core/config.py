"""Конфиг из окружения / .env (Pydantic Settings)."""

from __future__ import annotations

from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from engine.core.uploads import MAX_UPLOAD_BYTES


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Telegram
    telegram_bot_token: str
    # NoDecode → отключаем встроенный JSON-парс, CSV разбирает валидатор ниже
    allowed_user_ids: Annotated[list[int], NoDecode]

    # Агент
    # office_cwd удалён: в модели §5.2 cwd берётся из projects.brain_path (store.py),
    # дефолт на боевой каталог был миной молчаливого отказа (находка ревью Фазы 0).
    system_prompt: str | None = None
    # append-личность поверх пресета claude_code: один движок обслуживает любую
    # папку-мозг, сохраняя весь тулинг Claude Code
    system_prompt_append: str | None = None
    max_turns: int | None = None
    # таймаут ответа агента — иначе зависший receive_response держит per-chat lock вечно
    response_timeout: float = 180.0

    # Auth подписки (наследуется subprocess claude CLI)
    claude_code_oauth_token: str | None = None

    # Инстанс-состояние (§4: живёт в ~/agents/<name>/state/, не в мозге)
    db_path: str = "state.db"
    health_path: str = "health.json"

    # Проекты/мозги
    # Дефолтный проект окна при первом сообщении; пусто → первый активный из БД.
    default_project_slug: str | None = None
    # Простой клиента до idle-evict (§5.1) — сек; освобождает RAM в тишине.
    idle_timeout: float = 1800.0

    # Косметика §6.1/§9 — всё включено по умолчанию, выключается флагом в .env.
    # Пути относительны инстанс-каталогу (WorkingDirectory юнита), как db_path/health_path.
    buttons_enabled: bool = True
    buttons_path: str = "buttons.yaml"
    uploads_enabled: bool = True
    uploads_dir: str = "state/uploads"
    # Потолок на приём файла. Больше предела Bot API поднять нельзя (валидатор ниже):
    # движок обещал бы то, чего Telegram не даст, — тихий отказ вместо честного сообщения.
    max_upload_bytes: int = MAX_UPLOAD_BYTES
    send_file_enabled: bool = True

    @property
    def owner_user_id(self) -> int | None:
        """Владелец = первый из allow-list (кому шлём health-алерты §5.4)."""
        return self.allowed_user_ids[0] if self.allowed_user_ids else None

    @field_validator("max_upload_bytes", mode="after")
    @classmethod
    def _cap_upload_limit(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("MAX_UPLOAD_BYTES должен быть положительным")
        if v > MAX_UPLOAD_BYTES:
            raise ValueError(
                f"MAX_UPLOAD_BYTES выше предела Bot API ({MAX_UPLOAD_BYTES} байт ≈ 20 МБ): "
                "Telegram всё равно не отдаст боту файл больше — обещать нельзя"
            )
        return v

    @field_validator("allowed_user_ids", mode="before")
    @classmethod
    def _parse_ids(cls, v: object) -> object:
        if isinstance(v, str):
            try:
                return [int(x.strip()) for x in v.split(",") if x.strip()]
            except ValueError as exc:
                raise ValueError(
                    "ALLOWED_USER_IDS должен быть списком целых через запятую, напр. '111,222'"
                ) from exc
        return v
