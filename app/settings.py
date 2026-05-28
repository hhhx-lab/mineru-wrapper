from __future__ import annotations

import os
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    mineru_api_base_url: str = "http://127.0.0.1:8000"
    wrapper_public_base_url: str = "http://100.64.0.2:8100"
    redis_url: str = "redis://127.0.0.1:6379/0"
    wrapper_auth_token: str
    download_signing_secret: str

    result_dir: str = "/app/data/results"
    tmp_dir: str = "/app/data/tmp"

    mineru_backend: str = "hybrid-auto-engine"
    mineru_parse_method: str = "auto"
    mineru_lang_list: str = "ch"
    mineru_formula_enable: bool = True
    mineru_table_enable: bool = True
    mineru_image_analysis: bool = True
    mineru_return_images: bool = True

    mineru_max_attempts: int = 3
    mineru_task_timeout_seconds: int = 3600
    mineru_poll_interval_seconds: float = 5.0
    mineru_download_timeout_seconds: int = 600
    legacy_doc_conversion_timeout_seconds: int = 300
    mineru_result_ttl_hours: int = 72
    signed_url_ttl_seconds: int = 7200
    max_download_bytes: int = 524288000
    artifact_download_ttl_hours: int = 6
    artifact_done_ttl_hours: int = 24
    artifact_cleanup_interval_seconds: int = 300

    request_trust_env: bool = False

    @property
    def lang_items(self) -> list[str]:
        return [item.strip() for item in self.mineru_lang_list.split(",") if item.strip()] or ["ch"]

    @property
    def public_api_base(self) -> str:
        return self.wrapper_public_base_url.rstrip("/")

    @property
    def native_api_base(self) -> str:
        return self.mineru_api_base_url.rstrip("/")


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    os.makedirs(settings.result_dir, exist_ok=True)
    os.makedirs(settings.tmp_dir, exist_ok=True)
    return settings
