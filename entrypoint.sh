#!/bin/bash
set -e

echo "[entrypoint] hermes starting..."

# ─── 1. ZCode config (generated from env vars — survives rebuilds) ────────────
mkdir -p /root/.zcode/cli /root/.zcode/v2

# The ZCODE_API_KEY env var is set via Coolify. Generate the config that makes
# headless ZCode work (the format we reverse-engineered for v0.15.2).
ZCODE_KEY="${ZCODE_API_KEY:-}"
if [ -n "$ZCODE_KEY" ]; then
  python3 -c "
import json, os
config = {
    'model': 'builtin:zai-coding-plan/GLM-5.2',
    'provider': {
        'builtin:zai-coding-plan': {
            'name': 'Z.ai - Coding Plan',
            'kind': 'anthropic',
            'options': {
                'baseURL': 'https://api.z.ai/api/anthropic',
                'apiKey': os.environ['ZCODE_API_KEY'],
                'apiKeyRequired': True
            },
            'enabled': True,
            'source': 'custom',
            'models': {
                'GLM-5.2': {
                    'limit': {'context': 1000000},
                    'modalities': {'input': ['text'], 'output': ['text']}
                }
            }
        }
    },
    'mcp': {}
}
# Add brain MCP if BRAIN_URL is set
brain_url = os.environ.get('BRAIN_URL', '')
brain_token = os.environ.get('BRAIN_BEARER_TOKEN', '')
if brain_url and brain_token:
    config['mcp']['brain'] = {
        'type': 'remote',
        'url': brain_url.rstrip('/') + '/mcp',
        'headers': {'Authorization': 'Bearer ' + brain_token},
        'enabled': True
    }
with open('/root/.zcode/cli/config.json', 'w') as f:
    json.dump(config, f, indent=2)
print('[entrypoint] zcode config generated from env vars')
"
else
  echo "[entrypoint] WARNING: ZCODE_API_KEY not set — zcode headless won't work"
fi

# ─── 2. Hermes config (model + dashboard auth) ────────────────────────────────
HERMES_CFG="/root/.hermes/config.yaml"
if [ ! -f "$HERMES_CFG" ] || ! grep -q "model:" "$HERMES_CFG" 2>/dev/null; then
  python3 -c "
from plugins.dashboard_auth.basic import hash_password
import yaml, os

config = {
    'model': 'GLM-5.2',
    'dashboard': {
        'basic_auth': {
            'username': 'ops',
            'password_hash': hash_password(os.environ.get('HERMES_DASHBOARD_PASSWORD', 'hermes-ops-2026'))
        }
    }
}

# Merge with existing if present
try:
    with open('$HERMES_CFG') as f:
        existing = yaml.safe_load(f)
    if existing:
        existing.update(config)
        config = existing
except:
    pass

with open('$HERMES_CFG', 'w') as f:
    yaml.dump(config, f)
print('[entrypoint] hermes config.yaml created (model=GLM-5.2 + dashboard auth)')
"
fi

# ─── 3. Hermes API keys in .env ───────────────────────────────────────────────
ENV_FILE="/root/.hermes/.env"
touch "$ENV_FILE"
if ! grep -q "ANTHROPIC_API_KEY=" "$ENV_FILE" 2>/dev/null; then
  if [ -n "$ZCODE_API_KEY" ]; then
    echo "ANTHROPIC_API_KEY=$ZCODE_API_KEY" >> "$ENV_FILE"
    echo "ANTHROPIC_BASE_URL=https://api.z.ai/api/anthropic" >> "$ENV_FILE"
    echo "[entrypoint] hermes API keys written to .env"
  fi
fi

# ─── 4. Dev-loop skills ───────────────────────────────────────────────────────
SKILL_DIR="/root/.hermes/skills/software-development"
mkdir -p "$SKILL_DIR"
if [ -d "/opt/skills" ]; then
  cp -r /opt/skills/* "$SKILL_DIR/" 2>/dev/null || true
  echo "[entrypoint] dev-loop skills copied"
fi

# ─── 5. Initialize hermes state if fresh ──────────────────────────────────────
if [ ! -f "/root/.hermes/state.db" ]; then
  echo "[entrypoint] initializing hermes state..."
  hermes setup --non-interactive 2>/dev/null || true
fi

# ─── 6. Start hermes dashboard (web UI on 9119) ───────────────────────────────
# hermes dashboard serves the web UI that hermes.appdeed.com routes to.
# It also starts the backend API (JSON-RPC/WebSocket) internally.
hermes dashboard --host 0.0.0.0 --port 9119 --insecure --skip-build --no-open &
echo "[entrypoint] hermes dashboard started on 0.0.0.0:9119"

# ─── 7. Start the gateway (foreground — the dispatcher loop) ──────────────────
exec hermes gateway run
