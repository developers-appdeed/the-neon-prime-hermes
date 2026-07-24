# Hermes Agent — Deployment Guide

## What this runs

The Hermes gateway (orchestrator + kanban + agents) with ZCode as the coding executor.
The 3 dev-loop skills (coordinator, developer, zcode-executor) are baked into the image.

## Deploy steps

### 1. Build and start the container

```bash
cd /opt/hermes  # (or wherever Coolify checks out the repo)
# Copy zcode.cjs from a machine that has the ZCode desktop app:
#   cp /Applications/ZCode.app/Contents/Resources/glm/zcode.cjs ./
docker compose build
docker compose up -d
```

### 2. Authenticate ZCode (one-time, interactive)

ZCode v0.15.2 headless requires OAuth credentials. Run login inside the container:

```bash
docker exec -it hermes zcode login --no-browser
```

This prints a URL. Open it in a browser on your laptop, authorize with Z.AI,
and the tokens are stored in `/data/zcode/v2/credentials.json` (persistent volume).

**Token refresh:** ZCode uses refresh tokens automatically. If auth expires,
re-run the login command. The credentials persist in the mounted volume.

### 3. Configure the hermes gateway

Set these via Coolify env (or mount a config file):
- `BRAIN_URL` — the brain MCP URL (http://brain:8000)
- `BRAIN_BEARER_TOKEN` — the brain auth token
- Model provider for hermes itself (separate from ZCode) — set via `hermes model`

### 4. Verify

```bash
# Hermes health
docker exec hermes curl -sf http://localhost:8000/health

# ZCode works headless
docker exec hermes zcode --version

# Skills loaded
docker exec hermes hermes skills list
```

## Key findings (load-bearing)

1. **ZCode `--settings` flag is NOT implemented in v0.15.2** — the parser rejects it.
   Config must be at the default path (`$ZCODE_DATA_BASE_DIR/cli/config.json`).
2. **ZCode headless requires OAuth credentials** (`credentials.json`), not just a config file
   with a provider block. The `zcode login --no-browser` flow is the intended path.
3. **`--max-turns` flag is rejected by the parser** when combined with certain other flags.
   The dev-loop-developer skill works around this by using `--prompt` without `--max-turns`
   and relying on the session's natural completion.
4. **zcode.cjs is 11.5MB** — gitignored, copied into the build context at build time.
