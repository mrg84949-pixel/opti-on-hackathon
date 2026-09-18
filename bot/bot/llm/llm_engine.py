from dataclasses import dataclass

from bot.billing_access import frozen_reply_for_org, org_subscription_active
from bot.channels.phone import channel_phone
from bot.config import settings
from bot.db.database import AsyncSessionLocal
from bot.db.models import BotInteractionLog, Organization
from bot.debug_log import debug_log
from bot.llm.client_messages import AI_CONFIG_ERROR, AI_UNAVAILABLE, sanitize_client_reply
from bot.llm import booking_fsm
from bot.llm.context import TurnContext
from bot.llm.context_summary import get_context_summary
from bot.llm.prompts import DEFAULT_DENTISTRY_SYSTEM, MANAGE_APPOINTMENT_SYSTEM
from bot.llm.providers.base import SessionSetup
from bot.llm.providers.factory import get_llm_provider
from bot.llm.providers.registry import runtime_fingerprint
from bot.llm.tools import (
    DIALOG_MODE_MANAGE,
    get_or_create_customer_for_channel,
    load_services_catalog_for_org,
    make_tools,
    resolve_tool_mode,
)
from bot.logging_config import get_logger
from bot.services import booking_deterministic, client_change_intent, customer_service
from bot.services.bot_branding import build_org_system_instruction

logger = get_logger(__name__)


@dataclass
class _UserSession:
    handle: object
    provider_name: str
    runtime_fingerprint: str = ""
    tool_mode: str = "booking"


_sessions: dict[str, _UserSession] = {}


def _session_key(channel: str, user_id: str, org_id: int) -> str:
    return f"{channel}:{org_id}:{user_id or 'guest'}"


def clear_in_memory_session(*, channel: str, user_id: str, org_id: int) -> None:
    """Drop provider chat handle so the next turn starts a fresh LLM session."""
    _sessions.pop(_session_key(channel, user_id, org_id), None)


def _clip_text(s: str, n: int = 480) -> str:
    t = (s or "").strip().replace("\n", " ")
    if len(t) <= n:
        return t
    return t[: n - 1] + "…"


def _reply_to_client(text: str) -> str:
    return sanitize_client_reply(text)


async def _persist_interaction_log(
    *,
    org_id: int,
    channel: str,
    external_user_id: str,
    user_text: str,
    reply: str,
    status: str,
    error_hint: str | None = None,
) -> None:
    try:
        async with AsyncSessionLocal() as session:
            session.add(
                BotInteractionLog(
                    org_id=org_id,
                    channel=channel[:32],
                    external_user_id=(external_user_id or "")[:255],
                    user_message_preview=_clip_text(user_text, 500),
                    reply_preview=_clip_text(reply, 500),
                    status=status[:32],
                    error_hint=error_hint,
                )
            )
            await session.commit()
    except Exception:
        logger.exception(
            "Failed to persist bot interaction log",
            extra={"extra_data": {"event": "bot_interaction_log_error", "org_id": org_id}},
        )


async def _build_session_setup(
    *,
    target_org_id: int,
    channel: str,
    user_id: str,
) -> SessionSetup:
    async with AsyncSessionLocal() as session:
        org = await session.get(Organization, target_org_id)
        if org is None:
            raise ValueError(f"org_missing:{target_org_id}")
        phone = channel_phone(channel, user_id)
        customer = await get_or_create_customer_for_channel(
            session, org_id=target_org_id, channel_phone=phone
        )
        catalog = await load_services_catalog_for_org(session, target_org_id)
        tool_mode = await resolve_tool_mode(session, target_org_id, customer)

    ctx = TurnContext(org_id=target_org_id, customer_id=customer.id, services_catalog=catalog)
    manage_addon = MANAGE_APPOINTMENT_SYSTEM if tool_mode == DIALOG_MODE_MANAGE else ""
    summary = get_context_summary(customer.dialog_context) or ""
    sys_instr = build_org_system_instruction(
        org,
        base=DEFAULT_DENTISTRY_SYSTEM,
        manage_addon=manage_addon,
        context_summary=summary,
    )
    tools = make_tools(ctx, mode=tool_mode)
    return SessionSetup(
        system_instruction=sys_instr,
        tools=tools,
        ctx=ctx,
        channel=channel,
        tool_mode=tool_mode,
    )


