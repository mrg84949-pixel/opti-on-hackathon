from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from bot.config import Settings, settings
from bot.db.models import Organization
from bot.services.org_secrets import SECRET_FIELDS, get_org_secret, secret_is_set, set_org_secret


@dataclass(frozen=True)
class TenantEnvField:
    org_field: str
    env_label: str
    get_env_value: Callable[[Settings], str]
    is_secret: bool = True


TENANT_ENV_FIELDS: tuple[TenantEnvField, ...] = (
    TenantEnvField("telegram_bot_token", "TELEGRAM_TOKEN", lambda s: s.telegram_token),
    TenantEnvField("whatsapp_instance_id", "GREEN_API_INSTANCE_ID", lambda s: s.green_api_instance_id),
    TenantEnvField("whatsapp_api_token", "GREEN_API_TOKEN", lambda s: s.green_api_token),
    TenantEnvField(
        "whatsapp_meta_phone_number_id",
        "WHATSAPP_PHONE_NUMBER_ID",
        lambda s: s.whatsapp_phone_number_id,
        is_secret=False,
    ),
    TenantEnvField(
        "whatsapp_meta_access_token",
        "WHATSAPP_GRAPH_ACCESS_TOKEN",
        lambda s: s.whatsapp_graph_access_token,
    ),
    TenantEnvField(
        "whatsapp_provider",
        "WHATSAPP_PROVIDER",
        lambda s: s.whatsapp_provider,
        is_secret=False,
    ),
    TenantEnvField("crm_base_url", "AMOCRM_BASE_URL", lambda s: s.amocrm_base_url, is_secret=False),
    TenantEnvField("crm_api_token", "AMOCRM_TOKEN", lambda s: s.amocrm_token),
    TenantEnvField("crm_provider", "CRM_PROVIDER", lambda s: s.crm_provider, is_secret=False),
)


def mask_secret(value: str) -> str:
    v = (value or "").strip()
    if not v:
        return "(empty)"
    if len(v) <= 4:
        return "****"
    return f"{v[:2]}...{v[-2:]}"


def format_field_value(value: str, *, is_secret: bool) -> str:
    if not (value or "").strip():
        return "(empty)"
    return mask_secret(value) if is_secret else value.strip()


@dataclass
class MigrationPlanItem:
    org_field: str
    env_label: str
    action: str  # copy | skip_db_set | skip_no_env
    display_value: str


def plan_tenant_env_migration(org: Organization, cfg: Settings | None = None) -> list[MigrationPlanItem]:
    cfg = cfg or settings
    items: list[MigrationPlanItem] = []
    for field in TENANT_ENV_FIELDS:
        org_val = (
            "set"
            if field.org_field in SECRET_FIELDS and secret_is_set(org, field.org_field)
            else str(getattr(org, field.org_field) or "").strip()
        )
        env_val = field.get_env_value(cfg).strip()
        if org_val:
            display_source = (
                get_org_secret(org, field.org_field) or ""
                if field.org_field in SECRET_FIELDS
                else org_val
            )
            items.append(
                MigrationPlanItem(
                    org_field=field.org_field,
                    env_label=field.env_label,
                    action="skip_db_set",
                    display_value=format_field_value(display_source, is_secret=field.is_secret),
                )
            )
            continue
        if not env_val:
            items.append(
                MigrationPlanItem(
                    org_field=field.org_field,
                    env_label=field.env_label,
                    action="skip_no_env",
                    display_value="(empty)",
                )
            )
            continue
        items.append(
            MigrationPlanItem(
                org_field=field.org_field,
                env_label=field.env_label,
                action="copy",
                display_value=format_field_value(env_val, is_secret=field.is_secret),
            )
        )
    return items


def apply_tenant_env_migration(org: Organization, cfg: Settings | None = None) -> list[MigrationPlanItem]:
    cfg = cfg or settings
    plan = plan_tenant_env_migration(org, cfg)
    for item in plan:
        if item.action != "copy":
            continue
        field = next(f for f in TENANT_ENV_FIELDS if f.org_field == item.org_field)
        env_value = field.get_env_value(cfg).strip()
        if field.org_field in SECRET_FIELDS:
            set_org_secret(org, field.org_field, env_value)
        else:
            setattr(org, field.org_field, env_value)
    return plan


_STARTUP_MISMATCH_ORG_FIELDS = frozenset(
    {
        "telegram_bot_token",
        "whatsapp_instance_id",
        "whatsapp_api_token",
        "whatsapp_meta_phone_number_id",
        "whatsapp_meta_access_token",
        "crm_base_url",
        "crm_api_token",
    }
)


def tenant_strict_env_org_mismatches(org: Organization, cfg: Settings | None = None) -> list[str]:
    """Env has tenant secret but org field empty — should migrate or configure org."""
    cfg = cfg or settings
    errors: list[str] = []
    for field in TENANT_ENV_FIELDS:
        if field.org_field not in _STARTUP_MISMATCH_ORG_FIELDS:
            continue
        env_val = field.get_env_value(cfg).strip()
        org_val = (
            "set"
            if field.org_field in SECRET_FIELDS and secret_is_set(org, field.org_field)
            else str(getattr(org, field.org_field) or "").strip()
        )
        if env_val and not org_val:
            errors.append(
                f"{field.env_label} is set in env but organizations.{field.org_field} is empty; "
                "run scripts/migrate_tenant_env_to_db.py or configure org in admin"
            )
    return errors
