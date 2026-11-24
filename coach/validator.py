"""Deterministic checks the AI Coach's output must pass.

The validator is the ground truth in this system. The language model proposes
a guess, but nothing it says is trusted until this module has checked it
against the hints the game has actually given. None of this code calls a model,
so every rule here is fully unit-testable.
"""

from dataclasses import dataclass
import math

VALID_OUTCOMES = ("Too High", "Too Low", "Win")


@dataclass(frozen=True)
class Interval:
    """The inclusive range of numbers that are still possible secrets."""

    low: int
    high: int

    @property
    def size(self) -> int:
        return max(0, self.high - self.low + 1)

    @property
    def is_empty(self) -> bool:
        return self.size == 0

    @property
    def midpoint(self) -> int:
        return (self.low + self.high) // 2

    def contains(self, value: int) -> bool:
        return self.low <= value <= self.high


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    reason: str


def feasible_interval(low: int, high: int, feedback) -> Interval:
    """Narrow [low, high] using every (guess, outcome) pair seen so far.

    "Too High" on guess g means the secret is < g, so the upper bound becomes
    g - 1. "Too Low" means the lower bound becomes g + 1. If the hints
    contradict each other, the returned interval is empty, which is how the
    coach detects that the game itself is lying (see `hints_are_consistent`).
    """
    lo, hi = low, high
    for guess, outcome in feedback:
        if outcome not in VALID_OUTCOMES:
            raise ValueError(f"Unknown outcome {outcome!r} for guess {guess}")
        if outcome == "Too High":
            hi = min(hi, guess - 1)
        elif outcome == "Too Low":
            lo = max(lo, guess + 1)
        else:  # Win: only that number is possible.
            lo, hi = max(lo, guess), min(hi, guess)
    return Interval(lo, hi)


def hints_are_consistent(low: int, high: int, feedback) -> bool:
    """False when no secret could have produced every hint we were shown."""
    return not feasible_interval(low, high, feedback).is_empty


def guesses_needed(size: int) -> int:
    """Worst-case guesses binary search needs to find one of `size` numbers."""
    if size <= 0:
        return 0
    return math.ceil(math.log2(size + 1))


def validate_suggestion(guess, interval: Interval, already_guessed) -> ValidationResult:
    """Check a proposed guess. Returns why it was rejected, if it was."""
    if isinstance(guess, bool) or not isinstance(guess, int):
        return ValidationResult(False, f"Guess {guess!r} is not an integer.")
    if interval.is_empty:
        return ValidationResult(False, "No number is consistent with the hints.")
    if guess in already_guessed:
        return ValidationResult(
            False, f"{guess} was already guessed; repeating it wastes an attempt."
        )
    if not interval.contains(guess):
        return ValidationResult(
            False,
            f"{guess} contradicts the hints: the secret must be between "
            f"{interval.low} and {interval.high}.",
        )
    return ValidationResult(True, "Consistent with every hint so far.")
