"""Human handoff orchestration shared by LLM tools and deterministic paths."""

from __future__ import annotations

from bot.db.database import AsyncSessionLocal
from bot.notifications import notify_admins_about_human_transfer
from bot.services import customer_service


def render_handoff_assistant_text() -> str:
    """LLM tool return — instructions for the assistant, not for the client."""
    return (
        "Запрос передан администратору. Сообщите клиенту, что с ним свяжется "
        "сотрудник компании в ближайшее время."
    )


async def request_human_handoff(*, org_id: int, customer_id: int | None) -> None:
    """Mute client and notify admins when handoff is requested."""
    if customer_id is None:
        return
    async with AsyncSessionLocal() as session:
        await customer_service.mute_for_human_handoff(session, customer_id, org_id)
        await session.commit()
    await notify_admins_about_human_transfer(org_id=org_id, customer_id=customer_id)
