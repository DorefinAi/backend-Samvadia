"""Every setting in one place, read from environment variables (or a local .env)."""
from functools import lru_cache
from uuid import UUID

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://postgres:postgres@localhost:5432/vartmaan"
    org_id: UUID = UUID("00000000-0000-0000-0000-000000000001")

    cors_origins: str = "http://localhost:3000"
    cookie_secure: bool = True
    session_days: int = 7

    supabase_url: str = ""
    supabase_service_key: str = ""
    storage_bucket: str = "intake"
    max_upload_mb: int = 500

    ftp_protocol: str = "ftp"  # ftp | ftps | sftp
    ftp_host: str = ""
    ftp_port: int = 21
    ftp_user: str = ""
    ftp_password: str = ""
    ftp_base_dir: str = "/vartmaan/incoming"
    ftp_passive: bool = True
    sftp_host_key: str = ""
    sftp_allow_any_host_key: bool = False

    default_story_type: str = "AV"
    slug_subject_max_words: int = 3

    whatsapp_verify_token: str = ""
    whatsapp_app_secret: str = ""
    whatsapp_token: str = ""
    graph_api_version: str = "v21.0"
    whatsapp_merge_seconds: int = 90

    ms_tenant_id: str = ""
    ms_client_id: str = ""
    ms_client_secret: str = ""
    ms_mailbox: str = ""
    mail_poll_seconds: int = 60

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def sqlalchemy_url(self) -> str:
        url = self.database_url
        for prefix in ("postgres://", "postgresql://"):
            if url.startswith(prefix):
                return "postgresql+psycopg://" + url[len(prefix):]
        return url

    @property
    def mail_enabled(self) -> bool:
        return all([self.ms_tenant_id, self.ms_client_id, self.ms_client_secret, self.ms_mailbox])

    @property
    def whatsapp_enabled(self) -> bool:
        return bool(self.whatsapp_token and self.whatsapp_app_secret)


@lru_cache
def get_settings() -> Settings:
    return Settings()
