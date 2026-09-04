"""Real task-accuracy Q&A: given a textual representation of an artifact
(the flattened baseline, or the structured common-schema JSON) and a
natural-language question, calls a real Groq-hosted LLM and returns its
free-text answer.

This is the module `docs/eval-methodology.md` describes as not existing
yet — "task accuracy" (does a model actually answer correctly), as opposed
to `eval/run_eval.py`'s "fact-recoverability" (is the fact present at all,
checked with no model involved). It is deliberately small and
single-purpose: it does not know about `qa_dataset.json`'s shape, scoring,
or adapters. Two callers share it without duplicating the Groq-calling
logic:

- `eval/run_real_eval.py` — calls `ask()` twice per question (once with the
  baseline flattened text, once with the structured JSON) and scores each
  answer against `qa_dataset.json`'s `expect_substring`, producing the real
  baseline-vs-structured task-accuracy numbers this project has not had
  until now.
- `webapp/main.py`'s `POST /api/ask` — calls `ask()` once, with the
  structured JSON of whatever artifact the user just analyzed in the
  browser, so a person can ask an ad-hoc question interactively.

Why the structured representation handed to the model is `to_structured()`
(the region list), not `to_text()`'s flattened join or `to_json()`'s full
schema: `to_text()` is itself just another flattening (regions joined by
newlines with structure discarded) and wouldn't test anything different
from the CSV baseline. `to_json()` adds `source`/`checks` metadata that
carries no signal for answering a factual question. `to_structured()` is
the actual claim CLAUDE.md's problem statement makes worth testing: each
region typed, confidence-scored, and carrying its `relationships`
(formula-ref, cross-sheet-ref, connects-to, ...) — the exact information a
flat text dump throws away. Handing the model *that* is the real test of
whether preserving structure helps a model answer correctly, which is a
strictly stronger and different claim than "the fact is present somewhere
in this blob" (what `run_eval.py` already measures).

Handling a missing or invalid `GROQ_API_KEY` mirrors
`adapters/common/ocr.py`'s `tesseract_status()` pattern: report clearly,
never crash, never silently fabricate an answer.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

# Loads repo-root .env into os.environ if present; a no-op (not an error) if
# it doesn't exist, and never overrides a variable already set in the real
# environment (e.g. exported in the shell, or set by CI).
load_dotenv(ROOT / ".env")

# Picked from https://console.groq.com/docs/models (checked 2026-09-04):
# openai/gpt-oss-120b is Groq's flagship general-purpose production text
# model at the time this was written. Groq's model lineup changes over
# time, so this is overridable via GROQ_MODEL rather than hardcoded as the
# only option.
DEFAULT_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")

SYSTEM_PROMPT = (
    "You are answering a question about one artifact (a spreadsheet, document, "
    "or diagram) using ONLY the representation of it provided below. Answer "
    "concisely and directly, in plain text — no markdown formatting, no "
    "preamble. If the representation does not contain enough information to "
    "answer, say so plainly instead of guessing."
)


class GroqQAError(RuntimeError):
    """Base class for a handled failure in the Groq Q&A path — raised
    instead of letting the Groq SDK's own exception (or a KeyError on a
    missing env var) propagate as a raw crash."""


class GroqNotConfiguredError(GroqQAError):
    """Raised when GROQ_API_KEY is missing/empty, or Groq itself rejects it
    as invalid/expired. Callers (eval script, /api/ask) catch this and show
    its message directly instead of a stack trace or a fabricated answer."""


def api_key_status() -> tuple[bool, str]:
    """Returns (available, detail) with no network call — mirrors
    `adapters/common/ocr.py`'s `tesseract_status()` shape so callers can
    check availability up front (e.g. to decide whether to even show the
    "Ask a question" box) the same way the document/diagram adapters check
    for Tesseract before attempting OCR.
    """
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if not key:
        return False, (
            "GROQ_API_KEY is not set. Copy .env.example to .env at the repo "
            "root and fill in a real Groq API key (https://console.groq.com/keys) "
            "to enable Groq-backed Q&A."
        )
    return True, "GROQ_API_KEY is set."


def _client() -> Any:
    from groq import Groq  # imported lazily so importing this module never requires network/creds

    available, detail = api_key_status()
    if not available:
        raise GroqNotConfiguredError(detail)
    return Groq(api_key=os.environ["GROQ_API_KEY"])


def ask(question: str, context: str, *, model: str | None = None, client: Any | None = None) -> str:
    """Sends `context` (some textual representation of an artifact) plus
    `question` to a Groq chat-completion model and returns its free-text
    answer.

    `client` exists so tests can inject a fake Groq client — anything
    exposing `.chat.completions.create(...)` matching the real SDK's
    response shape (`response.choices[0].message.content`) — and verify the
    prompt-building and response-parsing logic without a network call or a
    real API key. Production callers never pass it; a real `Groq()` client
    is built from `GROQ_API_KEY` when omitted.

    Raises `GroqNotConfiguredError` if no key is configured, or if Groq
    itself reports the key is invalid/unauthorized. Raises the more general
    `GroqQAError` for any other Groq API failure (network, rate limit,
    model error). Either way: a clear, honest message, never a raw
    traceback and never a silently wrong/fabricated answer.
    """
    if client is None:
        client = _client()

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"Representation of the artifact:\n\n{context}\n\nQuestion: {question}",
        },
    ]

    try:
        response = client.chat.completions.create(
            model=model or DEFAULT_MODEL,
            messages=messages,
            temperature=0,
        )
    except Exception as exc:
        try:
            from groq import AuthenticationError
        except ImportError:  # pragma: no cover — groq always defines this; defensive only
            AuthenticationError = ()  # type: ignore[assignment]
        if isinstance(exc, AuthenticationError):
            raise GroqNotConfiguredError(
                f"Groq rejected the configured GROQ_API_KEY as invalid: {exc}"
            ) from exc
        raise GroqQAError(f"Groq API call failed: {type(exc).__name__}: {exc}") from exc

    answer = response.choices[0].message.content
    return (answer or "").strip()
