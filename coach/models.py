"""Backends that propose a next guess.

Every backend implements the same method:

    propose(state: GameState, rejections: list[Rejection]) -> Suggestion

`rejections` holds earlier proposals in this same turn that the validator
refused, plus the reason each was refused. That lets the agent loop hand the
model concrete feedback and ask it to try again.

Backends:
  * ClaudeCoachModel - calls Claude through the Anthropic SDK.
  * OfflineCoachModel - a deterministic rules-based coach, used when no API
    credentials are available so the app and test suite still run offline.
  * ChaosCoachModel - wraps another backend and injects faults on purpose.
    It exists only to stress-test the guardrails during evaluation.
"""

from dataclasses import dataclass, field
import json
import logging
import os
import random

from coach.validator import feasible_interval

logger = logging.getLogger("coach.models")

CLAUDE_MODEL = "claude-opus-5-5"


@dataclass(frozen=True)
class GameState:
    """Everything the coach is allowed to see. Deliberately has no secret."""

    low: int
    high: int
    feedback: tuple = ()  # ((guess, outcome), ...) in the order played
    attempts_left: int = 1

    @property
    def guessed(self) -> set:
        return {g for g, _ in self.feedback}


@dataclass(frozen=True)
class Suggestion:
    guess: object  # should be int; the validator checks, so don't trust it here
    reasoning: str
    confidence: float


@dataclass(frozen=True)
class Rejection:
    guess: object
    reason: str


class CoachModelError(Exception):
    """The backend could not produce a usable suggestion at all."""


# --------------------------------------------------------------------------
# Claude
# --------------------------------------------------------------------------

SYSTEM_PROMPT = """You are the AI Coach for a number-guessing game.

The game has a secret integer in an inclusive range. After each guess the game \
says "Too High" (the secret is smaller than the guess) or "Too Low" (the secret \
is larger than the guess). You never see the secret.

Your job: recommend the single best next guess.
- Work out, from the hints alone, which numbers are still possible.
- Never recommend a number that a hint has already ruled out, and never repeat \
a guess.
- Prefer the guess that splits the remaining possibilities most evenly, so the \
player is guaranteed to finish in as few attempts as possible.
- If your earlier proposals were rejected by the game's validator, read the \
reason and correct your reasoning; do not repeat a rejected guess.

Reply with JSON only. "reasoning" is at most two short sentences a player can \
follow. "confidence" is a number from 0 to 1: how sure you are that the guess \
is consistent with every hint and is an optimal split."""