async def get_ai_response(
    user_id: str,
    user_text: str,
    db_memory: dict | None = None,
    *,
    channel: str = "web",
    org_id: int | None = None,
) -> str:
    """Ответ LLM с инструментами и учётом организации/клиента. db_memory оставлен для совместимости."""
    _ = db_memory
    debug_log(
        run_id="audit-pre",
        hypothesis_id="H1",
        location="bot/llm/llm_engine.py:get_ai_response:entry",
        message="LLM request received",
        data={"channel": channel, "has_user_id": bool(user_id), "text_len": len(user_text or "")},
    )

    target_org_id = org_id if org_id is not None else settings.default_org_id
    key = _session_key(channel, user_id, target_org_id)
    provider = None
    ai_runtime_fingerprint = ""

    async with AsyncSessionLocal() as session:
        org = await session.get(Organization, target_org_id)
        if org is None:
            debug_log(
                run_id="audit-pre",
                hypothesis_id="H3",
                location="bot/llm/llm_engine.py:get_ai_response:org_missing",
                message="Organization not found for DEFAULT_ORG_ID",
                data={"org_id": target_org_id},
            )
            logger.error(
                "Organization not found for client ingress",
                extra={
                    "extra_data": {
                        "event": "org_missing",
                        "org_id": target_org_id,
                        "hint": "Check organizations table or DEFAULT_ORG_ID in .env",
                    }
                },
            )
            return _reply_to_client(AI_CONFIG_ERROR)
        frozen = frozen_reply_for_org(org)
        if frozen is not None:
            status = "billing_blocked" if not org_subscription_active(org) else "bot_paused"
            await _persist_interaction_log(
                org_id=org.id,
                channel=channel,
                external_user_id=user_id,
                user_text=user_text,
                reply=frozen,
                status=status,
            )
            return frozen

        provider = get_llm_provider(org)
        ai_runtime_fingerprint = runtime_fingerprint(org)
        phone = channel_phone(channel, user_id)
        customer = await get_or_create_customer_for_channel(
            session, org_id=target_org_id, channel_phone=phone
        )
        if await customer_service.maybe_auto_mute(session, customer):
            await session.commit()
        if customer_service.is_customer_muted(customer):
            await _persist_interaction_log(
                org_id=target_org_id,
                channel=channel,
                external_user_id=user_id,
                user_text=user_text,
                reply=customer_service.MUTE_USER_MESSAGE,
                status="muted",
            )
            return customer_service.MUTE_USER_MESSAGE

    intent_reply = await client_change_intent.try_handle_client_change_reply(
        org_id=target_org_id,
        channel=channel,
        user_id=user_id,
        user_text=user_text,
    )
    if intent_reply is not None:
        await _persist_interaction_log(
            org_id=target_org_id,
            channel=channel,
            external_user_id=user_id,
            user_text=user_text,
            reply=intent_reply,
            status="client_change_intent",
        )
        return intent_reply

    shortcut = await booking_deterministic.try_handle_booking_shortcut(
        org_id=target_org_id,
        channel=channel,
        user_id=user_id,
        user_text=user_text,
    )
    if shortcut is not None:
        shortcut_reply, shortcut_status = shortcut
        await _persist_interaction_log(
            org_id=target_org_id,
            channel=channel,
            external_user_id=user_id,
            user_text=user_text,
            reply=shortcut_reply,
            status=shortcut_status,
        )
        return shortcut_reply

    fsm = await booking_fsm.try_handle_booking_fsm(
        org_id=target_org_id,
        channel=channel,
        user_id=user_id,
        user_text=user_text,
    )
    if fsm is not None:
        fsm_reply, fsm_status = fsm
        await _persist_interaction_log(
            org_id=target_org_id,
            channel=channel,
            external_user_id=user_id,
            user_text=user_text,
            reply=fsm_reply,
            status=fsm_status,
        )
        return fsm_reply

    if provider.provider_name == "stub":
        debug_log(
            run_id="audit-pre",
            hypothesis_id="H1",
            location="bot/llm/llm_engine.py:get_ai_response:stub",
            message="Stub mode branch used",
            data={"stub_mode": True},
        )
        reply = _reply_to_client(await provider.send_turn(object(), user_text))
        await _persist_interaction_log(
            org_id=target_org_id,
            channel=channel,
            external_user_id=user_id,
            user_text=user_text,
            reply=reply,
            status="stub",
        )
        return reply

    if not provider.credentials_configured():
        debug_log(
            run_id="audit-pre",
            hypothesis_id="H2",
            location="bot/llm/llm_engine.py:get_ai_response:no_key",
            message="LLM provider key missing",
            data={"provider": provider.provider_name},
        )
        logger.warning(
            "LLM credentials missing for client ingress",
            extra={
                "extra_data": {
                    "event": "llm_credentials_missing",
                    "provider": provider.provider_name,
                    "detail": provider.missing_credentials_message(),
                }
            },
        )
        return _reply_to_client(AI_UNAVAILABLE)

    need_new_session = (
        key not in _sessions
        or _sessions[key].provider_name != provider.provider_name
        or _sessions[key].runtime_fingerprint != ai_runtime_fingerprint
    )
    if not need_new_session:
        try:
            fresh_setup = await _build_session_setup(
                target_org_id=target_org_id,
                channel=channel,
                user_id=user_id,
            )
        except ValueError:
            fresh_setup = None
        if fresh_setup is not None:
            fresh_mode = fresh_setup.tool_mode
            if _sessions[key].tool_mode != fresh_mode:
                need_new_session = True

    if need_new_session:
        try:
            setup = await _build_session_setup(
                target_org_id=target_org_id,
                channel=channel,
                user_id=user_id,
            )
        except ValueError as exc:
            if str(exc).startswith("org_missing:"):
                missing_id = str(exc).split(":", 1)[1]
                logger.error(
                    "Organization not found while building LLM session",
                    extra={
                        "extra_data": {
                            "event": "org_missing",
                            "org_id": missing_id,
                            "hint": "Check organizations table or DEFAULT_ORG_ID in .env",
                        }
                    },
                )
                return _reply_to_client(AI_CONFIG_ERROR)
            raise
        handle = await provider.create_session(setup)
        tool_mode = setup.tool_mode
        _sessions[key] = _UserSession(
            handle=handle,
            provider_name=provider.provider_name,
            runtime_fingerprint=ai_runtime_fingerprint,
            tool_mode=tool_mode,
        )
        debug_log(
            run_id="audit-pre",
            hypothesis_id="H4",
            location="bot/llm/llm_engine.py:get_ai_response:new_session",
            message="New chat session initialized",
            data={"channel": channel, "provider": provider.provider_name},
        )

    sess = _sessions[key]
    try:
        raw_out = await provider.send_turn(sess.handle, user_text)
    except Exception as exc:
        debug_log(
            run_id="audit-pre",
            hypothesis_id="H6",
            location="bot/llm/llm_engine.py:get_ai_response:send_error",
            message="send_turn raised error",
            data={"error_type": type(exc).__name__, "error_text": str(exc)[:220]},
        )
        err_user = _reply_to_client(provider.user_message_for_error(exc))
        await _persist_interaction_log(
            org_id=target_org_id,
            channel=channel,
            external_user_id=user_id,
            user_text=user_text,
            reply=err_user,
            status="error",
            error_hint=str(exc)[:4000],
        )
        return err_user

    out = _reply_to_client(raw_out)
    if out == AI_UNAVAILABLE and raw_out.strip():
        debug_log(
            run_id="prod-launch",
            hypothesis_id="H7",
            location="bot/llm/llm_engine.py:get_ai_response:sanitized_to_unavailable",
            message="Client-safe filter replaced a non-empty raw reply with AI_UNAVAILABLE",
            data={"raw_reply": raw_out[:500]},
        )
    await _persist_interaction_log(
        org_id=target_org_id,
        channel=channel,
        external_user_id=user_id,
        user_text=user_text,
        reply=out,
        status="ok",
    )
    return out
