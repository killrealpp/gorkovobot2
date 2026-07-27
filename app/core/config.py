from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, AliasChoices
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = Path(os.getenv("APP_ENV_FILE", PROJECT_ROOT / ".env"))
if not ENV_FILE.is_absolute():
    ENV_FILE = PROJECT_ROOT / ENV_FILE


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8-sig", extra="ignore")

    app_env: str = "local"
    app_debug: bool = False
    app_timezone: str = "Europe/Moscow"
    log_level: str = "INFO"
    http_trust_env: bool = False
    hold_ttl_minutes: int = 15
    watchlist_enabled: bool = False

    # Channel selection. CLIENT_CHANNELS is kept because your MAX .env already uses it.
    client_channels: str = Field(default="telegram", validation_alias=AliasChoices("CLIENT_CHANNELS", "CLIENT_CHANNEL"))

    telegram_bot_token: str = ""
    telegram_proxy_url: str = ""
    admin_telegram_chat_id: str = ""

    # MAX channel.
    max_bot_token: str = ""
    max_api_base_url: str = "https://platform-api.max.ru"
    max_mode: str = "polling"  # polling | webhook
    max_webhook_enabled: bool = False
    max_webhook_secret: str = ""
    max_webhook_url: str = ""
    max_webhook_host: str = "0.0.0.0"
    max_webhook_port: int = 8080
    max_webhook_path: str = "/webhooks/max"
    max_poll_429_sleep_seconds: int = 60
    max_poll_error_sleep_seconds: int = 10
    max_admin_chat_id: str = ""
    max_admin_user_id: str = ""
    max_admin_secret: str = ""
    max_admin_target_file: str = "data/max_admin_target.json"
    max_admin_error_sleep_seconds: int = 60
    max_media_enabled: bool = True
    max_media_group_enabled: bool = True
    max_message_format: str = "markdown"  # plain | markdown | html

    # Voice messages / speech-to-text.
    voice_messages_enabled: bool = True
    voice_transcription_base_url: str = ""  # defaults to OPENAI_BASE_URL or OpenAI API
    voice_transcription_api_key: str = ""  # defaults to OPENROUTER_API_KEY/OPENAI_API_KEY
    voice_transcription_model: str = "whisper-1"
    voice_transcription_language: str = "ru"
    voice_transcription_response_format: str = "json"
    voice_transcription_timeout_seconds: float = 60.0
    voice_max_download_mb: int = 25
    voice_temp_dir: str = "data/voice_tmp"

    ai_provider: str = "openai"

    # OpenAI-compatible chat completions settings.
    # OPENAI_API_KEY is used by default. DEEPSEEK_API_KEY is kept as a fallback
    # so old .env files do not crash immediately after update.
    openai_api_key: str = ""
    # Extra aliases: convenient when switching providers without renaming env everywhere.
    ai_api_key: str = ""
    api_key: str = ""
    openrouter_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    parser_model: str = "gpt-4o-mini"
    answer_model: str = "gpt-4o"
    answer_fallback_model: str = "gpt-4o-mini"
    openai_temperature: float = 0.1


    # LLM token optimization knobs. These do not change the dialog architecture;
    # they only limit how much context is sent to models on every turn.
    llm_history_limit: int = 6
    llm_availability_limit: int = 120
    llm_object_dates_limit: int = 14
    llm_availability_max_chars: int = 8000
    llm_knowledge_max_chars: int = 8000
    llm_media_decision_enabled: bool = True
    llm_media_availability_max_chars: int = 2500

    # Token budgets per LLM step. Lower values reduce TPM/rate-limit pressure.
    parser_max_tokens: int = 700
    answer_max_tokens: int = 1600
    finalizer_max_tokens: int = 1200
    media_max_tokens: int = 400
    # Kept for backward compatibility with older code.
    openai_max_tokens: int = 700

    # 429 / transient error retry policy.
    llm_max_retries: int = 3
    llm_retry_base_seconds: float = 1.5
    llm_retry_max_seconds: float = 8.0
    llm_request_timeout_seconds: float = 45.0

    # Backward compatibility with the old DeepSeek/OpenRouter-style config.
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    openai_model: str = "gpt-4o"

    yclients_base_url: str = "https://api.yclients.com/api/v1"
    yclients_partner_token: str = ""
    yclients_user_token: str = ""
    yclients_company_id: str = ""
    yclients_sync_enabled: bool = True
    yclients_sync_days_back: int = 1
    yclients_sync_days_forward: int = 60
    yclients_sync_interval_seconds: int = 3600

    db_host: str = Field(default="", validation_alias=AliasChoices("DB_HOST", "OWN_PORT", "SUPABASE_HOST"))
    db_port: int = Field(default=5432, validation_alias=AliasChoices("DB_PORT", "OWN_HOST", "SUPABASE_PORT"))
    db_name: str = Field(default="", validation_alias=AliasChoices("DB_NAME", "OWN_NAME", "SUPABASE_DB_NAME"))
    db_user: str = Field(default="", validation_alias=AliasChoices("DB_USER", "OWN_USER", "SUPABASE_USER"))
    db_password: str = Field(default="", validation_alias=AliasChoices("DB_PASSWORD", "OWN_PASSWORD", "SUPA_PASS", "SUPABASE_PASSWORD"))
    db_sslmode: str = Field(default="prefer", validation_alias=AliasChoices("DB_SSLMODE", "DATABASE_SSL_MODE"))
    db_sslrootcert: str = "~/.postgresql/root.crt"
    db_target_session_attrs: str = "read-write"
    db_connect_timeout: int = 15

    payment_provider: str = "yookassa"
    payment_shop_id: str = ""
    payment_secret_key: str = ""
    payment_success_url: str = ""
    payment_fail_url: str = ""
    prepayment_amount_rub: int = 1
    payment_status_loop_enabled: bool = True
    payment_status_sync_interval_seconds: int = 10
    watchlist_loop_enabled: bool = True
    watchlist_loop_interval_seconds: int = 15
    startup_availability_refresh_enabled: bool = True
    startup_availability_refresh_delay_seconds: int = 10

    # Automatic retention cleanup. Disabled by default so production never
    # starts mutating historical data until the target DB has been reviewed.
    retention_cleanup_enabled: bool = False
    retention_cleanup_dry_run: bool = False
    retention_cleanup_interval_seconds: int = 86400
    retention_cleanup_startup_delay_seconds: int = 300
    retention_cleanup_max_rows: int = 1000
    retention_cleanup_include_files: bool = True
    retention_cleanup_only: str = (
        "old_messages,"
        "old_inactive_conversations,"
        "old_system_logs,"
        "old_inactive_slot_holds,"
        "active_holds_past_expiry,"
        "old_sent_admin_notifications,"
        "stale_availability_cache,"
        "past_availability_cache_dates,"
        "old_inactive_watchlist,"
        "old_voice_temp_files"
    )
    retention_messages_days: int = 90
    retention_system_logs_days: int = 30
    retention_holds_days: int = 1
    retention_admin_notifications_days: int = 30
    retention_availability_cache_days: int = 7
    retention_watchlist_days: int = 30
    retention_voice_temp_days: int = 1
    retention_booking_review_days: int = 180

    sqlite_path: str = Field(default="bot.sqlite3")
    admin_profile_path: str = "business_profile/admin_profile.yaml"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def sqlite_path() -> Path:
    path = Path(get_settings().sqlite_path)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path