SUGGESTION_SCHEMA = {
    "type": "object",
    "properties": {
        "guess": {"type": "integer"},
        "reasoning": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["guess", "reasoning", "confidence"],
    "additionalProperties": False,
}


def build_user_prompt(state: GameState, rejections) -> str:
    lines = [
        f"Range: {state.low} to {state.high} (inclusive).",
        f"Attempts left: {state.attempts_left}.",
    ]
    if state.feedback:
        lines.append("Hints so far, in order:")
        for i, (guess, outcome) in enumerate(state.feedback, start=1):
            lines.append(f"  {i}. guessed {guess} -> {outcome}")
    else:
        lines.append("No guesses have been made yet.")
    if rejections:
        lines.append("Your earlier proposals this turn were rejected:")
        for r in rejections:
            lines.append(f"  - {r.guess!r}: {r.reason}")
    lines.append("What should the player guess next?")
    return "\n".join(lines)


def parse_suggestion(text: str) -> Suggestion:
    """Turn the model's JSON text into a Suggestion, or raise CoachModelError."""
    try:
        data = json.loads(text)
        guess = data["guess"]
        reasoning = str(data["reasoning"]).strip()
        confidence = float(data["confidence"])
    except (ValueError, KeyError, TypeError) as exc:
        raise CoachModelError(f"Malformed model output: {text[:200]!r}") from exc

    # Guardrail: a probability outside [0, 1] is meaningless. Clamp it rather
    # than reject a guess that may be perfectly good, but log that it happened.
    if not 0.0 <= confidence <= 1.0:
        logger.warning("Model confidence %s out of range; clamping", confidence)
        confidence = min(1.0, max(0.0, confidence))
    return Suggestion(guess=guess, reasoning=reasoning, confidence=confidence)


class ClaudeCoachModel:
    """Asks Claude for a guess using structured (JSON-schema) output."""

    name = "claude"

    def __init__(self, client=None, model: str = CLAUDE_MODEL):
        if client is None:
            import anthropic  # imported lazily so offline mode needs no SDK

            client = anthropic.Anthropic(timeout=60.0, max_retries=2)
        self.client = client
        self.model = model

    def propose(self, state: GameState, rejections) -> Suggestion:
        import anthropic

        prompt = build_user_prompt(state, rejections)
        logger.debug("Claude prompt:\n%s", prompt)
        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=4000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
                # A simple, latency-sensitive task: low effort is plenty.
                output_config={
                    "effort": "low",
                    "format": {"type": "json_schema", "schema": SUGGESTION_SCHEMA},
                },
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.AuthenticationError as exc:
            raise CoachModelError("Anthropic credentials were rejected.") from exc
        except anthropic.RateLimitError as exc:
            raise CoachModelError("Rate limited by the Anthropic API.") from exc
        except anthropic.APIStatusError as exc:
            raise CoachModelError(f"Anthropic API error {exc.status_code}.") from exc
        except anthropic.APIConnectionError as exc:
            raise CoachModelError("Could not reach the Anthropic API.") from exc

        if response.stop_reason == "refusal":
            raise CoachModelError("The model declined the request.")
        if response.stop_reason == "max_tokens":
            raise CoachModelError("The model ran out of output tokens.")

        text = next((b.text for b in response.content if b.type == "text"), "")
        return parse_suggestion(text)


# --------------------------------------------------------------------------
# Offline and fault-injection backends
# --------------------------------------------------------------------------


class OfflineCoachModel:
    """Rules-based coach: always the midpoint of the remaining interval.

    This is the baseline the Claude coach is measured against, and what the
    app uses when it has no API credentials.
    """

    name = "offline"

    def propose(self, state: GameState, rejections) -> Suggestion:
        interval = feasible_interval(state.low, state.high, state.feedback)
        if interval.is_empty:
            raise CoachModelError("No number fits the hints.")
        guess = interval.midpoint
        return Suggestion(
            guess=guess,
            reasoning=(
                f"The secret must be between {interval.low} and {interval.high}; "
                f"{guess} splits that range in half."
            ),
            confidence=1.0,
        )


class ChaosCoachModel:
    """Wraps a backend and, with probability `fault_rate`, sabotages it.

    Faults are the ways a real language model can go wrong: a guess the hints
    already ruled out, a repeated guess, an out-of-range number, a non-integer,
    or no usable answer at all. Seeded, so evaluation runs are reproducible.
    """

    FAULTS = ("contradicts_hints", "repeat", "out_of_range", "not_an_int", "error")

    def __init__(self, inner, fault_rate: float = 0.3, seed: int = 0):
        self.inner = inner
        self.fault_rate = fault_rate
        self.rng = random.Random(seed)
        self.name = f"chaos({inner.name}, p={fault_rate})"

    def propose(self, state: GameState, rejections) -> Suggestion:
        if self.rng.random() >= self.fault_rate:
            return self.inner.propose(state, rejections)

        fault = self.rng.choice(self.FAULTS)
        interval = feasible_interval(state.low, state.high, state.feedback)
        if fault == "error":
            raise CoachModelError("Injected fault: backend returned nothing usable.")
        if fault == "not_an_int":
            guess = "fifty"
        elif fault == "repeat" and state.feedback:
            guess = state.feedback[-1][0]
        elif fault == "out_of_range":
            guess = state.high + 37
        else:  # contradicts_hints (or repeat with no history yet)
            guess = interval.high + 1 if interval.high < state.high else state.low - 1
        return Suggestion(guess=guess, reasoning=f"[injected fault: {fault}]", confidence=0.9)


def default_model():
    """Claude when credentials are configured, otherwise the offline coach."""
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        try:
            return ClaudeCoachModel()
        except ImportError:
            logger.warning("anthropic SDK not installed; using offline coach")
    return OfflineCoachModel()
