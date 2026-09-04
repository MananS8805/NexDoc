# ADR 0004: fact-recoverability eval over LLM-graded eval, for now

## Status

Accepted, implemented in [`eval/run_eval.py`](../../eval/run_eval.py). Superseded-later candidate — see "Revisit when."

## Context

CLAUDE.md's Phase 1 spec describes the eval as questions "answered by an
LLM given (a) the flattened baseline and (b) the structured JSON," scored
for accuracy — a task-accuracy eval. An earlier draft of `run_eval.py`
implemented exactly that, calling the Anthropic API per question per
context. It requires `ANTHROPIC_API_KEY`, which was not available in the
environment this was built in.

CLAUDE.md's own guardrail is explicit and takes priority over matching the
original spec literally: "Never present a fabricated or estimated number as
measured." Exiting with an error and blocking all eval work on a missing
credential was the strictly-correct-but-unhelpful option; the credential
being unavailable doesn't change whether *some* real, honestly-scoped
number is worth having now.

## Decision

Rebuilt the eval as a deterministic fact-lookup check: for each question,
look up the exact cell/region it's about in both representations and check
whether the expected fact is present there at all. No model call, no API
key, fully reproducible offline. This measures something real and
genuinely weaker than task accuracy — see
[`docs/eval-methodology.md`](../eval-methodology.md) for the precise
distinction — and says so explicitly everywhere the number is shown
(README, UI prototype's proof-note, this ADR).

## Consequences

- The number produced (0% baseline / 100% structured on the test fixture)
  is real and reproducible, but it is not evidence that an LLM answers
  these questions better given structured input — that's a different,
  untested claim. Every place this number appears is worded to reflect
  that (`docs/eval-methodology.md`'s "read every number against
  distinction (1), not (2)").
- The eval runs in under a second with zero external dependencies, which
  is a genuine advantage independent of the API-key situation — it can run
  in CI, in an offline environment, or as a fast pre-check before spending
  API budget on a heavier task-accuracy eval later.
- `eval/qa_dataset.json`'s `check` field (`cell_value`, `cross_sheet_ref`,
  `unresolved_reason`) was designed to name what's being tested
  structurally, specifically so a future task-accuracy eval could reuse the
  same question set rather than starting over.

## Revisit when

An `ANTHROPIC_API_KEY` (or equivalent) is available and the user
explicitly authorizes its use for this purpose — CLAUDE.md's non-goals list
is explicit that no LLM API calls happen anywhere in this project without
that authorization. At that point, a task-accuracy eval alongside (not
necessarily replacing) this one would answer the question this one
deliberately doesn't: not just "is the fact present," but "does having it
actually improve what a model does with it."
