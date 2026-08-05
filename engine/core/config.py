"""Конфиг из окружения / .env (Pydantic Settings)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
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

    # Инстанс-состояние (§4: живёт в ~/agents/<name>/state/, не в мозге).
    # STATE_DIR — ЯВНЫЙ корень состояния и единственный источник для проверки путей
    # `[SEND_FILE:]` (§8.2). Раньше корень выводился как parent(db_path), и при дефолтном
    # `DB_PATH=state.db` им становился каталог инстанса — вместе с `.env`, где лежат токен
    # бота и токен подписки (K1/M-07/SEC-5/C17). Настройка развязана: сдвиг БД больше не
    # двигает границу безопасности.
    state_dir: str = "state"
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
    # None → считаем от STATE_DIR (одна вселенная путей). Явный UPLOADS_DIR остаётся
    # рабочей ручкой: у кого-то uploads уезжают на отдельный диск.
    uploads_dir: str | None = None
    # Потолок на приём файла. Больше предела Bot API поднять нельзя (валидатор ниже):
    # движок обещал бы то, чего Telegram не даст, — тихий отказ вместо честного сообщения.
    max_upload_bytes: int = MAX_UPLOAD_BYTES
    send_file_enabled: bool = True
    # Черновик «печатает…» (§9). Раньше интервал и окно были зашиты в bot.py: ученик,
    # у которого бот стоит в живой группе, не мог ни притушить черновик, ни выключить его —
    # только править код движка. STREAM_INTERVAL — быстрая ступень лестницы (см. DraftTuning),
    # остальные ступени считаются от неё.
    # Границы — не педантизм: ручку правит ученик руками в .env. STREAM_INTERVAL=0 даёт
    # цикл без сна (100% CPU на его же VPS), окно шире 4096 — 400 MESSAGE_TOO_LONG на
    # каждом тике, а окно мельче служебной строки-счётчика показывает пустоту вместо текста.
    stream_enabled: bool = True
    stream_interval: float = Field(default=1.2, ge=0.2)
    stream_max_units: int = Field(default=3600, ge=200, le=4000)

    @property
    def owner_user_id(self) -> int | None:
        """Владелец = первый из allow-list (кому шлём health-алерты §5.4)."""
        return self.allowed_user_ids[0] if self.allowed_user_ids else None

    @property
    def uploads_path(self) -> Path:
        """Куда кладём принятые файлы: явный UPLOADS_DIR либо `<STATE_DIR>/uploads`."""
        if self.uploads_dir:
            return Path(self.uploads_dir)
        return Path(self.state_dir) / "uploads"

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
