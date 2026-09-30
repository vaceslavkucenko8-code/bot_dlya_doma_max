"""Централизованная конфигурация приложения.

Все настройки читаются из переменных окружения (см. .env.example).
Никаких секретов и хардкода значений тут быть не должно.
"""
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- База данных ---
    database_url: str = Field(
        default="postgresql+psycopg2://domradar:domradar@localhost:5432/domradar",
        alias="DATABASE_URL",
    )

    # --- MAX бот / API ---
    max_bot_token: str = Field(default="", alias="MAX_BOT_TOKEN")
    max_api_base_url: str = Field(default="https://platform-api2.max.ru", alias="MAX_API_BASE_URL")
    max_api_timeout_seconds: float = Field(default=5.0, alias="MAX_API_TIMEOUT_SECONDS")
    max_api_max_retries: int = Field(default=3, alias="MAX_API_MAX_RETRIES")
    max_ca_bundle: str = Field(default="", alias="MAX_CA_BUNDLE")

    # --- Webhook безопасность ---
    webhook_shared_secret: str = Field(default="", alias="WEBHOOK_SHARED_SECRET")

    # --- SLA ---
    sla_threshold_hours: int = Field(default=24, alias="SLA_THRESHOLD_HOURS")
    sla_check_interval_minutes: int = Field(default=60, alias="SLA_CHECK_INTERVAL_MINUTES")

    # --- Файлы (фото от жителей) ---
    photos_storage_dir: str = Field(default="/data/photos", alias="PHOTOS_STORAGE_DIR")
    photo_max_bytes: int = Field(default=10 * 1024 * 1024, ge=1, alias="PHOTO_MAX_BYTES")
    # Список хостов (через запятую), с которых разрешено скачивать фото,
    # например CDN MAX. Поддомены разрешены. Пусто = любой публичный хост.
    photo_allowed_hosts: str = Field(default="", alias="PHOTO_ALLOWED_HOSTS")

    # --- Ограничения входных данных ---
    # Максимальный размер тела HTTP-запроса. Самый крупный легальный запрос —
    # текст 4000 символов (≈8 КБ в UTF-8), так что 64 КБ — с большим запасом.
    max_request_body_bytes: int = Field(default=64 * 1024, ge=1024, alias="MAX_REQUEST_BODY_BYTES")
    # Окно, в течение которого повтор ТОГО ЖЕ текста от того же жителя по ещё
    # открытому инциденту считается дублем (двойное нажатие, повторная
    # доставка webhook ботом) и не создаёт новую заявку.
    duplicate_message_window_seconds: int = Field(default=600, ge=0, alias="DUPLICATE_MESSAGE_WINDOW_SECONDS")

    # --- Прочее ---
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    environment: str = Field(default="production", alias="ENVIRONMENT")


    @property
    def is_dev_environment(self) -> bool:
        """Окружения, где допустим webhook без секрета (с предупреждением).
        Всё остальное, включая значение по умолчанию production, — закрыто."""
        return self.environment.strip().lower() in {"local", "test", "development", "dev"}

    @property
    def photo_allowed_host_list(self) -> list[str]:
        return [h.strip().lower().lstrip(".") for h in self.photo_allowed_hosts.split(",") if h.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
