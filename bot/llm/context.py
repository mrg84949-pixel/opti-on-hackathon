from dataclasses import dataclass, field


@dataclass
class TurnContext:
    """Изменяемый контекст на время диалога (инструменты читают актуальные поля)."""

    org_id: int
    customer_id: int | None = None
    services_catalog: str = field(default="")
    human_transfer_requested: bool = False
