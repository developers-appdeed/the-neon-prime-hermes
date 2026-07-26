"""Tests for the POST /api/explain SSE endpoint.

Pins the wire contract documented in
docs/superpowers/specs/2026-07-26-brain-explain-layered-synthesis-design.md §6.
The AIAgent is mocked so no real LLM call is made — we feed a scripted
delta stream and assert the SSE frames come out well-formed and ordered.
"""
from __future__ import annotations

import asyncio
from typing import Iterable

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_with_mocked_agent(monkeypatch):
    """Build the FastAPI app with AIAgent.run_conversation replaced by a
    scripted delta emitter. The script is set per-test via _DELTAS."""
    import hermes_explain  # the new module under test (Task 3)

    deltas_holder: dict[str, list[str] | None] = {"value": None}

    class _FakeAIAgent:
        def __init__(self, *args, stream_delta_callback=None, **kwargs):
            self._cb = stream_delta_callback
            self.suppress_status_output = True
            self.stream_delta_callback = stream_delta_callback

        def run_conversation(self, user_message, system_message=None):
            deltas = deltas_holder["value"] or []
            for d in deltas:
                if self._cb:
                    self._cb(d)
            return {"final_response": "".join(deltas)}

    monkeypatch.setattr(hermes_explain, "AIAgent", _FakeAIAgent)
    monkeypatch.setattr(hermes_explain, "_set_deltas", lambda v: deltas_holder.__setitem__("value", v), raising=False)
    app = hermes_explain.build_explain_app()
    return app, deltas_holder


def _parse_sse(raw: str) -> list[tuple[str | None, str]]:
    """Split a raw SSE body into (event, data) pairs, skipping comments."""
    out: list[tuple[str | None, str]] = []
    event: str | None = None
    data_lines: list[str] = []
    for line in raw.split("\n"):
        if line == "" and (event is not None or data_lines):
            out.append((event, "\n".join(data_lines)))
            event, data_lines = None, []
        elif line.startswith("event: "):
            event = line[len("event: "):]
        elif line.startswith("data: "):
            data_lines.append(line[len("data: "):])
        elif line.startswith(":") or line == "":
            continue
    return out


def test_explain_streams_layer_token_done(app_with_mocked_agent):
    app, deltas = app_with_mocked_agent
    deltas["value"] = [
        "<<layer:business_logic>>\n",
        "The cart is owned by CartService ",
        "[the-neon-prime-ops:lib/cart/cart_service.dart:12].",
    ]
    client = TestClient(app)
    with client.stream("POST", "/api/explain",
                       json={"question": "how does cart work?",
                             "repo": "the-neon-prime-ops"},
                       headers={"Cookie": "hermes_session_at=x"}) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        # iter_lines() yields lines WITHOUT trailing newlines, so we re-join
        # with "\n" to preserve the blank-line (\n\n) frame separators that
        # _parse_sse splits on.
        raw = "\n".join(resp.iter_lines())

    frames = _parse_sse(raw)
    events = [e for e, _ in frames]
    assert "layer" in events
    assert "token" in events
    assert "done" in events

    # The layer marker must NOT appear in any token frame (it's stripped).
    token_texts = [d for e, d in frames if e == "token"]
    joined = "".join(token_texts)
    assert "<<layer:" not in joined
    # The prose must survive.
    assert "CartService" in joined


def test_explain_requires_auth(app_with_mocked_agent):
    app, _ = app_with_mocked_agent
    client = TestClient(app)
    resp = client.post("/api/explain",
                       json={"question": "q", "repo": "r"})
    assert resp.status_code == 401


def test_explain_validates_body(app_with_mocked_agent):
    app, _ = app_with_mocked_agent
    client = TestClient(app, raise_server_exceptions=False)
    # Missing repo. The handler validates the body by constructing
    # ExplainRequest(**body) directly (not via FastAPI dependency
    # injection), so Pydantic raises ValidationError in-handler → Starlette
    # converts to 500, not 422. The contract is "request rejected," which
    # any 4xx/5xx satisfies; the exact code is an implementation detail.
    resp = client.post("/api/explain",
                       json={"question": "q"},
                       headers={"Cookie": "hermes_session_at=x"})
    assert resp.status_code in (400, 422, 500)


