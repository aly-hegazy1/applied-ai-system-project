"""Tests for the AI Coach: validator, agent loop, guardrails, Claude adapter.

None of these call the real API. The Claude adapter is tested with a fake
client (to check request shape and response parsing) and with a real SDK
client pointed at a closed port (to check that network failures degrade to
the safe fallback instead of crashing).
"""

import json
from types import SimpleNamespace

import pytest

from coach import (
    ChaosCoachModel,
    ClaudeCoachModel,
    CoachAgent,
    CoachModelError,
    GameState,
    OfflineCoachModel,
)
from coach.models import Rejection, Suggestion, build_user_prompt, parse_suggestion
from coach.validator import (
    feasible_interval,
    guesses_needed,
    hints_are_consistent,
    validate_suggestion,
)
import evaluate


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

def test_interval_narrows_from_both_sides():
    iv = feasible_interval(1, 100, [(50, "Too High"), (25, "Too Low")])
    assert (iv.low, iv.high, iv.size) == (26, 49, 24)


def test_contradictory_hints_give_an_empty_interval():
    # "50 is too high" and "60 is too low" cannot both be true.
    assert not hints_are_consistent(1, 100, [(50, "Too High"), (60, "Too Low")])


def test_unknown_outcome_is_rejected_loudly():
    with pytest.raises(ValueError):
        feasible_interval(1, 100, [(50, "Go higher")])


@pytest.mark.parametrize(
    "guess, expected_ok",
    [(30, True), (26, True), (49, True), (25, False), (50, False), (0, False),
     ("30", False), (30.0, False), (True, False)],
)
def test_validate_suggestion(guess, expected_ok):
    iv = feasible_interval(1, 100, [(50, "Too High"), (25, "Too Low")])
    assert validate_suggestion(guess, iv, {50, 25}).ok is expected_ok


def test_repeated_guess_is_rejected_with_a_reason():
    iv = feasible_interval(1, 10, [])
    result = validate_suggestion(5, iv, {5})
    assert not result.ok and "already guessed" in result.reason


def test_guesses_needed_matches_the_difficulty_table():
    assert guesses_needed(20) == 5
    assert guesses_needed(100) == 7
    assert guesses_needed(1) == 1


# ---------------------------------------------------------------------------
# Agent loop with scripted models
# ---------------------------------------------------------------------------

class ScriptedModel:
    """Returns (or raises) a fixed sequence of responses and records calls."""

    name = "scripted"

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def propose(self, state, rejections):
        self.calls.append(list(rejections))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return Suggestion(guess=item, reasoning="scripted", confidence=0.8)


STATE = GameState(1, 100, ((50, "Too High"), (25, "Too Low")), attempts_left=6)


def test_valid_first_suggestion_is_accepted():
    result = CoachAgent(ScriptedModel(37)).suggest(STATE)
    assert result.source == "model" and result.guess == 37
    assert result.first_try_valid and result.model_calls == 1


def test_rejected_suggestion_is_retried_with_feedback():
    model = ScriptedModel(75, 37)
    result = CoachAgent(model).suggest(STATE)
    assert result.source == "model" and result.guess == 37
    assert len(result.rejections) == 1
    # The second call must have been told why the first one failed.
    assert "between 26 and 49" in model.calls[1][0].reason


def test_falls_back_to_midpoint_after_max_attempts():
    model = ScriptedModel(75, 80, 90)
    result = CoachAgent(model, max_attempts=3).suggest(STATE)
    assert result.source == "fallback"
    assert result.guess == 37  # midpoint of 26..49
    assert result.model_calls == 3 and len(result.rejections) == 3


def test_model_error_falls_back_without_retrying():
    model = ScriptedModel(CoachModelError("API down"), 37)
    result = CoachAgent(model).suggest(STATE)
    assert result.source == "fallback" and result.guess == 37
    assert result.model_calls == 1  # a broken backend is not asked again


def test_glitch_is_reported_and_model_is_never_called():
    model = ScriptedModel()
    state = GameState(1, 100, ((50, "Too High"), (60, "Too Low")), 5)
    result = CoachAgent(model).suggest(state)
    assert result.glitch_detected and result.guess is None
    assert model.calls == []


def test_trace_records_every_step():
    result = CoachAgent(ScriptedModel(75, 37)).suggest(STATE)
    kinds = [line.split(":")[0] for line in result.trace]
    assert kinds == ["PLAN", "ACT #1", "CHECK #1", "ACT #2", "CHECK #2"]


