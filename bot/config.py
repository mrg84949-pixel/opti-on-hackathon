from __future__ import annotations

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Database
    database_url: str = "postgresql+asyncpg://bot:bot@localhost:5432/bot_saas"

    # AI providers: gemini (prod default) | groq (dev) | stub
    ai_provider: str = "gemini"
    ai_use_stub: bool = False
    ai_stub_message: str = "Тестовый режим: ответ без вызова ИИ."
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash-lite"
    # Gemini resilience: bounded retry+backoff on 429/RESOURCE_EXHAUSTED (mirrors Groq).
    gemini_max_retries: int = 2
    gemini_retry_base_delay: float = 0.5
    gemini_retry_max_delay: float = 8.0
    groq_api_key: str = ""
    groq_model: str = "openai/gpt-oss-120b"
    groq_base_url: str = "https://api.groq.com/openai/v1"
    # Groq resilience: bounded retry+backoff on 429/rate-limit (no provider switch).
    groq_max_retries: int = 2
    groq_retry_base_delay: float = 0.5
    groq_retry_max_delay: float = 8.0
    ai_booking_shortcuts: bool = True
    ai_booking_fsm: bool = False

    # Organization
    default_org_id: int = 1
    tenant_config_strict: bool = False

    # Auth
    user_auth_secret: str = "change-me"
    admin_api_token: str = ""
    admin_ui_token: str = ""

    # Telegram
    telegram_token: str = ""
    telegram_webhook_url: str = ""
    telegram_webhook_secret: str = ""
    admin_telegram_bot_token: str = ""

    # MacDent CRM inbound webhook (onCreate/onChange/onRemove) — unguessable
    # path segment, since MacDent's webhook.set has no way to attach a custom
    # auth header. Single shared secret for now (one MacDent clinic in pilot);
    # revisit if/when a second MacDent org is onboarded.
    macdent_webhook_secret: str = ""

    # WhatsApp — default provider
    whatsapp_provider: str = "green"
    whatsapp_verify_token: str = ""
    whatsapp_app_secret: str = ""
    whatsapp_graph_access_token: str = ""
    whatsapp_graph_api_version: str = "v21.0"
    whatsapp_phone_number_id: str = ""

    # Green-API (WhatsApp legacy/dev)
    green_api_instance_id: str = ""
    green_api_token: str = ""

    # SMTP
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from_email: str = ""
    smtp_use_tls: bool = True

    # Twilio Voice (hackathon call module)
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_phone_number: str = ""

    # CRM
    crm_provider: str = "macdent"
    crm_env_fallback_enabled: bool = False
    amocrm_base_url: str = ""
    amocrm_token: str = ""

    # Google OAuth
    google_oauth_client_id: str = ""
    google_oauth_redirect_uri: str = ""

    # URLs
    public_app_url: str = ""
    backend_public_url: str = ""
    frontend_base_url: str = "http://localhost:3000"
    contact_form_to_email: str = ""

    # Ops
    log_level: str = "INFO"
    llm_debug_trace: bool = False
    strict_startup_validation: bool = False
    tenant_secrets_master_key: str = ""
    skip_metadata_create_all: bool = False

    # Reminders
    reminder_job_interval_minutes: int = 60
    reminder_window_minutes: int = 60
    reminder_fair_max_per_org_per_tick: int = 100
    crm_sync_interval_minutes: int = 15

    # Stress/smoke runners — TG user id without tg: prefix (not a real personal account)
    stress_tg_user: str = "900000001"

    # Admin-proposed appointment change — hours for client to respond
    client_change_response_hours: int = 24

    # Rate limits (in-memory per process)
    rate_limit_enabled: bool = True
    rate_limit_auth_ip_limit: int = 30
    rate_limit_auth_email_limit: int = 10
    rate_limit_webhook_ip_limit: int = 600
    rate_limit_contact_ip_limit: int = 10
    # Per-sender (not per-IP) — protects LLM spend from one WhatsApp number
    # flooding the bot. Separate from rate_limit_webhook_ip_limit above,
    # which caps total requests per IP, not per conversation.
    rate_limit_whatsapp_sender_limit: int = 3
    rate_limit_whatsapp_sender_window_seconds: int = 60

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()


def tenant_env_fallback_allowed() -> bool:
    return not settings.tenant_config_strict


def resolve_tenant_outbound_secret(org_value: str | None, env_value: str | None) -> str:
    org_val = (org_value or "").strip()
    if org_val or not tenant_env_fallback_allowed():
        return org_val
    return (env_value or "").strip()


def crm_env_override_allowed() -> bool:
    return tenant_env_fallback_allowed()
