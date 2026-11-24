import os
import random

import streamlit as st

# FIX: Refactored all game logic out of this file and into logic_utils.py using
# Claude Code in agent mode. app.py is now UI + session state only, which is
# what makes tests/test_game_logic.py able to import the logic directly.
from logic_utils import (
    check_guess,
    get_attempt_limit,
    get_hint_message,
    get_range_for_difficulty,
    parse_guess,
    update_score,
)

from coach import ClaudeCoachModel, CoachAgent, GameState, OfflineCoachModel
from coach.logging_config import configure_logging

configure_logging()

st.set_page_config(page_title="Glitchy Guesser + AI Coach", page_icon="🎮")

st.title("🎮 Glitchy Guesser + 🤖 AI Coach")
st.caption(
    "A number-guessing game with an AI coach that checks its own advice "
    "against the game's hints before it shows it to you."
)

st.sidebar.header("Settings")

difficulty = st.sidebar.selectbox(
    "Difficulty",
    ["Easy", "Normal", "Hard"],
    index=1,
)

# FIX (Bug 6): Hard gave 5 attempts over a 1-50 range, but binary search needs
# 6 there -- it could not be won. Every difficulty is now solvable with perfect
# play: Easy needs 5 of 6, Normal needs 7 of 8, Hard needs 7 of 7. The table
# now lives in logic_utils.ATTEMPT_LIMITS so evaluate.py uses the same numbers.
attempt_limit = get_attempt_limit(difficulty)

low, high = get_range_for_difficulty(difficulty)

st.sidebar.caption(f"Range: {low} to {high}")
st.sidebar.caption(f"Attempts allowed: {attempt_limit}")

# ---- AI Coach configuration ----------------------------------------------
st.sidebar.header("AI Coach")
has_credentials = bool(
    os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
)
coach_mode = st.sidebar.radio(
    "Coach backend",
    ["Claude", "Offline (rules-based)"],
    index=0 if has_credentials else 1,
    help="Claude needs ANTHROPIC_API_KEY. Offline mode needs nothing.",
)
if coach_mode == "Claude" and not has_credentials:
    st.sidebar.warning("No ANTHROPIC_API_KEY found - using the offline coach.")
    coach_mode = "Offline (rules-based)"


@st.cache_resource
def get_agent(mode: str) -> CoachAgent:
    model = ClaudeCoachModel() if mode == "Claude" else OfflineCoachModel()
    return CoachAgent(model, max_attempts=3)


agent = get_agent(coach_mode)
st.sidebar.caption(f"Model: `{agent.model.name}`, up to {agent.max_attempts} tries per hint")


# FIX (Bug 3): "New Game" used to reset only attempts and secret, so status
# stayed "won"/"lost" and the st.stop() guard below killed the app forever.
# Starting a round now happens in exactly one place, so the initial game and
# every restart are guaranteed to set the same keys.
def start_new_round(low: int, high: int):
    st.session_state.secret = random.randint(low, high)
    st.session_state.attempts = 0
    st.session_state.score = 0
    st.session_state.status = "playing"
    st.session_state.history = []
    # (guess, outcome) pairs: what the AI Coach is allowed to see.
    st.session_state.feedback = []
    st.session_state.coach_result = None


if "status" not in st.session_state:
    start_new_round(low, high)

st.subheader("Make a guess")

# FIX (Bug 6): this said "between 1 and 100" no matter what, even on Easy
# where the range is 1-20. It now reports the difficulty's actual range.
st.info(
    f"Guess a number between {low} and {high}. "
    f"Attempts left: {attempt_limit - st.session_state.attempts}"
)

with st.expander("Developer Debug Info"):
    st.write("Secret:", st.session_state.secret)
    st.write("Attempts:", st.session_state.attempts)
    st.write("Score:", st.session_state.score)
    st.write("Difficulty:", difficulty)
    st.write("History:", st.session_state.history)
    st.write("Feedback seen by coach:", st.session_state.feedback)

raw_guess = st.text_input(
    "Enter your guess:",
    key=f"guess_input_{difficulty}"
)

