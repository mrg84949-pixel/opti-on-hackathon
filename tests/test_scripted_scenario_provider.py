from __future__ import annotations

import pytest

from bot.llm.context import TurnContext
from bot.llm.providers.base import SessionSetup
from bot.llm.providers.scripted_scenario import ScriptedScenarioProvider
from bot.llm.scenarios import GET_SERVICES


@pytest.mark.asyncio
async def test_turn_index_persists_across_session_rebuilds():
    async def get_services_info() -> str:
        return "catalog"

    provider = ScriptedScenarioProvider(GET_SERVICES)
    setup = SessionSetup(
        system_instruction="",
        tools=[get_services_info],
        ctx=TurnContext(org_id=1, customer_id=10, services_catalog=""),
        tool_mode="booking",
    )

    first = await provider.create_session(setup)
    await provider.send_turn(first, "Какие услуги?")
    assert provider._turn_index == 1

    rebuilt = await provider.create_session(setup)
    assert rebuilt.step_index == 1