def test_agent_rejects_bad_configuration():
    with pytest.raises(ValueError):
        CoachAgent(OfflineCoachModel(), max_attempts=0)
    with pytest.raises(ValueError):
        CoachAgent(OfflineCoachModel()).suggest(GameState(10, 1))


def test_coach_never_sees_the_secret():
    # GameState is the only thing a model receives; it has no secret field.
    assert "secret" not in GameState.__dataclass_fields__


# ---------------------------------------------------------------------------
# Claude adapter (no network)
# ---------------------------------------------------------------------------

def fake_response(text, stop_reason="end_turn"):
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="thinking"), SimpleNamespace(type="text", text=text)],
    )


class FakeClient:
    def __init__(self, response):
        self.kwargs = None
        outer = self

        class Messages:
            def create(self, **kwargs):
                outer.kwargs = kwargs
                return response

        self.beta = SimpleNamespace(messages=Messages())


def test_claude_adapter_sends_hints_and_schema_and_parses_reply():
    reply = json.dumps({"guess": 37, "reasoning": "Halve 26-49.", "confidence": 0.9})
    client = FakeClient(fake_response(reply))
    suggestion = ClaudeCoachModel(client=client).propose(STATE, [])

    assert suggestion == Suggestion(37, "Halve 26-49.", 0.9)
    kw = client.kwargs
    assert kw["model"] == "claude-opus-5-5"
    assert kw["output_config"]["format"]["type"] == "json_schema"
    prompt = kw["messages"][0]["content"]
    assert "guessed 50 -> Too High" in prompt and "guessed 25 -> Too Low" in prompt


def test_claude_refusal_becomes_a_model_error():
    client = FakeClient(fake_response("", stop_reason="refusal"))
    with pytest.raises(CoachModelError):
        ClaudeCoachModel(client=client).propose(STATE, [])


def test_malformed_json_becomes_a_model_error():
    with pytest.raises(CoachModelError):
        parse_suggestion("Sure! I think 37.")


def test_confidence_is_clamped_into_range():
    assert parse_suggestion('{"guess": 3, "reasoning": "x", "confidence": 7}').confidence == 1.0


def test_rejections_are_included_in_the_retry_prompt():
    prompt = build_user_prompt(STATE, [Rejection(75, "nope")])
    assert "75: nope" in prompt


def test_unreachable_api_degrades_to_fallback_not_crash():
    anthropic = pytest.importorskip("anthropic")
    client = anthropic.Anthropic(
        api_key="test-key", base_url="http://127.0.0.1:9", max_retries=0, timeout=2.0
    )
    result = CoachAgent(ClaudeCoachModel(client=client)).suggest(STATE)
    assert result.source == "fallback" and result.guess == 37
    assert "Could not reach the Anthropic API" in result.trace[1]


# ---------------------------------------------------------------------------
# End-to-end reliability (the evaluation harness)
# ---------------------------------------------------------------------------

def test_offline_coach_wins_every_game_within_the_optimal_bound():
    summary, _ = evaluate.run("offline", games=30, seed=1)
    assert summary["win_rate_pct"] == 100.0
    assert summary["wins_within_optimal_bound_pct"] == 100.0


def test_guardrails_hold_even_when_the_model_is_mostly_wrong():
    summary, _ = evaluate.run("chaos", games=30, seed=1, fault_rate=0.8)
    assert summary["suggestions_rejected"] > 0 and summary["fallbacks"] > 0
    assert summary["invalid_guesses_played"] == 0
    assert summary["win_rate_pct"] == 100.0


def test_no_false_glitch_alarms_on_an_honest_game():
    summary, _ = evaluate.run("chaos", games=30, seed=3, fault_rate=0.5)
    assert summary["false_glitch_alarms"] == 0


def test_glitched_game_is_caught_at_least_sometimes():
    summary, _ = evaluate.run("offline", games=60, seed=42, glitch=True)
    assert summary["games_with_lying_hints"] > 0
    assert summary["glitch_detected_in_lying_games"] > 0
    assert summary["false_glitch_alarms"] == 0


def test_chaos_model_is_reproducible_with_a_seed():
    a = ChaosCoachModel(OfflineCoachModel(), 0.5, seed=9)
    b = ChaosCoachModel(OfflineCoachModel(), 0.5, seed=9)
    for _ in range(20):
        try:
            ga = a.propose(STATE, []).guess
        except CoachModelError:
            ga = "error"
        try:
            gb = b.propose(STATE, []).guess
        except CoachModelError:
            gb = "error"
        assert ga == gb