col1, col2, col3 = st.columns(3)
with col1:
    submit = st.button("Submit Guess 🚀")
with col2:
    new_game = st.button("New Game 🔁")
with col3:
    show_hint = st.checkbox("Show hint", value=True)

if new_game:
    # FIX (Bug 3): was random.randint(1, 100) regardless of difficulty, so an
    # Easy round (1-20) could be handed an unreachable secret.
    start_new_round(low, high)
    st.rerun()

if st.session_state.status != "playing":
    if st.session_state.status == "won":
        st.success("You already won. Start a new game to play again.")
    else:
        st.error("Game over. Start a new game to try again.")
    st.stop()


def play_guess(guess_int: int):
    """Apply one valid guess to the round. Used by Submit and by the coach."""
    st.session_state.attempts += 1
    st.session_state.history.append(guess_int)

    # FIX (Bug 2, root cause): this used to cast the secret to str on every
    # even-numbered attempt, so the same guess was judged numerically one
    # turn and alphabetically the next. The AI first suggested patching
    # check_guess to tolerate both types; I pushed back and deleted the
    # cast here instead, because the caller was the actual bug.
    outcome = check_guess(guess_int, st.session_state.secret)
    st.session_state.feedback.append((guess_int, outcome))
    st.session_state.coach_result = None  # old advice is stale now
    message = get_hint_message(outcome)

    if show_hint:
        st.warning(message)

    st.session_state.score = update_score(
        current_score=st.session_state.score,
        outcome=outcome,
        attempt_number=st.session_state.attempts,
    )

    if outcome == "Win":
        st.balloons()
        st.session_state.status = "won"
        st.success(
            f"You won! The secret was {st.session_state.secret}. "
            f"Final score: {st.session_state.score}"
        )
    elif st.session_state.attempts >= attempt_limit:
        st.session_state.status = "lost"
        st.error(
            f"Out of attempts! "
            f"The secret was {st.session_state.secret}. "
            f"Score: {st.session_state.score}"
        )


if submit:
    ok, guess_int, err = parse_guess(raw_guess, low, high)

    if not ok:
        # FIX (Bug 4): the counter used to be incremented before this check, so
        # a typo burned a turn and pushed the raw text into the history list.
        st.error(err)
    else:
        play_guess(guess_int)

# ---- AI Coach panel ---------------------------------------------------------
st.divider()
st.subheader("🤖 AI Coach")
st.caption(
    "The coach only sees the range and the hints - never the secret. Every "
    "suggestion is checked against the hints before you see it."
)

if st.session_state.status == "playing":
    if st.button("Ask the coach for a hint 💡"):
        state = GameState(
            low=low,
            high=high,
            feedback=tuple(st.session_state.feedback),
            attempts_left=attempt_limit - st.session_state.attempts,
        )
        with st.spinner("Coach is thinking and checking its work..."):
            try:
                st.session_state.coach_result = agent.suggest(state)
            except Exception as exc:  # never let the coach crash the game
                st.session_state.coach_result = None
                st.error(f"The coach hit an unexpected error: {exc}")

    result = st.session_state.coach_result
    if result is not None:
        if result.glitch_detected:
            st.error(f"🐛 Glitch detected: {result.reasoning}")
        else:
            badge = {
                "model": f"✅ verified suggestion from `{result.model_name}`",
                "fallback": "🛡️ safe fallback (the AI's answer failed validation)",
            }[result.source]
            st.markdown(f"**Try {result.guess}.** {result.reasoning}")
            conf = (
                f"{result.confidence:.0%}" if result.confidence is not None else "n/a"
            )
            lo, hi = result.interval
            st.caption(
                f"{badge} · model confidence: {conf} · still possible: {lo}-{hi} · "
                + (
                    "a win is still guaranteed with perfect play"
                    if result.can_guarantee_win
                    else "a win is no longer guaranteed"
                )
            )
            if st.button(f"Play {result.guess} ▶️"):
                play_guess(result.guess)

        with st.expander("How the coach checked its work"):
            for line in result.trace:
                st.text(line)

st.divider()
st.caption("Built by an AI that claims this code is production-ready. Now it checks.")
