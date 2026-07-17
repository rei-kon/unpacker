"""Конфиг из окружения / .env (Pydantic Settings)."""

from __future__ import annotations

from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


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

    # Хранилище сессий
    db_path: str = "sessions.db"

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
