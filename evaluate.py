"""Reliability harness for the AI Coach.

Lets the coach play complete games against the real game logic and measures
how often it wins, how often its suggestions survive validation, and how often
the guardrails have to step in.

Examples:
    python evaluate.py --suite                       # offline, chaos, glitch
    python evaluate.py --model offline --games 60
    python evaluate.py --model chaos --fault-rate 0.5
    python evaluate.py --model claude --games 9      # needs ANTHROPIC_API_KEY
    python evaluate.py --model offline --glitch      # replay the original Bug 2

`--glitch` swaps in the ORIGINAL buggy comparison from the Module 1 starter
code (the secret was cast to str on even attempts, so "9" > "50"). It measures
whether the coach notices that the game is lying instead of trusting it.
"""

import argparse
import json
import random
import statistics
import sys
from pathlib import Path

from coach import (
    ChaosCoachModel,
    ClaudeCoachModel,
    CoachAgent,
    GameState,
    OfflineCoachModel,
)
from coach.logging_config import configure_logging
from coach.validator import feasible_interval, guesses_needed
from logic_utils import check_guess, get_attempt_limit, get_range_for_difficulty

REPORT_DIR = Path(__file__).resolve().parent / "reports"
DIFFICULTIES = ("Easy", "Normal", "Hard")


def glitched_check_guess(guess: int, secret: int, attempt: int) -> str:
    """The original starter-code bug, reproduced on purpose for evaluation."""
    if attempt % 2 == 0:
        g, s = str(guess), str(secret)
        if g == s:
            return "Win"
        return "Too High" if g > s else "Too Low"
    return check_guess(guess, secret)


def play_game(agent, difficulty: str, secret: int, glitch: bool = False) -> dict:
    low, high = get_range_for_difficulty(difficulty)
    limit = get_attempt_limit(difficulty)
    feedback = []
    turns = []
    lies = 0
    invalid_played = 0
    misled = 0
    outcome = "lost"

    for attempt in range(1, limit + 1):
        state = GameState(low, high, tuple(feedback), limit - attempt + 1)
        result = agent.suggest(state)
        turns.append(result)
        if result.glitch_detected:
            outcome = "glitch_detected"
            break
        # Audit what was actually played, independently of the agent's own
        # validator: an int, in range, not repeated, and consistent with the
        # TRUE hints (so this also catches a validator bug).
        true_interval = feasible_interval(
            low, high, [(g, check_guess(g, secret)) for g, _ in feedback]
        )
        if (
            not isinstance(result.guess, int)
            or not low <= result.guess <= high
            or result.guess in {g for g, _ in feedback}
        ):
            invalid_played += 1  # a guardrail failure: should always be 0
        elif not true_interval.contains(result.guess):
            misled += 1  # followed a hint that was a lie (glitch mode only)
        hint = (
            glitched_check_guess(result.guess, secret, attempt)
            if glitch
            else check_guess(result.guess, secret)
        )
        if hint != check_guess(result.guess, secret):
            lies += 1
        feedback.append((result.guess, hint))
        if hint == "Win":
            outcome = "won"
            break

    return {
        "difficulty": difficulty,
        "secret": secret,
        "outcome": outcome,
        "guesses": len(feedback),
        "optimal_bound": guesses_needed(high - low + 1),
        "lies": lies,
        "invalid_played": invalid_played,
        "misled": misled,
        "turns": [
            {
                "guess": t.guess,
                "source": t.source,
                "confidence": t.confidence,
                "model_calls": t.model_calls,
                "rejections": [r.reason for r in t.rejections],
            }
            for t in turns
        ],
    }


