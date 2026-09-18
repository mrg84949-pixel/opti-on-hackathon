from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bot.llm.providers.base import SessionSetup
from bot.llm.scenarios import BotScenario

_SCENARIO_COMPLETE_REPLY = "scenario complete"


def _find_tool(tools: list[Any], name: str):
    for fn in tools:
        if fn.__name__ == name:
            return fn
    raise KeyError(f"Tool not found: {name}")


@dataclass
class ScriptedScenarioSession:
    setup: SessionSetup
    scenario: BotScenario
    step_index: int = 0


class ScriptedScenarioProvider:
    """Test-only LLM provider: runs predefined tool calls per scenario turn."""

    provider_name = "scripted"

    def __init__(self, scenario: BotScenario):
        self.scenario = scenario
        self._turn_index = 0

    def credentials_configured(self) -> bool:
        return True

    def missing_credentials_message(self) -> str:
        return "ScriptedScenarioProvider does not require credentials."

    async def create_session(self, setup: SessionSetup) -> ScriptedScenarioSession:
        return ScriptedScenarioSession(
            setup=setup,
            scenario=self.scenario,
            step_index=self._turn_index,
        )

    async def send_turn(self, session: ScriptedScenarioSession, user_text: str) -> str:
        _ = user_text
        if session.step_index >= len(session.scenario.turns):
            return _SCENARIO_COMPLETE_REPLY

        turn = session.scenario.turns[session.step_index]
        parts: list[str] = []
        for action in turn.actions:
            tool = _find_tool(session.setup.tools, action.tool)
            result = await tool(**action.kwargs)
            parts.append(str(result))
        session.step_index += 1
        self._turn_index = session.step_index
        return "\n\n".join(parts)

    def user_message_for_error(self, exc: Exception) -> str:
        return str(exc)
