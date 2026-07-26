# Hermes Agent — the dev-loop orchestrator + ZCode executor
# zcode.cjs is downloaded from the pinned GitHub release (not gitignored in the repo).
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git openssh-client curl ripgrep ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y nodejs && rm -rf /var/lib/apt/lists/*

# Download the pinned zcode.cjs from GitHub release (v0.15.2)
RUN curl -fsSL -L -o /opt/zcode.cjs \
    "https://github.com/developers-appdeed/the-neon-prime-brain/releases/download/zcode-v0.15.2/zcode.cjs" \
    && chmod +x /opt/zcode.cjs \
    && ln -s /opt/zcode.cjs /usr/local/bin/zcode

# uv (for uvx) — needed by the redis + grafana stdio MCPs the code-explainer
# agent spawns. Installed per the official standalone installer.
RUN curl -LsSf https://astral.sh/uv/install.sh | sh && \
    ln -s /root/.local/bin/uv /usr/local/bin/uv && \
    ln -s /root/.local/bin/uvx /usr/local/bin/uvx

# docker CLI (no daemon) — the postgres MCPs spawn sibling containers on the
# coolify network via `docker run`. The daemon socket is mounted in
# docker-compose.yml; the CLI just needs to talk to it.
RUN apt-get update && apt-get install -y --no-install-recommends docker.io \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir graphifyy "hermes-agent[all]"

ENV ZCODE_DATA_BASE_DIR=/data/zcode

COPY skills/ /opt/skills/
COPY hermes_explain.py /opt/hermes_explain.py
COPY entrypoint.sh /opt/entrypoint.sh
RUN chmod +x /opt/entrypoint.sh

WORKDIR /root
VOLUME ["/root/.hermes", "/root/.zcode", "/repos"]

EXPOSE 9119

HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=30s \
  CMD hermes gateway status 2>/dev/null | grep -q "running" || exit 1

ENTRYPOINT ["/opt/entrypoint.sh"]
