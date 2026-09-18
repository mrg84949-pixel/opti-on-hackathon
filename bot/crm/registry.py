from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from bot.config import settings
from bot.crm.amocrm import provider_from_org as amocrm_from_org
from bot.crm.base import CRMProvider
from bot.crm.generic_rest import provider_from_org as generic_rest_from_org
from bot.crm.macdent import provider_from_org as macdent_from_org
from bot.crm.yclients import provider_from_org as yclients_from_org
from bot.db.models import Organization
from bot.services.org_secrets import get_org_secret

logger = logging.getLogger(__name__)

ProviderFactory = Callable[[Organization], CRMProvider]

KNOWN_CRM_PROVIDER_KEYS = frozenset({"amocrm", "demo", "none", "generic_rest", "yclients", "macdent"})
CRM_CONFIG_PATH_KEYS = frozenset(
    {
        "slots_path",
        "booking_path",
        "staff_path",
        "list_appointments_path",
        "cancelled_status_value",
        "company_id",
        "default_service_id",
        "where_know",
        "funnel_stage",
        "filial",
    }
)


def crm_config(org: Organization) -> dict[str, Any]:
    raw = getattr(org, "crm_config", None)
    return dict(raw) if isinstance(raw, dict) else {}


def public_crm_config(org: Organization) -> dict[str, str]:
    """Non-secret CRM config keys safe for API responses."""
    return {
        key: str(value).strip()
        for key, value in crm_config(org).items()
        if key in CRM_CONFIG_PATH_KEYS and str(value or "").strip()
    }


def merge_crm_config(org: Organization, patch: dict[str, Any] | None) -> None:
    if not patch:
        return
    current = crm_config(org)
    for key, value in patch.items():
        if key not in CRM_CONFIG_PATH_KEYS:
            continue
        cleaned = (str(value).strip() if value is not None else "") or None
        if cleaned is None:
            current.pop(key, None)
        else:
            current[key] = cleaned
    org.crm_config = current or None


@dataclass(frozen=True)
class IntegrationField:
    name: str
    label: str
    field_type: Literal["string", "secret", "url"]
    required: bool


@dataclass(frozen=True)
class CrmIntegrationMeta:
    code: str
    display_name: str
    docs_url: str | None
    fields: tuple[IntegrationField, ...]


def _build_amocrm(org: Organization) -> CRMProvider:
    return amocrm_from_org(
        base_url=org.crm_base_url,
        token=get_org_secret(org, "crm_api_token"),
        config=crm_config(org),
    )


def _build_generic_rest(org: Organization) -> CRMProvider:
    return generic_rest_from_org(
        base_url=org.crm_base_url,
        token=get_org_secret(org, "crm_api_token"),
        config=crm_config(org),
    )


def _build_yclients(org: Organization) -> CRMProvider:
    cfg = crm_config(org)
    return yclients_from_org(
        partner_token=get_org_secret(org, "crm_api_token"),
        company_id=str(cfg.get("company_id") or "").strip() or None,
        user_token=get_org_secret(org, "crm_user_token"),
        config=cfg,
    )


def _build_macdent(org: Organization) -> CRMProvider:
    # Base URL is fixed (https://api-developer.macdent.kz) — MacDent's API
    # shape isn't client-configurable like generic_rest, so org.crm_base_url
    # is intentionally not read here.
    return macdent_from_org(
        access_token=get_org_secret(org, "crm_api_token"),
        config=crm_config(org),
    )


def _build_demo(_org: Organization) -> CRMProvider:
    return amocrm_from_org(base_url=None, token=None)


CRM_REGISTRY: dict[str, ProviderFactory] = {
    "amocrm": _build_amocrm,
    "generic_rest": _build_generic_rest,
    "yclients": _build_yclients,
    "macdent": _build_macdent,
    "demo": _build_demo,
    "none": _build_demo,
}

CRM_INTEGRATION_META: dict[str, CrmIntegrationMeta] = {
    "amocrm": CrmIntegrationMeta(
        code="amocrm",
        display_name="amoCRM",
        docs_url=None,
        fields=(
            IntegrationField(
                name="crm_base_url",
                label="Base URL",
                field_type="url",
                required=True,
            ),
            IntegrationField(
                name="crm_api_token",
                label="API token",
                field_type="secret",
                required=True,
            ),
        ),
    ),
    "demo": CrmIntegrationMeta(
        code="demo",
        display_name="Без CRM (демо)",
        docs_url=None,
        fields=(),
    ),
    "none": CrmIntegrationMeta(
        code="none",
        display_name="Без внешней CRM",
        docs_url=None,
        fields=(),
    ),
    "generic_rest": CrmIntegrationMeta(
        code="generic_rest",
        display_name="Generic REST",
        docs_url=None,
        fields=(
            IntegrationField(
                name="crm_base_url",
                label="Base URL",
                field_type="url",
                required=True,
            ),
            IntegrationField(
                name="crm_api_token",
                label="API token",
                field_type="secret",
                required=True,
            ),
        ),
    ),
    "yclients": CrmIntegrationMeta(
        code="yclients",
        display_name="YClients",
        docs_url="https://developers.yclients.com/ru/",
        fields=(
            IntegrationField(
                name="crm_api_token",
                label="Partner token",
                field_type="secret",
                required=True,
            ),
            IntegrationField(
                name="company_id",
                label="Company ID",
                field_type="string",
                required=True,
            ),
            IntegrationField(
                name="default_service_id",
                label="Default service ID",
                field_type="string",
                required=False,
            ),
            IntegrationField(
                name="crm_user_token",
                label="User token",
                field_type="secret",
                required=False,
            ),
        ),
    ),
    "macdent": CrmIntegrationMeta(
        code="macdent",
        display_name="MacDent",
        docs_url=None,
        fields=(
            IntegrationField(
                name="crm_api_token",
                label="Access token",
                field_type="secret",
                required=True,
            ),
            IntegrationField(
                name="filial",
                label="Филиал (id из filial.find)",
                field_type="string",
                required=True,
            ),
        ),
    ),
}


def resolve_crm_provider_key(org: Organization) -> str:
    raw = (org.crm_provider or settings.crm_provider or "demo").strip().lower()
    if raw in ("", "demo", "none"):
        return "demo"
    if raw in CRM_REGISTRY:
        return raw
    logger.warning("Unknown crm_provider %r for org %s; using demo", raw, getattr(org, "id", "?"))
    return "demo"


def build_crm_provider(org: Organization) -> CRMProvider:
    key = resolve_crm_provider_key(org)
    factory = CRM_REGISTRY.get(key, _build_demo)
    return factory(org)


def list_crm_integration_meta() -> list[CrmIntegrationMeta]:
    return [
        CRM_INTEGRATION_META[key]
        for key in ("none", "macdent", "amocrm", "yclients", "generic_rest", "demo")
    ]


def crm_without_external_system(org: Organization) -> bool:
    raw = (org.crm_provider or "").strip().lower()
    return raw in ("", "none", "demo")


def crm_provider_is_demo_mode(org: Organization) -> bool:
    key = resolve_crm_provider_key(org)
    if key in ("demo",):
        return True
    if key == "amocrm":
        provider = _build_amocrm(org)
        return bool(getattr(provider, "demo_mode", False))
    if key == "generic_rest":
        provider = _build_generic_rest(org)
        return bool(getattr(provider, "demo_mode", False))
    if key == "yclients":
        provider = _build_yclients(org)
        return bool(getattr(provider, "demo_mode", False))
    if key == "macdent":
        provider = _build_macdent(org)
        return bool(getattr(provider, "demo_mode", False))
    return True
