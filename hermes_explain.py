"""POST /api/explain — streaming, layered code explanation.

Standalone FastAPI app. Mounted into hermes_cli/web_server.py in production
(see Task 4). Kept separate so it can be tested in isolation.

Contract: docs/superpowers/specs/2026-07-26-brain-explain-layered-synthesis-design.md
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import AsyncGenerator, Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

# Re-exported so tests can monkeypatch the class.
from run_agent import AIAgent  # noqa: E402

_LAYER_RE = re.compile(r"<<layer:(business_logic|architecture|database|micro)>>")

# Repo-relative skill path. Works in the git checkout (where this file sits at
# the repo root next to skills/). In production the entrypoint copies this
# module into the installed hermes_cli/ package dir, where this path no longer
# resolves — see _resolve_skill_path() for the production fallback.
_REPO_SKILL_PATH = Path(__file__).resolve().parent / "skills" / "code-explainer" / "SKILL.md"


def _resolve_skill_path() -> Path:
    """Resolve the code-explainer SKILL.md location, handling both the
    production install layout and the in-repo test layout.

    Production layout: the entrypoint (entrypoint.sh ~line 102) copies the
    bundled skills tree into the hermes skills hub at
    ``<skills_dir>/software-development/code-explainer/SKILL.md``, where
    ``<skills_dir>`` is ``~/.hermes/skills`` (or ``$HERMES_HOME/skills`` under
    a non-default profile). This module itself is copied into the installed
    ``hermes_cli/`` package dir, so ``Path(__file__).parent`` is NOT the repo
    root in production and the historical repo-relative path
    (``_REPO_SKILL_PATH``) does not exist there.

    Approach: prefer the hermes skills hub (resolved via the same
    ``hermes_constants.get_skills_dir()`` the rest of hermes uses, so profile
    switches and HERMES_HOME overrides just work); fall back to the
    repo-relative path so this module keeps working from a source checkout
    (and so the test suite, which has no installed copy of the skill, can
    still load it). Raise FileNotFoundError with both candidates if neither
    exists — a silent wrong path is exactly the production bug we're fixing.
    """
    candidates: list[Path] = []
    try:
        from hermes_constants import get_skills_dir
        candidates.append(get_skills_dir() / "software-development" / "code-explainer" / "SKILL.md")
    except Exception:
        # hermes_constants unavailable (e.g. running outside the venv) — skip
        # the hub path and rely on the repo-relative fallback below.
        pass
    candidates.append(_REPO_SKILL_PATH)
    for p in candidates:
        if p.is_file():
            return p
    raise FileNotFoundError(
        "code-explainer SKILL.md not found. Tried: "
        + ", ".join(str(c) for c in candidates)
    )


# Set by tests to script agent deltas. Production code leaves this None.
_DELTAS: Optional[list[str]] = None


def _set_deltas(value):  # tests monkeypatch this
    global _DELTAS
    _DELTAS = value


class ExplainRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=500)
    repo: str = Field(..., min_length=1)


def _load_skill_body() -> str:
    """Read the code-explainer SKILL.md body (after the frontmatter)."""
    raw = _resolve_skill_path().read_text()
    # Split off YAML frontmatter if present.
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) >= 3:
            return parts[2].lstrip()
    return raw


def _build_agent(question: str, repo: str, enqueue):
    """Build an AIAgent for one explanation. Mirrors oneshot._run_agent
    (hermes_cli/oneshot.py:313) but wires stream_delta_callback and uses
    the code-explainer skill as the prompt body."""
    from hermes_cli.config import load_config
    from hermes_cli.runtime_provider import resolve_runtime_provider

    cfg = load_config()
    model_cfg = cfg.get("model") or {}
    if isinstance(model_cfg, str):
        effective_model = model_cfg
    else:
        effective_model = model_cfg.get("default") or model_cfg.get("model") or ""

    runtime = resolve_runtime_provider(requested=None, target_model=effective_model or None)

    # Fallback chain + clarify shim — same two-line wiring oneshot uses
    # (oneshot.py:401/414 and 426/439). The code-explainer skill never
    # triggers a clarify in v1, but fallback_model is cheap insurance
    # against a primary-model outage. NOTE: we do NOT replicate the
    # HERMES_INFERENCE_MODEL env override or detect_provider_for_model
    # auto-detection from oneshot.py:339/381 here — those are only
    # meaningful when the caller is overriding the model, which the
    # explain endpoint never does (it always uses the configured default).

    # Read-only MCP toolset per spec §5.2. Plan mode is enforced by the
    # allow-list + role discipline; hermes' AIAgent doesn't take a --mode flag
    # directly (that's a ZCode concept), so the skill text carries the rule.
    toolsets = [
        "Read", "Grep",
        "mcp__brain__query_graph", "mcp__brain__explain", "mcp__brain__get_node",
        "mcp__brain__get_neighbors", "mcp__brain__get_community",
        "mcp__brain__god_nodes", "mcp__brain__shortest_path",
        "mcp__postgres-tnp-dev__execute_sql", "mcp__postgres-tnp-prod__execute_sql",
        "mcp__redis-tnp-dev__get", "mcp__redis-tnp-dev__hgetall",
        "mcp__redis-tnp-prod__get", "mcp__redis-tnp-prod__hgetall",
        "mcp__grafana__query_loki_logs", "mcp__grafana__query_prometheus",
    ]

    session_db = None  # ephemeral; no persistence
    skill_body = _load_skill_body()
    prompt = (
        f"{skill_body}\n\n"
        f"---\n\nExplain, in repo `{repo}`, the following:\n\n{question}\n"
    )

    from hermes_cli.fallback_config import get_fallback_chain
    from hermes_cli.oneshot import _oneshot_clarify_callback
    _fb = get_fallback_chain(cfg)

    agent = AIAgent(
        api_key=runtime.get("api_key"),
        base_url=runtime.get("base_url"),
        provider=runtime.get("provider"),
        api_mode=runtime.get("api_mode"),
        model=effective_model,
        enabled_toolsets=toolsets,
        quiet_mode=True,
        platform="cli",
        session_db=session_db,
        credential_pool=runtime.get("credential_pool"),
        fallback_model=_fb or None,
        clarify_callback=_oneshot_clarify_callback,
        stream_delta_callback=enqueue,
    )
    agent.suppress_status_output = True
    agent.tool_gen_callback = None
    return agent, prompt


async def _explain_stream(req: ExplainRequest) -> AsyncGenerator[bytes, None]:
    """Run the agent in a thread, parse its delta stream, emit SSE frames."""
    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()
    layers_seen: list[str] = []

    def emit_sse(event: str, data: dict) -> bytes:
        return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()

    def enqueue(delta: str):
        # Called from the agent thread. Push raw delta to the queue; the
        # async loop parses it below.
        asyncio.run_coroutine_threadsafe(queue.put(("delta", delta)), loop)

    def run_agent():
        try:
            if _DELTAS is not None:
                # Test path: replay scripted deltas.
                for d in _DELTAS:
                    enqueue(d)
            else:
                agent, prompt = _build_agent(req.question, req.repo, enqueue)
                agent.run_conversation(prompt)
            asyncio.run_coroutine_threadsafe(queue.put(("done", None)), loop)
        except Exception as e:  # noqa: BLE001
            asyncio.run_coroutine_threadsafe(queue.put(("error", str(e))), loop)

    # Heartbeat task.
    async def heartbeat():
        while True:
            await asyncio.sleep(20)
            await queue.put(("heartbeat", None))

    hb_task = asyncio.create_task(heartbeat())
    import threading
    thread = threading.Thread(target=run_agent, daemon=True)
    thread.start()

    try:
        while True:
            kind, payload = await queue.get()
            if kind == "heartbeat":
                yield b": heartbeat\n\n"
                continue
            if kind == "error":
                yield emit_sse("error", {"message": str(payload)})
                return
            if kind == "done":
                yield emit_sse("done", {"layers": layers_seen})
                return
            # kind == "delta" — parse layer markers out of the chunk.
            assert isinstance(payload, str)
            text = payload
            while True:
                m = _LAYER_RE.search(text)
                if not m:
                    break
                layer = m.group(1)
                if layer not in layers_seen:
                    layers_seen.append(layer)
                # Emit the marker as a layer event, then continue with the
                # text after it as a potential token.
                yield emit_sse("layer", {"name": layer})
                text = text[m.end():]
                # Strip a leading newline left behind by the marker line.
                if text.startswith("\n"):
                    text = text[1:]
            if text:
                yield emit_sse("token", {"text": text})
    finally:
        hb_task.cancel()


def build_explain_app() -> FastAPI:
    app = FastAPI(title="hermes-explain")

    @app.post("/api/explain")
    async def explain(request: Request):
        # Auth: dashboard session cookie. Mirrors kanban plugin routes.
        cookie = request.headers.get("cookie", "")
        if "hermes_session_at=" not in cookie:
            raise HTTPException(status_code=401, detail="not authenticated")
        body = await request.json()
        req = ExplainRequest(**body)
        return StreamingResponse(
            _explain_stream(req),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return app
