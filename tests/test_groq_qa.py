"""Tests for qa/groq_qa.py — the real task-accuracy Q&A path.

No live Groq call happens anywhere in this file (no test here needs, or
should need, a real GROQ_API_KEY): the "happy path" and "bad key" tests
inject a fake client that mimics the real SDK's shape
(`.chat.completions.create(...)` returning something with
`.choices[0].message.content`), proving the prompt-building and
response-parsing plumbing is correct without a network call. The "no key
configured at all" test needs no mock — it's a real, honestly-testable
behavior: unset the env var and confirm the module reports it clearly
instead of crashing.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from qa.groq_qa import GroqNotConfiguredError, GroqQAError, api_key_status, ask


class _FakeCompletions:
    """Stands in for `groq.Groq().chat.completions`. Records exactly what it
    was called with so the test can assert the real prompt shape, and either
    returns a canned response or raises whatever the test wants the
    underlying SDK to raise.
    """

    def __init__(self, content: str | None = None, to_raise: Exception | None = None):
        self.content = content
        self.to_raise = to_raise
        self.last_call: dict | None = None

    def create(self, **kwargs):
        self.last_call = kwargs
        if self.to_raise is not None:
            raise self.to_raise
        message = SimpleNamespace(content=self.content)
        choice = SimpleNamespace(message=message)
        return SimpleNamespace(choices=[choice])


def _fake_client(content: str | None = None, to_raise: Exception | None = None):
    completions = _FakeCompletions(content=content, to_raise=to_raise)
    return SimpleNamespace(chat=SimpleNamespace(completions=completions)), completions


# ---------- no key configured: real behavior, no mock needed ----------


def test_api_key_status_reports_missing_key_clearly(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    available, detail = api_key_status()
    assert available is False
    assert "GROQ_API_KEY" in detail
    assert detail  # non-empty, human-readable


def test_api_key_status_true_when_set(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_fake_for_this_test_only")
    available, detail = api_key_status()
    assert available is True
    assert "set" in detail.lower()


def test_ask_without_a_key_raises_clear_error_not_a_crash(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(GroqNotConfiguredError) as excinfo:
        ask("what is the total?", "some context")
    assert "GROQ_API_KEY" in str(excinfo.value)


# ---------- happy path, mocked client: proves the plumbing ----------


def test_ask_builds_prompt_and_returns_parsed_answer():
    client, completions = _fake_client(content="  The total is 3900.  ")

    answer = ask("what is the total?", "E3: 3900", client=client)

    assert answer == "The total is 3900."  # stripped, not fabricated/reformatted

    assert completions.last_call is not None
    messages = completions.last_call["messages"]
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    # both the context and the literal question must actually reach the model
    assert "E3: 3900" in messages[1]["content"]
    assert "what is the total?" in messages[1]["content"]
    assert completions.last_call["temperature"] == 0


def test_ask_uses_default_model_unless_overridden():
    from qa.groq_qa import DEFAULT_MODEL

    client, completions = _fake_client(content="answer")
    ask("q", "ctx", client=client)
    assert completions.last_call["model"] == DEFAULT_MODEL

    client2, completions2 = _fake_client(content="answer")
    ask("q", "ctx", client=client2, model="some-other-model")
    assert completions2.last_call["model"] == "some-other-model"


def test_ask_handles_empty_content_without_crashing():
    client, _ = _fake_client(content=None)
    assert ask("q", "ctx", client=client) == ""


# ---------- error paths from the (mocked) Groq API itself ----------


def test_ask_wraps_authentication_error_as_not_configured():
    import httpx
    from groq import AuthenticationError

    request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    response = httpx.Response(401, request=request)
    auth_error = AuthenticationError("Invalid API Key", response=response, body=None)
    client, _ = _fake_client(to_raise=auth_error)

    with pytest.raises(GroqNotConfiguredError) as excinfo:
        ask("q", "ctx", client=client)
    assert "invalid" in str(excinfo.value).lower()


def test_ask_wraps_other_api_failures_as_groq_qa_error():
    client, _ = _fake_client(to_raise=RuntimeError("connection reset"))

    with pytest.raises(GroqQAError) as excinfo:
        ask("q", "ctx", client=client)
    assert "connection reset" in str(excinfo.value)
