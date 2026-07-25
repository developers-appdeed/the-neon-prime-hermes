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
SKILL_PATH = Path(__file__).resolve().parent / "skills" / "code-explainer" / "SKILL.md"

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
    raw = SKILL_PATH.read_text()
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
    pending_marker: Optional[str] = None

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
