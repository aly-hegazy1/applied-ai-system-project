# Model Card: Glitchy Guesser AI Coach

## Model details

| | |
|---|---|
| **System** | AI Coach for the Glitchy Guesser number game |
| **Type** | Agentic workflow (plan → act → check → retry → fallback) around a language model |
| **Model** | Claude Opus 5.5 (`claude-opus-5-5`) via the Anthropic API, using JSON-schema structured output and `effort: "low"`. A rules-based offline backend is used when no API key is configured. |
| **Inputs** | Range, the (guess, outcome) hint history, attempts left. Never the secret. |
| **Outputs** | A guess, a short explanation, a 0-1 confidence, a source label (verified / fallback / glitch), and a step-by-step trace |
| **Built on** | *Game Glitch Investigator* (CodePath AI110, Modules 1-3) |

## Intended use

To help a player of a number-guessing game choose a good next guess and
understand *why* it is good. It is also a teaching example of treating a
language model's output as an unverified proposal. It is not meant for any
decision where being wrong has real consequences.

## Evaluation results

From `python evaluate.py --suite` (30 seeded games per configuration) and
`python -m pytest` (52/52 passing). Full numbers are in the README.

- **0 invalid guesses played across 120 games**, including runs where the model
  was sabotaged 30% and 60% of the time. At 60% faults the validator rejected
  119 suggestions; retries recovered 44 turns and the fallback covered 50.
- **100% win rate** on honest games, always within the binary-search bound.
- **Glitched game** (the original Bug 2 string comparison replayed): win rate
  86.7%. The game lied in 4 games; the coach detected it in 2. There were no
  false glitch alarms in any of the 120 games.
- **Claude backend:** not yet measured, because no API key was available when
  this was written. `python evaluate.py --model claude --games 9` produces the
  same table for the live model.

---

## Limitations and biases

- **It can only catch lies that contradict each other.** The coach detects a
  glitch when no secret fits every hint. A game that lies *consistently*, for
  example one that always says "Too Low", leaves a non-empty interval, and the
  coach follows it straight into a loss. In glitch mode 20 guesses were misled
  by false hints this way before any contradiction appeared.
- **Detection depends on how many attempts are left.** The replayed Bug 2 only
  lies when the secret is a single digit. On Normal (8 attempts) the lies
  collided on the 8th turn and the coach flagged them. On Hard (7 attempts)
  the same sequence ran out of attempts first, and the player simply lost.
  Same bug, same secret, different outcome, purely because of the attempt
  budget.
- **The confidence score is not calibrated.** Claude reports its own
  confidence. Nothing in this system checks that 0.9 means "right 90% of the
  time", and the offline coach always reports 1.0. Treat it as the model's
  self-report, not as a probability. The *verified* badge, which comes from
  the validator, is the signal to trust.
- **The validator checks consistency, not optimality.** A Claude guess of 27
  in a 26-49 range is valid and will be accepted, even though 37 is better. A
  valid but poor guess can cost the player a guaranteed win, and the coach
  reports that through `can_guarantee_win` rather than preventing it.
- **The bias toward the midpoint is a design choice.** The fallback and the
  offline coach always choose the midpoint. That is optimal against a
  uniformly random secret, but it is fully predictable, so a game that picked
  its secret adversarially could exploit it.
- **The offline baseline flatters the numbers.** The 100% figures come from a
  perfect rules-based model plus injected faults, not from Claude. The fault
  types are ones I imagined; a real model could fail in ways the chaos backend
  doesn't simulate.

## Could it be misused, and how is that prevented?

The direct stakes are low, since it's a guessing game. The realistic misuse
cases come from the pattern:

- **Cheating or answer leaking.** If the coach could see the secret, it could
  leak it, whether through a prompt-injection-style request or just by being
  "helpful". Prevention: `GameState` has no secret field, so the model never
  receives it. A test asserts this, so it can't be added back by accident.
  (The Developer Debug panel does show the secret; it is a debug tool from the
  original assignment and is clearly labelled.)
- **Over-trust.** A fluent explanation can make a bad move look authoritative.
  Prevention: every suggestion carries a visible *verified* or *fallback*
  badge and a full trace, and the UI never presents an unvalidated model
  guess as advice.
- **Cost or abuse of the API key.** Someone could hammer the "Ask the coach"
  button. Mitigations: low effort, a tiny prompt, at most 3 model calls per
  hint, no retry after an API error, and the key is read only from the
  environment (`.env` is git-ignored). A public deployment would still need
  per-user rate limiting, which this project does not have.
- **Copying the pattern into higher-stakes settings.** "LLM proposes,
  validator checks" is only as good as the validator. Here correctness is
  fully checkable. In domains where it isn't, the same architecture would give
  false confidence. I would not reuse it there without a much stronger check
  and a human in the loop.

## What surprised me while testing reliability

1. **An honest metric found something the code was hiding.** The first draft
   of `evaluate.py` reported `invalid_guesses_played: 0` as a constant, on the
   reasoning that the validator makes invalid guesses impossible. Replacing
   that with an independent audit against the *true* hints made glitch mode
   show **20** "invalid" guesses. These weren't guardrail failures; they were
   the coach correctly following hints that were lies. Splitting the number
   into `invalid_guesses_played` (still 0) and
   `guesses_misled_by_false_hints` (20) produced the most honest finding in
   the project.
2. **The original bug was much quieter than I remembered.** In Module 1 the
   string comparison felt like it broke everything. Played by a binary-search
   coach, it lied in only 4 of 30 games, because `"9" > "50"`-style flips only
   happen when the secret is a single digit. A bug that rarely fires is
   *harder* to catch, for humans and for this coach.
3. **The guardrails held up far better than the model did.** At a 60% fault
   rate fewer than 40% of suggestions were valid on the first try, yet the win
   rate didn't move. Reliability came from the loop around the model, not
   from the model.

---

## Collaboration with AI

I built this stage with **Claude Code (Claude Opus 5.5) in agent mode** inside
VS Code. I gave it the assignment brief and the existing repo. It proposed the
architecture, wrote the `coach` package, tests, evaluator, CLI and docs, and
ran the tests, the evaluation suite and a scripted end-to-end play-through of
the Streamlit app. I reviewed the design, the evaluation numbers and these
write-ups.

**A suggestion I accepted: validator-first design, with the model kept blind
to the answer range.** The AI proposed that Claude should receive only the
raw hint history, not the computed interval of possible secrets, and that a
deterministic validator should check every guess. I accepted it because it
gives the model real reasoning to do and gives the check real work to do. If
the model were handed the interval, the validator would almost never fire and
the "agentic" loop would be theatre. It also made the system measurable: every
accept, reject, retry and fallback is countable.

**A suggestion I did not accept as written: the hard-coded reliability
metric.** The AI's first version of `evaluate.py` hard-coded
`invalid_guesses_played` to 0, with a comment saying it was guaranteed "by
construction". The claim was *true* for the honest game modes, but a metric
that can't fail isn't measuring anything. It just repeats the design's
assumptions. I had it replaced with an independent check that replays the true
hints. That change exposed the 20 misled guesses in glitch mode and led to
splitting the metric in two (see "What surprised me" above). This is the same
lesson as Module 1, where I rejected an AI fix that coerced types with `int()`
inside `check_guess`: a check that silently can't fail hides problems instead
of catching them.

**What I'd keep doing.** Using AI to generate the scaffolding and first
drafts was fast. The valuable human work was asking "how would I know if this
were wrong?" about each claim it made, and then making the code answer that
question.
