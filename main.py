from contextlib import asynccontextmanager
from time import perf_counter

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
import asyncio
import bot.llm.llm_engine as llm_engine

from bot.api.macdent_webhook import router as macdent_webhook_router
from bot.api.telegram import router as telegram_router
from bot.api.whatsapp import router as whatsapp_router
from bot.channels.voice.twilio_voice import router as voice_router
from bot.automation import start_scheduler, stop_scheduler
from bot.config import settings
from bot.db import init_db
from bot.db.database import AsyncSessionLocal
from bot.db.models import Organization
from bot.services.rate_limit import RateLimitExceeded, client_ip, get_limiter
from bot.services.secret_encryption import tenant_secrets_master_key_errors
from bot.services.tenant_env_migration import tenant_strict_env_org_mismatches
from bot.logging_config import configure_logging, get_logger, new_request_id, request_id_ctx
from sqlalchemy import text as sql_text
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest  # pyright: ignore[reportMissingImports]
from web.user_auth import auth_secret_configured
from web.admin_api import router as web_router
from web.contact_api import router as contact_router
from web.user_api import router as user_router


configure_logging(settings.log_level)
logger = get_logger(__name__)
http_requests_total = Counter(
    "bot_http_requests_total",
    "Total HTTP requests",
    ["method", "path", "status"],
)
http_request_duration_seconds = Histogram(
    "bot_http_request_duration_seconds",
    "HTTP request duration in seconds",
    ["method", "path"],
)
http_request_errors_total = Counter(
    "bot_http_request_errors_total",
    "Total HTTP request errors",
    ["method", "path", "error_type"],
)


def _runtime_config_errors() -> list[str]:
    errors: list[str] = []
    if settings.default_org_id <= 0:
        errors.append("DEFAULT_ORG_ID must be a positive integer")
    if not auth_secret_configured():
        errors.append("USER_AUTH_SECRET must be configured and not use the default placeholder")
    if (
        (settings.telegram_token or "").strip()
        and (settings.telegram_webhook_url or "").strip()
        and not (settings.telegram_webhook_secret or "").strip()
    ):
        errors.append("TELEGRAM_WEBHOOK_SECRET should be set when using Telegram webhook")
    if (settings.whatsapp_verify_token or "").strip() and not (settings.whatsapp_app_secret or "").strip():
        errors.append(
            "WHATSAPP_APP_SECRET should be set when Meta WhatsApp webhook is configured (WHATSAPP_VERIFY_TOKEN)"
        )
    errors.extend(tenant_secrets_master_key_errors())
    return errors


async def _tenant_strict_config_errors() -> list[str]:
    if not settings.tenant_config_strict:
        return []
    async with AsyncSessionLocal() as session:
        org = await session.get(Organization, settings.default_org_id)
        if org is None:
            return [f"Organization id={settings.default_org_id} not found for tenant strict check"]
        return tenant_strict_env_org_mismatches(org)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Application startup", extra={"extra_data": {"event": "startup"}})
    config_errors = _runtime_config_errors()
    if config_errors:
        logger.warning(
            "Runtime configuration issues detected",
            extra={"extra_data": {"event": "runtime_config_warning", "errors": config_errors}},
        )
        if settings.strict_startup_validation:
            raise RuntimeError("; ".join(config_errors))
    await init_db()
    strict_errors = await _tenant_strict_config_errors()
    if strict_errors:
        logger.warning(
            "Tenant strict configuration issues detected",
            extra={"extra_data": {"event": "tenant_strict_config_warning", "errors": strict_errors}},
        )
        if settings.strict_startup_validation:
            raise RuntimeError("; ".join(strict_errors))
    start_scheduler()
    yield
    stop_scheduler()
    logger.info("Application shutdown", extra={"extra_data": {"event": "shutdown"}})


app = FastAPI(title="AI Business Bot API", lifespan=lifespan)


@app.exception_handler(RateLimitExceeded)
async def rate_limit_exception_handler(_request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=429,
        content={"detail": "Too many requests", "retry_after": exc.retry_after},
        headers={"Retry-After": str(exc.retry_after)},
    )


app.include_router(router=telegram_router,prefix="/bot",tags=["Telegram Bot"])
app.include_router(router=whatsapp_router,prefix="/bot",tags=["WhatsApp Bot"])
app.include_router(router=voice_router,prefix="/bot",tags=["Voice (Twilio)"])
app.include_router(router=macdent_webhook_router,prefix="/bot",tags=["MacDent CRM"])
app.include_router(router=web_router,prefix="/api/web",tags=["Website Admin Panel"])
app.include_router(router=contact_router,prefix="/api/web",tags=["Public Website"])
app.include_router(router=user_router,prefix="/api/web/user",tags=["Website User API"])