def summarize(games: list, label: str) -> dict:
    turns = [t for g in games for t in g["turns"]]
    won = [g for g in games if g["outcome"] == "won"]
    model_turns = [t for t in turns if t["source"] == "model"]
    confs = [t["confidence"] for t in model_turns if t["confidence"] is not None]
    lied_to = [g for g in games if g["lies"] > 0]
    honest = [g for g in games if g["lies"] == 0]

    def pct(n, d):
        return round(100.0 * n / d, 1) if d else 0.0

    return {
        "label": label,
        "games": len(games),
        "wins": len(won),
        "win_rate_pct": pct(len(won), len(games)),
        "avg_guesses_to_win": round(statistics.mean(g["guesses"] for g in won), 2)
        if won
        else None,
        "wins_within_optimal_bound_pct": pct(
            sum(g["guesses"] <= g["optimal_bound"] for g in won), len(won)
        ),
        "coach_turns": len(turns),
        "first_try_valid_pct": pct(
            sum(t["source"] == "model" and not t["rejections"] for t in turns),
            len(turns),
        ),
        "suggestions_rejected": sum(len(t["rejections"]) for t in turns),
        "recovered_by_retry": sum(
            t["source"] == "model" and bool(t["rejections"]) for t in turns
        ),
        "fallbacks": sum(t["source"] == "fallback" for t in turns),
        "invalid_guesses_played": sum(g["invalid_played"] for g in games),
        "guesses_misled_by_false_hints": sum(g["misled"] for g in games),
        "avg_model_confidence": round(statistics.mean(confs), 3) if confs else None,
        "games_with_lying_hints": len(lied_to),
        "glitch_detected_in_lying_games": sum(
            g["outcome"] == "glitch_detected" for g in lied_to
        ),
        "false_glitch_alarms": sum(g["outcome"] == "glitch_detected" for g in honest),
    }


def build_model(name: str, fault_rate: float, seed: int):
    if name == "offline":
        return OfflineCoachModel()
    if name == "chaos":
        return ChaosCoachModel(OfflineCoachModel(), fault_rate=fault_rate, seed=seed)
    if name == "claude":
        return ClaudeCoachModel()
    raise ValueError(f"Unknown model {name!r}")


def run(model_name, games, seed, glitch=False, fault_rate=0.3, max_attempts=3):
    rng = random.Random(seed)
    agent = CoachAgent(build_model(model_name, fault_rate, seed), max_attempts)
    results = []
    for i in range(games):
        difficulty = DIFFICULTIES[i % len(DIFFICULTIES)]
        low, high = get_range_for_difficulty(difficulty)
        results.append(play_game(agent, difficulty, rng.randint(low, high), glitch))
    label = agent.model.name + (" + glitched game" if glitch else "")
    return summarize(results, label), results


def print_summary(s: dict):
    print(f"\n=== {s['label']} ===")
    width = max(len(k) for k in s)
    for key, value in s.items():
        if key != "label":
            print(f"  {key:<{width}}  {value}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--model", choices=["offline", "chaos", "claude"], default="offline")
    parser.add_argument("--games", type=int, default=30)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--fault-rate", type=float, default=0.3)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--glitch", action="store_true")
    parser.add_argument("--suite", action="store_true", help="run the standard offline suite")
    args = parser.parse_args(argv)

    configure_logging()
    REPORT_DIR.mkdir(exist_ok=True)

    if args.suite:
        configs = [
            ("offline", False, args.fault_rate),
            ("chaos", False, args.fault_rate),
            ("chaos", False, 0.6),
            ("offline", True, args.fault_rate),
        ]
    else:
        configs = [(args.model, args.glitch, args.fault_rate)]

    summaries = []
    for model_name, glitch, fault_rate in configs:
        summary, games = run(
            model_name, args.games, args.seed, glitch, fault_rate, args.max_attempts
        )
        print_summary(summary)
        summaries.append(summary)
        stem = f"eval_{model_name}" + (f"_p{fault_rate}" if model_name == "chaos" else "")
        stem += "_glitch" if glitch else ""
        (REPORT_DIR / f"{stem}.json").write_text(
            json.dumps({"summary": summary, "games": games}, indent=2)
        )

    if args.suite:
        (REPORT_DIR / "eval_suite_summary.json").write_text(json.dumps(summaries, indent=2))
        print(f"\nReports written to {REPORT_DIR.name}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
