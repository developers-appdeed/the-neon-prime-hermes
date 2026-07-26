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
    'model': 'builtin:zai-coding-plan/glm-5.2',
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
                'glm-5.2': {
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
    'model': 'glm-5.2',
    'dashboard': {
        'basic_auth': {
            'username': 'ops',
            'password_hash': hash_password(os.environ.get('HERMES_DASHBOARD_PASSWORD', 'hermes-ops-2026'))
        }
    }
}

# Merge with existing if present (preserve any mcp_servers already present
# so a manual `hermes mcp add` isn't clobbered on container restart).
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
print('[entrypoint] hermes config.yaml ensured (model + dashboard auth)')
"
fi

# ─── 2b. MCP servers for the code-explainer agent (spec §5.2) ────────────────
# ALWAYS RUNS (not gated on first-time config creation). The config.yaml
# persists across deploys via the ./data/hermes volume, so the model/dashboard
# block above only fires once — but MCP wiring must reconcile on every start
# so adding/removing env vars (e.g. GRAFANA_SERVICE_ACCOUNT_TOKEN) takes effect
# without wiping the volume. Existing manually-added servers are preserved.
python3 -c "
import yaml, os
HERMES_CFG = '/root/.hermes/config.yaml'
# Load existing (or start fresh if somehow missing)
try:
    with open(HERMES_CFG) as f:
        config = yaml.safe_load(f) or {}
except Exception:
    config = {}

# All services are on the \`coolify\` docker network, which this container
# joins (see docker-compose.yml). Hostnames come from ds6c/services.env.
# We use \${ENV_VAR} interpolation — hermes resolves these from .env / env at
# config load time (tools/mcp_tool.py:_interpolate_env_vars). Secrets never
# land in the repo; they're injected via Coolify env.
#
# Read-only posture: the code-explainer's allow-list in hermes_explain.py
# only grants the *read* tool names (postgres execute_sql, redis get/hgetall,
# grafana query_*). The MCPs themselves are unrestricted at the DB layer
# (crystaldba/postgres-mcp --access-mode=unrestricted), so the allow-list is
# the enforcement boundary, not the MCP. This mirrors how the dev-loop-tester
# skill works (SKILL.md:57-58).
mcp = {}
# All services are on the `coolify` docker network, which this container
# joins (see docker-compose.yml). Hostnames come from ds6c/services.env.
# We use ${ENV_VAR} interpolation — hermes resolves these from .env / env at
# config load time (tools/mcp_tool.py:_interpolate_env_vars). Secrets never
# land in the repo; they're injected via Coolify env.
#
# Read-only posture: the code-explainer's allow-list in hermes_explain.py
# only grants the *read* tool names (postgres execute_sql, redis get/hgetall,
# grafana query_*). The MCPs themselves are unrestricted at the DB layer
# (crystaldba/postgres-mcp --access-mode=unrestricted), so the allow-list is
# the enforcement boundary, not the MCP. This mirrors how the dev-loop-tester
# skill works (SKILL.md:57-58).
mcp = {}

# Brain — already wired in the ZCode config above, but the code-explainer
# runs as a direct AIAgent (not through ZCode), so it needs the brain MCP
# registered here too.
if os.environ.get('BRAIN_URL') and os.environ.get('BRAIN_BEARER_TOKEN'):
    mcp['brain'] = {
        'type': 'http',
        'url': os.environ['BRAIN_URL'].rstrip('/') + '/mcp',
        'headers': {'Authorization': 'Bearer ${BRAIN_BEARER_TOKEN}'},
    }

# Postgres dev + prod — stdio docker containers, DATABASE_URI via env.
# Hostnames are the Coolify resource UUIDs (resolve on the coolify net).
if os.environ.get('DEV_POSTGRES_DB_URL'):
    mcp['postgres-tnp-dev'] = {
        'type': 'stdio',
        'command': 'docker',
        'args': ['run', '-i', '--rm', '--network', 'coolify',
                 '-e', 'DATABASE_URI',
                 'crystaldba/postgres-mcp', '--access-mode=unrestricted'],
        'env': {'DATABASE_URI': '${DEV_POSTGRES_DB_URL}'},
    }
if os.environ.get('PROD_POSTGRES_DB_URL'):
    mcp['postgres-tnp-prod'] = {
        'type': 'stdio',
        'command': 'docker',
        'args': ['run', '-i', '--rm', '--network', 'coolify',
                 '-e', 'DATABASE_URI',
                 'crystaldba/postgres-mcp', '--access-mode=unrestricted'],
        'env': {'DATABASE_URI': '${PROD_POSTGRES_DB_URL}'},
    }

# Redis dev + prod — stdio uvx.
# Package: redis-mcp (NOT redis-mcp-server — that one hard-pins numpy>=2.2.4,
# whose wheels require the X86_V2 baseline the Coolify host CPU lacks).
# redis-mcp is numpy-free, FastMCP-based. It reads REDIS_HOST/PORT/DB/PASSWORD/
# USERNAME from env (no --url flag), so we parse the DEV/PROD_REDIS_URL
# (redis://default:PWD@HOST:6379/0) here and pass the split fields in the
# MCP env block. urllib handles the parsing.
from urllib.parse import urlparse
def _redis_env(url_var):
    u = urlparse(os.environ.get(url_var, ''))
    if not u.hostname:
        return None
    env = {'REDIS_HOST': u.hostname, 'REDIS_PORT': str(u.port or 6379),
           'REDIS_DB': str((u.path or '/0')[1:] or '0')}
    if u.password:
        env['REDIS_PASSWORD'] = u.password
    if u.username and u.username != 'default':
        env['REDIS_USERNAME'] = u.username
    return env
