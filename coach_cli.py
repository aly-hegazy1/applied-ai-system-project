"""Ask the AI Coach for one suggestion from the command line.

Examples:
    python coach_cli.py --range 1 100
    python coach_cli.py --range 1 100 --hints 50:high 25:low --attempts-left 6
    python coach_cli.py --range 1 100 --hints 50:high 60:low      # contradiction
    python coach_cli.py --model chaos --fault-rate 1.0 --range 1 20
    python coach_cli.py --model claude --range 1 100 --hints 50:low

Hints are GUESS:high (the game said "Too High") or GUESS:low ("Too Low").
"""

import argparse
import sys

from coach import ChaosCoachModel, CoachAgent, GameState, OfflineCoachModel
from coach.logging_config import LOG_FILE, configure_logging

OUTCOMES = {"high": "Too High", "low": "Too Low"}


def parse_hint(text: str):
    try:
        guess, word = text.split(":")
        return int(guess), OUTCOMES[word.strip().lower()]
    except (ValueError, KeyError):
        raise argparse.ArgumentTypeError(
            f"bad hint {text!r}: use GUESS:high or GUESS:low, e.g. 50:high"
        )


def main(argv=None):
    parser = argparse.ArgumentParser(description="Ask the AI Coach for a guess.")
    parser.add_argument("--range", nargs=2, type=int, default=[1, 100], metavar=("LOW", "HIGH"))
    parser.add_argument("--hints", nargs="*", type=parse_hint, default=[])
    parser.add_argument("--attempts-left", type=int, default=8)
    parser.add_argument("--model", choices=["offline", "claude", "chaos"], default="offline")
    parser.add_argument("--fault-rate", type=float, default=0.5)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)

    low, high = args.range
    if low > high:
        parser.error("LOW must not be greater than HIGH")
    if args.attempts_left < 1:
        parser.error("--attempts-left must be at least 1")

    configure_logging()

    if args.model == "claude":
        from coach import ClaudeCoachModel

        model = ClaudeCoachModel()
    elif args.model == "chaos":
        model = ChaosCoachModel(OfflineCoachModel(), args.fault_rate, args.seed)
    else:
        model = OfflineCoachModel()

    state = GameState(low, high, tuple(args.hints), args.attempts_left)
    result = CoachAgent(model).suggest(state)

    print("Agent trace:")
    for line in result.trace:
        print(f"  {line}")
    print()
    if result.glitch_detected:
        print(f"GLITCH DETECTED: {result.reasoning}")
    else:
        conf = f"{result.confidence:.2f}" if result.confidence is not None else "n/a"
        print(f"Suggested guess : {result.guess}")
        print(f"Reasoning       : {result.reasoning}")
        print(f"Source          : {result.source} ({result.model_name})")
        print(f"Confidence      : {conf}")
        print(f"Win guaranteed  : {result.can_guarantee_win}")
    print(f"\n(full log: {LOG_FILE.relative_to(LOG_FILE.parent.parent)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
