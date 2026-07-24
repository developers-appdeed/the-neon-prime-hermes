# the-neon-prime-hermes

The Hermes Agent — the autonomous dev-loop orchestrator for The Neon Prime platform.

## What it runs

- **Hermes gateway** — the dispatcher loop that picks kanban cards and assigns agents
- **Hermes serve** — HTTP/WebSocket API (port 9119) for the fastify bridge to call
- **ZCode executor** — headless code generation via `zcode.cjs` (downloaded from the brain repo's GitHub release)
- **4 dev-loop skills**: coordinator, developer, tester, zcode-executor

## Architecture

Part of the Digital Brain Platform:
- Queries the **brain** (`http://brain:8000`) for code context
- Executes via **ZCode** (`zcode --prompt ... --mode yolo`)
- Talks to the **fastify bridge** (which the ops app connects to)

## Deploy

Deployed via Coolify as a docker-compose app. The entrypoint auto-configures
everything from env vars (`ZCODE_API_KEY`, `BRAIN_URL`, `BRAIN_BEARER_TOKEN`).

See [DEPLOY.md](./DEPLOY.md) for details.

## Metrics sidecar

`metrics-exporter/` contains a lightweight Prometheus exporter that reads the
hermes kanban DB (read-only) and exposes card/agent metrics on :9091.
