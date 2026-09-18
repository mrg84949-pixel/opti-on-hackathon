## Product Context (READ FIRST)

Before implementing any feature, read `docs/PRODUCT_SPEC.md` — it contains:
- Full functional map with statuses (what's done, what's TODO)
- Business rules that MUST be followed
- Bot states, tool functions, and the appointment lifecycle
- Priority order for remaining MVP work

For **tenant data security, config, CRM integrations, and post-MVP cohesion**, read `docs/PRODUCT_LAYERS_PLAN.md` — **T1 (data isolation) before UI polish**; client secrets in DB per org, encrypted in prod, never env fallback; no `default_org_id` in webhook/outbound paths; new CRMs only via `CRMProvider` + registry — see `docs/crm-provider-guide.md`; golden owner scenario — `docs/cohesion-golden-path.md`.

## Architecture Rules (MUST follow)

### Backend (Python/FastAPI)

1. **Configuration**: All env vars go through `bot/config.py` (Pydantic Settings singleton). Never use `os.getenv()` directly.
2. **Imports**: `from bot.config import settings` — then `settings.field_name`.
3. **No `load_dotenv()`** anywhere except `alembic/env.py` (Pydantic handles .env loading).
4. **Database access**: Use `AsyncSessionLocal()` context manager. Never create raw engine connections.
5. **Business logic** belongs in `bot/services/` (when created) — NOT in route handlers.
6. **Route handlers** should be thin: parse request → call service → format response.
7. **LLM tools** (`bot/llm/tools/`) should call services, not execute DB queries directly.
8. **Tests** that need different config values: use `monkeypatch.setenv("FIELD_NAME", "value")` — the `tests/conftest.py` auto-syncs to `settings`.

### Frontend (Next.js 16 / React 19)

1. **Server Components** (RSC) call `lib/api.ts` directly.
2. **Client Components** (`"use client"`) call `/api/*` route handlers (BFF pattern). Never import `lib/api.ts` from client components.
3. **New pages** in authenticated area go under `app/(shell)/`.
4. **Shared components** → `components/`. UI primitives → `components/ui/`.
5. **i18n strings** → `lib/site-dictionary.ts`.

### General

- Run `python3 -m pytest tests/` before committing backend changes.
- Run `python scripts/preflight_check.py --quick` before release slices.
- Run `npm run lint` (in `frontend-bot/`) before committing frontend changes.
- Never commit `os.getenv()` calls in Python — use `settings.*` from `bot/config.py`.
- When adding new bot tools: add to `bot/llm/tools.py`, update `docs/PRODUCT_SPEC.md` status.
- When adding LLM providers: implement in `bot/llm/providers/`, register in `factory.py`, add config fields to `bot/config.py`.
- When adding CRM providers: implement `CRMProvider`, register in `bot/crm/registry.py` — follow `docs/crm-provider-guide.md`.
- Telegram bot token UI: **Бот → Каналы → Telegram** (`/bot/channels?section=telegram`), not `/bot/integrations`; test via `POST /org-integrations/test-telegram`.
- Self-service org lifecycle: register → `/subscribe` → sandbox checkout `optibot-trial` → `confirm_checkout` provisions org+admin → `/my-requests` → `POST /user/auth/admin-handoff` → `/cabinet` — see `docs/dev-runbook.md` § T4.1–T4.2.
- Ops org lifecycle (manual onboarding): `POST /api/web/organizations` (Bearer `ADMIN_API_TOKEN` only, no `owner_user_id`) — see `docs/dev-runbook.md` § T4.3–T4.4.
- Default prod provider remains `AI_PROVIDER=gemini`; use `groq` for local/TG dev tests.
- Bot scenario autotests (D6 / D6.1): test-only `ScriptedScenarioProvider` in `bot/llm/providers/scripted_scenario.py` — **not** registered in `factory.py`; run `python scripts/bot_scenario_verify.py` (booking, manage cancel/reschedule multi-turn).
- Bot customization (D7): per-org `bot_display_name`, `bot_welcome_message`, `bot_tone` on `organizations`; UI `/bot/settings`; helpers in `bot/services/bot_branding.py`; TG `/start` uses welcome without LLM.
- When tools.py exceeds ~15 functions: split into `bot/llm/tools/` submodules (see `docs/REFACTORING_PLAN.md` Stage 3).

