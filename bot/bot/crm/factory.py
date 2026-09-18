from bot.config import crm_env_override_allowed, settings
from bot.crm.amocrm import provider_from_env, provider_from_org
from bot.crm.base import CRMProvider
from bot.crm.registry import build_crm_provider, crm_config, resolve_crm_provider_key
from bot.db.models import Organization
from bot.services.org_secrets import get_org_secret


def get_crm_provider(org: Organization) -> CRMProvider:
    """
    Multi-tenant приоритет: используем crm_* на уровне Organization.
    Env fallback (AMOCRM_*) разрешаем только при CRM_ENV_FALLBACK_ENABLED=true и не strict.
    """
    allow_env = crm_env_override_allowed() and settings.crm_env_fallback_enabled
    key = resolve_crm_provider_key(org)

    if key == "amocrm":
        base_url = org.crm_base_url
        token = get_org_secret(org, "crm_api_token")
        if allow_env:
            base_url = base_url or settings.amocrm_base_url
            token = token or settings.amocrm_token
        return provider_from_org(base_url=base_url, token=token, config=crm_config(org))

    if key == "demo" and allow_env:
        # Legacy dev convenience: an org with NO crm_provider configured can
        # fall back to a shared AMOCRM_* env sandbox when explicitly opted
        # in. Must not extend to yclients/generic_rest/macdent below — those
        # have their own real per-org credentials and must never be silently
        # swapped for a hardcoded env-configured AmoCRMProvider.
        return provider_from_env()

    return build_crm_provider(org)
