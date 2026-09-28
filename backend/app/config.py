"""Настройки сервиса из переменных окружения. Живое: менять только добавлением полей."""
from datetime import date
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

APP_DIR = Path(__file__).resolve().parent
ROOT = APP_DIR.parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./state/moscollector.db"
    ml_url: str = "http://ml:8001"
    ml_timeout_s: float = 120.0
    demo_today: date = date(2026, 6, 30)
    demo_settings_locked: bool = False
    secret_key: str = ""
    demo_password: str = ""
    integration_api_key: str = ""
    seed_demo: bool = True
    cookie_secure: bool = False
    session_hours: int = 12
    contracts_dir: Path = ROOT / "contracts"
    raw_data_dir: Path = ROOT / "data" / "raw"
    static_dir: Path = APP_DIR / "static"
    # Корпоративный каталог (backend/app/directory.py). Пустой ldap_url — только локальные
    # учётные записи из users, как до подключения каталога.
    ldap_url: str = ""
    ldap_starttls: bool = False
    ldap_tls_verify: bool = True
    ldap_tls_ca_file: str = ""
    ldap_timeout_s: float = 5.0
    ldap_bind_dn: str = ""
    ldap_bind_password: str = ""
    ldap_user_dn_template: str = ""
    ldap_user_base: str = ""
    ldap_user_filter: str = "(uid={login})"
    ldap_group_base: str = ""
    ldap_group_filter: str = "(|(member={user_dn})(uniqueMember={user_dn})(memberUid={login}))"
    ldap_role_groups: str = ""
    ldap_allow_local: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