### Business Rules (enforce in code)

- Appointments require: customer_id, service, time. Never create without all three.
- Admin confirms every appointment unless `org.auto_confirm_appointments` is true (then bot sets CONFIRMED on booking).
- Bot must NOT discuss off-topic — use system prompt + tool-only approach.
- Reminders are template messages (no AI) — sent 24h and 2h before; skip when `customers.disable_reminders` (bot tool) or `customers.muted_until` is active (handoff / admin / no-show mute). Mute also blocks LLM replies and retention follow-ups.
- Retention follow-up: scheduler job N days after `appointments.completed_at`; configured in `/bot/settings`; also respects `disable_reminders` and mute.
- Revenue KPI (`/stats`): sum `appointments.service_price_minor` for COMPLETED in last 30d; price parsed from org catalog `price_label` at booking and stored on appointment (`service_name` snapshot).
- Post-complete message blocks (static, not LLM): optional care advice from matching org service (`care_message`) and org upsell text (`post_service_upsell_message`) appended in `render_complete_text` on admin complete.
- Context compression: after appointment is created, summarize chat and store in DB.
- Billing gate: if `org.billing_paid_until` < now → bot returns TARIFF_BLOCKED_MESSAGE.

## Cursor Cloud specific instructions

### Architecture

- **Backend**: Python FastAPI app (`main.py`) served by uvicorn on port 8000.
- **Frontend**: Next.js 16 app in `frontend-bot/` on port 3000.
- **Database**: PostgreSQL 16 via `docker-compose.yml` (container: `bot_saas_postgres`).

### Starting services

1. **PostgreSQL**: `docker compose up -d` (from repo root). Ensure Docker daemon is running first.
2. **Backend**: `uvicorn main:app --reload --host 0.0.0.0 --port 8000` (from repo root).
3. **Frontend**: `npm run dev` (from `frontend-bot/`).

### Environment files

- Backend: `.env` at repo root (see `.env.example`). Minimum required: `DATABASE_URL`, `USER_AUTH_SECRET`.
- Frontend: `frontend-bot/.env`. Required: `BACKEND_API_BASE_URL`, `ADMIN_API_TOKEN`, `ADMIN_UI_TOKEN`, `DEFAULT_ORG_ID`.
- Set `AI_USE_STUB=1` or `AI_PROVIDER=stub` to skip live LLM API calls in dev/test.

### Database migrations

Run `alembic upgrade head` from repo root after starting PostgreSQL.

### Running tests

```
Bash
python3 -m pytest tests/
```
Tests mock DB/API calls and do not require external services. All 119 tests should pass.
### Linting
```
Bash
cd frontend-bot && npm run lint
```
### Gotchas
- Python packages install to ~/.local/bin — ensure it's on PATH (export PATH="$HOME/.local/bin:$PATH").
- Docker requires fuse-overlayfs storage driver and iptables-legacy in the Cloud Agent VM (nested container environment).
- The backend warns at startup if USER_AUTH_SECRET is not set but continues unless STRICT_STARTUP_VALIDATION=1 (enabled by default in `docker-compose.prod.yml`).
- Rate limits: `RATE_LIMIT_ENABLED` (default true), per-path limits in `bot/config.py`; in-memory per uvicorn process.
- pytest-asyncio version pinned at 1.2.0 uses asyncio_mode = auto in pytest.ini.
- tests/conftest.py auto-syncs monkeypatch.setenv() calls to the settings singleton.