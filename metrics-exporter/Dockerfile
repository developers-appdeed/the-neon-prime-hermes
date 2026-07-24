# Hermes metrics exporter sidecar.
# Python stdlib only (sqlite3, http.server) — no pip install, tiny image.
# Reads the hermes kanban DB over a read-only volume mount and never writes.
FROM python:3.12-alpine
COPY exporter.py /exporter.py
# Runs as root by default. The hermes home host directory is `0700 root:root`
# (created by Coolify's deploy of the hermes app), so a non-root sidecar user
# cannot traverse into it to read kanban.db. The bind mount is `:ro` so the
# sidecar provably cannot mutate hermes state regardless of the in-container uid.
EXPOSE 9091
CMD ["python3", "/exporter.py"]
