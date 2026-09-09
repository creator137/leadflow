from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://leadflow:leadflow@localhost:5432/leadflow"
    app_environment: str = "development"
    public_base_url: str = "http://localhost:8000"
    secret_key: str = "development-only-change-me"
    chrome_binary: str | None = None
    source_max_scan: int = 500
    yandex_grid: bool = True
    yandex_enrich_emails: bool = False
    log_level: str = "INFO"
    search_provider: str = "serper"
    serper_api_key: str | None = None
    manager_email: str | None = None
    admin_username: str | None = None
    admin_password: str | None = None
    google_sheets_spreadsheet_id: str = "1-xcz_byoVsmrwLFK1qH4ykQSNff3LOkxoHiu0ceHpqg"
    google_service_account_json: str | None = None
    google_sheets_email_status_header: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()


def validate_production_secrets(settings: Settings) -> None:
    if settings.app_environment.casefold() != "production":
        return
    if not settings.admin_password or settings.admin_password == "change-me-before-production":
        raise RuntimeError("ADMIN_PASSWORD must be changed for production")
    if settings.secret_key == "development-only-change-me" or len(settings.secret_key) < 32:
        raise RuntimeError("SECRET_KEY must be a unique value of at least 32 characters for production")
