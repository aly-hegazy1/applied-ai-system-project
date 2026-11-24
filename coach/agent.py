"""The AI Coach agent: plan, act, check, and retry.

One call to `CoachAgent.suggest()` runs this loop:

  1. PLAN   Work out which secrets are still possible from the hints. If no
            number fits every hint, the game is lying: report a glitch
            instead of guessing.
  2. ACT    Ask the model for a guess (it sees the hints, never the secret).
  3. CHECK  Validate that guess deterministically. If it is rejected, send
            the model the reason and go back to ACT, up to `max_attempts`.
  4. GUARD  If the model never produces a valid guess, or errors out, fall
            back to the provably safe binary-search midpoint.

Every step is appended to `CoachResult.trace` and written to the log, so a
human can audit exactly why the coach said what it said.
"""

from dataclasses import dataclass, field
import logging

from coach.models import CoachModelError, GameState, Rejection
from coach.validator import (
    feasible_interval,
    guesses_needed,
    validate_suggestion,
)

logger = logging.getLogger("coach.agent")


@dataclass
class CoachResult:
    guess: object  # int, or None when a glitch was detected
    reasoning: str
    confidence: object  # float from the model, or None for fallback/glitch
    source: str  # "model", "fallback", or "glitch"
    model_name: str
    interval: tuple  # (low, high) still possible; (low, low-1) if empty
    model_calls: int = 0
    rejections: list = field(default_factory=list)
    trace: list = field(default_factory=list)
    can_guarantee_win: bool = False

    @property
    def glitch_detected(self) -> bool:
        return self.source == "glitch"

    @property
    def first_try_valid(self) -> bool:
        return self.source == "model" and not self.rejections


class CoachAgent:
    def __init__(self, model, max_attempts: int = 3):
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self.model = model
        self.max_attempts = max_attempts

    def suggest(self, state: GameState) -> CoachResult:
        if state.low > state.high:
            raise ValueError(f"Invalid range {state.low}..{state.high}")

        trace = []

        def step(message: str):
            trace.append(message)
            logger.info(message)

        # ---- 1. PLAN -----------------------------------------------------
        interval = feasible_interval(state.low, state.high, state.feedback)
        step(
            f"PLAN: range {state.low}-{state.high}, {len(state.feedback)} hint(s), "
            f"{state.attempts_left} attempt(s) left -> secret is in "
            f"{interval.low}-{interval.high} ({interval.size} possible)"
        )
        can_win = guesses_needed(interval.size) <= state.attempts_left

        if interval.is_empty:
            step("GLITCH: no number is consistent with every hint; refusing to guess")
            return CoachResult(
                guess=None,
                reasoning=(
                    "These hints contradict each other - no secret could produce "
                    "all of them. The game's feedback is wrong, so any guess "
                    "would be a coin flip."
                ),
                confidence=None,
                source="glitch",
                model_name=self.model.name,
                interval=(interval.low, interval.high),
                trace=trace,
            )

        # ---- 2-3. ACT and CHECK, with retries ----------------------------
        rejections = []
        calls = 0
        for attempt in range(1, self.max_attempts + 1):
            calls += 1
            try:
                suggestion = self.model.propose(state, list(rejections))
            except CoachModelError as exc:
                step(f"ACT #{attempt}: {self.model.name} failed: {exc}")
                break  # a failing backend will not heal by asking again

            step(
                f"ACT #{attempt}: {self.model.name} proposed {suggestion.guess!r} "
                f"(confidence {suggestion.confidence:.2f})"
            )
            check = validate_suggestion(suggestion.guess, interval, state.guessed)
            if check.ok:
                step(f"CHECK #{attempt}: accepted - {check.reason}")
                return CoachResult(
                    guess=suggestion.guess,
                    reasoning=suggestion.reasoning,
                    confidence=suggestion.confidence,
                    source="model",
                    model_name=self.model.name,
                    interval=(interval.low, interval.high),
                    model_calls=calls,
                    rejections=rejections,
                    trace=trace,
                    can_guarantee_win=can_win,
                )
            step(f"CHECK #{attempt}: rejected - {check.reason}")
            logger.warning("Coach suggestion rejected: %s", check.reason)
            rejections.append(Rejection(suggestion.guess, check.reason))

        # ---- 4. GUARD: deterministic fallback ----------------------------
        guess = interval.midpoint
        step(f"FALLBACK: using binary-search midpoint {guess}")
        logger.warning("Coach fell back to midpoint after %d model call(s)", calls)
        return CoachResult(
            guess=guess,
            reasoning=(
                f"The AI's suggestion could not be verified, so this is the safe "
                f"default: {guess} is the midpoint of the {interval.low}-"
                f"{interval.high} range that is still possible."
            ),
            confidence=None,
            source="fallback",
            model_name=self.model.name,
            interval=(interval.low, interval.high),
            model_calls=calls,
            rejections=rejections,
            trace=trace,
            can_guarantee_win=can_win,
        )