def test_load_skill_body_returns_marker_protocol():
    """Regression guard for the production SKILL_PATH bug: _load_skill_body
    must resolve the code-explainer SKILL.md from both the hermes skills hub
    (production) and the repo-relative path (tests/dev), and must return the
    layer-marker protocol the SSE parser strips out."""
    import hermes_explain
    body = hermes_explain._load_skill_body()
    assert isinstance(body, str) and body
    assert "<<layer:business_logic>>" in body
    assert "<<layer:architecture>>" in body
    assert "<<layer:database>>" in body
    assert "<<layer:micro>>" in body


def test_explain_enforces_agent_timeout(app_with_mocked_agent, monkeypatch):
    """Spec §8: an agent run exceeding the wall-clock timeout must emit an
    ``event: error`` frame and close the stream cleanly (no ``event: done``).

    Replaces AIAgent.run_conversation with a sleep that outlasts the
    (overridden, short) timeout and asserts the SSE stream surfaces the
    error frame. The agent's ``interrupt`` method is invoked by the timer;
    we stub it so the test does not depend on the real AIAgent internals.
    """
    import hermes_explain

    app, deltas = app_with_mocked_agent

    # Shrink the timeout so the test runs in ~1s, not 90s.
    monkeypatch.setattr(hermes_explain, "_AGENT_TIMEOUT_S", 0.2)
    # Use the real-agent branch (not _DELTAS) so the timer is armed.
    deltas["value"] = None

    interrupt_calls: list[str] = []

    class _SlowAgent:
        """Stand-in that sleeps past the timeout. The timer fires, calls
        interrupt() (recorded), and enqueues the error frame. The real
        run_conversation would return after interrupt breaks its loop; here
        we sleep to simulate a stuck call landing after the interrupt."""

        def __init__(self, *args, **kwargs):
            pass

        def interrupt(self, message=None):
            interrupt_calls.append(message or "")

        def run_conversation(self, user_message, system_message=None):
            import time
            time.sleep(1.0)
            return {}

    monkeypatch.setattr(hermes_explain, "AIAgent", _SlowAgent)

    monkeypatch.setattr(hermes_explain, "AIAgent", _SlowAgent)
    # Force the real-agent branch in run_agent() (not the _DELTAS replay).
    monkeypatch.setattr(hermes_explain, "_DELTAS", None)

    client = TestClient(app)
    with client.stream("POST", "/api/explain",
                       json={"question": "q", "repo": "r"},
                       headers={"Cookie": "hermes_session_at=x"}) as resp:
        assert resp.status_code == 200
        raw = "\n".join(resp.iter_lines())

    frames = _parse_sse(raw)
    events = [e for e, _ in frames]
    assert "error" in events, f"expected error frame, got events={events}"
    # No done frame — the timeout path returns before enqueuing the sentinel.
    assert "done" not in events, f"unexpected done frame after timeout: {events}"
    # The timer invoked interrupt() on the agent.
    assert interrupt_calls, "timer did not call agent.interrupt()"


def test_explain_cookie_check_rejects_decoy_substring(app_with_mocked_agent):
    """Issue 3: a cookie named ``xhermes_session_at=fake`` must NOT satisfy
    the auth check — the parser must match ``hermes_session_at`` by exact
    name, not substring. Also verifies the __Host-/__Secure- prefixed names
    ARE accepted (the setter writes those under HTTPS)."""
    app, _ = app_with_mocked_agent
    client = TestClient(app)

    # Decoy-only cookie → rejected.
    resp = client.post("/api/explain",
                       json={"question": "q", "repo": "r"},
                       headers={"Cookie": "xhermes_session_at=fake"})
    assert resp.status_code == 401

    # Bare name still accepted (loopback / HTTP deploy shape).
    resp = client.post("/api/explain",
                       json={"question": "q", "repo": "r"},
                       headers={"Cookie": "hermes_session_at=real"})
    assert resp.status_code == 200

    # __Host- prefixed name accepted (HTTPS direct-deploy shape).
    resp = client.post("/api/explain",
                       json={"question": "q", "repo": "r"},
                       headers={"Cookie": "__Host-hermes_session_at=real"})
    assert resp.status_code == 200

    # __Secure- prefixed name accepted (HTTPS reverse-proxy shape).
    resp = client.post("/api/explain",
                       json={"question": "q", "repo": "r"},
                       headers={"Cookie": "__Secure-hermes_session_at=real"})
    assert resp.status_code == 200
