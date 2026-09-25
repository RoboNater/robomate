"""A2A discovery metadata for the hub."""

from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentSkill,
    HTTPAuthSecurityScheme,
    SecurityScheme,
)


def build_agent_card(public_url: str) -> AgentCard:
    """Build the public A2A 0.3 agent card."""

    return AgentCard(
        name="Agent Comms Hub",
        description="Coordinates pull-based implementer and reviewer workers.",
        url=f"{public_url}/a2a",
        version="0.1.0",
        capabilities=AgentCapabilities(streaming=True),
        default_input_modes=["text/plain", "application/json"],
        default_output_modes=["text/plain", "application/json"],
        skills=[
            AgentSkill(
                id="worker-coordination",
                name="Worker coordination",
                description=(
                    "Registers workers and coordinates assignments, questions, "
                    "progress, and results."
                ),
                tags=["coordination", "software-development", "pull-model"],
            )
        ],
        security_schemes={
            "bearerAuth": SecurityScheme(
                root=HTTPAuthSecurityScheme(
                    scheme="bearer",
                    bearer_format="opaque",
                    description="Pre-shared token supplied in the Authorization header.",
                )
            )
        },
        security=[{"bearerAuth": []}],
    )