if os.environ.get('DEV_REDIS_URL'):
    _re = _redis_env('DEV_REDIS_URL')
    if _re:
        mcp['redis-tnp-dev'] = {
            'type': 'stdio', 'command': 'uvx',
            'args': ['--from', 'redis-mcp', 'redis-mcp', '--transport', 'stdio'],
            'env': _re,
        }
if os.environ.get('PROD_REDIS_URL'):
    _re = _redis_env('PROD_REDIS_URL')
    if _re:
        mcp['redis-tnp-prod'] = {
            'type': 'stdio', 'command': 'uvx',
            'args': ['--from', 'redis-mcp', 'redis-mcp', '--transport', 'stdio'],
            'env': _re,
        }

# Grafana — CONDITIONAL. The grafana MCP needs a service-account token
# (GRAFANA_SERVICE_ACCOUNT_TOKEN) that isn't in services.env by default —
# admin user/pass won't work. Create one in Grafana UI
# (Configuration → Service Accounts → Add token, scope metrics:query + logs:query),
# add it to Coolify env for this app, redeploy. Until then the explainer's
# grafana layer is skipped gracefully (skill degrades per SKILL.md refusal section).
# Note: prometheus access also rides through here (mcp__grafana__query_prometheus),
# per the design decision to not register a separate prometheus MCP.
if os.environ.get('GRAFANA_SERVICE_ACCOUNT_TOKEN'):
    mcp['grafana'] = {
        'type': 'stdio',
        'command': 'uvx',
        'args': ['mcp-grafana'],
        'env': {
            'GRAFANA_URL': os.environ.get('GRAFANA_URL', 'http://ds6c-grafana:3000'),
            'GRAFANA_SERVICE_ACCOUNT_TOKEN': '${GRAFANA_SERVICE_ACCOUNT_TOKEN}',
        },
    }

if mcp:
    # Merge: don't overwrite a manually-added server with the same name.
    existing_mcp = config.get('mcp_servers', {}) or {}
    for name, cfg in mcp.items():
        existing_mcp.setdefault(name, cfg)
    config['mcp_servers'] = existing_mcp

with open(HERMES_CFG, 'w') as f:
    yaml.dump(config, f)
registered = list((config.get('mcp_servers') or {}).keys())
print(f'[entrypoint] hermes config.yaml reconciled; mcp_servers: {registered}')
"

# ─── 3. Hermes API keys in .env ───────────────────────────────────────────────
# Hermes routes glm-* models through its native Z.AI provider, which needs
# GLM_API_KEY (not ANTHROPIC_API_KEY). We set all the recognized env var names.
ENV_FILE="/root/.hermes/.env"
touch "$ENV_FILE"
if ! grep -q "GLM_API_KEY=" "$ENV_FILE" 2>/dev/null; then
  if [ -n "$ZCODE_API_KEY" ]; then
    echo "GLM_API_KEY=$ZCODE_API_KEY" >> "$ENV_FILE"
    echo "ZAI_API_KEY=$ZCODE_API_KEY" >> "$ENV_FILE"
    echo "Z_AI_API_KEY=$ZCODE_API_KEY" >> "$ENV_FILE"
    echo "[entrypoint] hermes GLM/ZAI API keys written to .env"
  fi
fi

# ─── 4. Dev-loop skills ───────────────────────────────────────────────────────
SKILL_DIR="/root/.hermes/skills/software-development"
mkdir -p "$SKILL_DIR"
if [ -d "/opt/skills" ]; then
  cp -r /opt/skills/* "$SKILL_DIR/" 2>/dev/null || true
  echo "[entrypoint] dev-loop skills copied"
fi

# ─── 4b. /api/explain endpoint (code-explainer feature) ──────────────────────
# Copy hermes_explain.py into the installed hermes_cli package so the
# `from hermes_cli.hermes_explain import build_explain_app` in web_server.py
# resolves, then idempotently inject the mount call into web_server.py.
# See patches/web_server-explain-mount.patch for the human-readable record.
if [ -f /opt/hermes_explain.py ]; then
  HERMES_PKG_DIR=$(python3 -c "import hermes_cli, os; print(os.path.dirname(hermes_cli.__file__))" 2>/dev/null || echo "")
  if [ -n "$HERMES_PKG_DIR" ] && [ -d "$HERMES_PKG_DIR" ]; then
    cp /opt/hermes_explain.py "$HERMES_PKG_DIR/hermes_explain.py" 2>/dev/null || true
    WS="$HERMES_PKG_DIR/web_server.py"
    if ! grep -q "hermes_explain" "$WS" 2>/dev/null; then
      python3 -c "
mount_block = '''
# ─── /api/explain — streaming code explanation (brain explain feature) ────────
try:
    from hermes_cli.hermes_explain import build_explain_app as _build_explain_app
    for _r in _build_explain_app().routes:
        app.routes.append(_r)
    _log.info(\"Mounted /api/explain (code-explainer)\")
except Exception as _exc:  # noqa: BLE001
    _log.warning(\"Failed to mount /api/explain: %s\", _exc)

'''
with open('$WS') as f:
    src = f.read()
# Insert immediately before the module-scope mount_spa(app) call. That call
# installs a /{full_path:path} catch-all that would swallow /api/explain if
# the mount came after it. Only patch the first occurrence (count=1).
marker = 'mount_spa(app)'
if marker in src and 'hermes_explain' not in src:
    src = src.replace(marker, mount_block + marker, 1)
    with open('$WS', 'w') as f:
        f.write(src)
    print('[entrypoint] /api/explain mount injected before mount_spa(app)')
else:
    print('[entrypoint] /api/explain mount skipped (marker not found or already present)')
" 2>&1 || true
    fi
  fi
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
