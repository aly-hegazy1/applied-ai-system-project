"""AI Coach: an agentic, self-checking hint assistant for Glitchy Guesser."""

from coach.agent import CoachAgent, CoachResult
from coach.models import (
    ChaosCoachModel,
    ClaudeCoachModel,
    CoachModelError,
    GameState,
    OfflineCoachModel,
    default_model,
)

__all__ = [
    "ChaosCoachModel",
    "ClaudeCoachModel",
    "CoachAgent",
    "CoachModelError",
    "CoachResult",
    "GameState",
    "OfflineCoachModel",
    "default_model",
]