def _middleware_rate_limit_rule(method: str, path: str) -> tuple[str, int, int] | None:
    if method != "POST":
        return None
    if path == "/bot/webhook":
        return ("webhook", settings.rate_limit_webhook_ip_limit, 60)
    if path in ("/bot/whatsapp/webhook", "/bot/whatsapp/meta"):
        return ("webhook", settings.rate_limit_webhook_ip_limit, 60)
    if path == "/api/web/contact":
        return ("contact", settings.rate_limit_contact_ip_limit, 3600)
    return None


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    if settings.rate_limit_enabled:
        rule = _middleware_rate_limit_rule(request.method, request.url.path)
        if rule is not None:
            prefix, limit, window = rule
            ip = client_ip(request)
            retry_after = await get_limiter().check(
                f"{prefix}:ip:{ip}",
                limit=limit,
                window_seconds=window,
            )
            if retry_after is not None:
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Too many requests", "retry_after": retry_after},
                    headers={"Retry-After": str(retry_after)},
                )
    return await call_next(request)


@app.middleware("http")
async def request_context_middleware(request: Request, call_next):
    started = perf_counter()
    path = request.url.path
    method = request.method
    request_id = request.headers.get("x-request-id") or new_request_id()
    token = request_id_ctx.set(request_id)
    try:
        response = await call_next(request)
    except Exception as exc:
        duration = perf_counter() - started
        http_request_errors_total.labels(method=method, path=path, error_type=type(exc).__name__).inc()
        http_requests_total.labels(method=method, path=path, status="500").inc()
        http_request_duration_seconds.labels(method=method, path=path).observe(duration)
        logger.exception(
            "Unhandled request error",
            extra={
                "extra_data": {
                    "event": "http_request_error",
                    "method": method,
                    "path": path,
                    "duration_ms": round(duration * 1000, 2),
                    "error_type": type(exc).__name__,
                }
            },
        )
        raise

    duration = perf_counter() - started
    status_code = response.status_code
    http_requests_total.labels(method=method, path=path, status=str(status_code)).inc()
    http_request_duration_seconds.labels(method=method, path=path).observe(duration)
    logger.info(
        "HTTP request completed",
        extra={
            "extra_data": {
                "event": "http_request",
                "method": method,
                "path": path,
                "status_code": status_code,
                "duration_ms": round(duration * 1000, 2),
            }
        },
    )
    response.headers["x-request-id"] = request_id
    request_id_ctx.reset(token)
    return response

class ChatRequest(BaseModel):
    user_message: str
    client_id: str = "web-guest"
    org_id: int | None = Field(default=None, ge=1)


def _resolve_web_chat_org_id(request_org_id: int | None) -> int:
    if request_org_id is not None:
        return request_org_id
    if not settings.tenant_config_strict:
        return settings.default_org_id
    raise HTTPException(
        status_code=400,
        detail="org_id is required when TENANT_CONFIG_STRICT is enabled",
    )


async def _require_web_chat_org(org_id: int) -> Organization:
    async with AsyncSessionLocal() as session:
        org = await session.get(Organization, org_id)
    if org is None:
        raise HTTPException(status_code=404, detail="organization not found")
    return org


@app.get("/")
async def root():
    return {"message": "Сервер работает. Для чата используйте POST /chat"}

@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/readyz")
async def readyz():
    errors = _runtime_config_errors()
    if errors:
        return JSONResponse(
            status_code=503,
            content={"status": "error", "checks": {"config": "failed"}, "errors": errors},
        )
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(sql_text("SELECT 1"))
    except Exception as exc:
        return JSONResponse(
            status_code=503,
            content={
                "status": "error",
                "checks": {"config": "ok", "database": "failed"},
                "errors": [f"database check failed: {type(exc).__name__}"],
            },
        )
    return {"status": "ok", "checks": {"config": "ok", "database": "ok"}}


@app.get("/metrics")
async def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/chat")
async def chat_with_bot(request: ChatRequest):
    incoming_text = request.user_message
    target_org_id = _resolve_web_chat_org_id(request.org_id)
    await _require_web_chat_org(target_org_id)
    logger.info(
        "Web chat request received",
        extra={
            "extra_data": {
                "event": "chat_request",
                "client_id": request.client_id,
                "org_id": target_org_id,
                "text_len": len(incoming_text),
            }
        },
    )
    try:
        ai_response = await llm_engine.get_ai_response(
            user_id=request.client_id,
            user_text=incoming_text,
            db_memory={},
            channel="web",
            org_id=target_org_id,
        )
        return {
            "status": "success",
            "reply": ai_response
        }
    except Exception as exc:
        logger.exception(
            "Web chat request failed",
            extra={"extra_data": {"event": "chat_error", "error_type": type(exc).__name__}},
        )
        raise


def main():
    response = asyncio.run(
        llm_engine.get_ai_response(user_id="cli", user_text="", db_memory={}, channel="web")
    )
    logger.info("CLI response generated", extra={"extra_data": {"event": "cli_main", "has_text": bool(response)}})


if __name__ == "__main__":
    main()