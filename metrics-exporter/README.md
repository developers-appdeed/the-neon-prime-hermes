# Hermes metrics exporter (sidecar)

A read-only Prometheus exporter that does **not** touch the running
`hermes gateway` process. It inspects the kanban SQLite DB
(`$HERMES_HOME/kanban.db`, table `tasks`) and serves Prometheus text format on
`:9091/metrics`.

## Why a sidecar

Hermes v0.18.2 has no built-in `/metrics` endpoint and we deliberately do not
rebuild the gateway image. The sidecar reads the kanban DB in
[`mode=ro`](https://www.sqlite.org/uri.html) (SQLite read-only URI) — it can
never write, so the gateway's live WAL is never at risk. The DB schema was
verified on the live server on 2026-07-24: the kanban file is `kanban.db` and
the cards table is `tasks` (columns `status`, `priority`, `assignee`,
`consecutive_failures`).

## Metrics exposed

| Metric | Type | Labels | Source |
|---|---|---|---|
| `hermes_up` | gauge | — | `1` if `kanban.db` is openable read-only, else `0` |
| `hermes_cards_total` | gauge | `status`, `priority` | `SELECT status, priority, COUNT(*) FROM tasks GROUP BY status, priority` |
| `hermes_agents_active` | gauge | — | `SELECT COUNT(*) FROM tasks WHERE status='progress'` |
| `hermes_agent_cap` | gauge | — | `HERMES_MAX_AGENTS` env (default `7`) |
| `hermes_blocked_cards` | gauge | — | `WHERE status='blocked'` |
| `hermes_tasks_completed` | gauge | — | `WHERE status='done'` (cumulative; use `increase()` in queries) |
| `hermes_debug_loop_retries` | histogram | — | observes `consecutive_failures` per task |

Notes on collection model:

- `hermes_tasks_completed` is exposed as a **gauge** because the sidecar re-reads
  the DB on each scrape (no in-process counter state). The value is monotonic in
  normal operation (tasks don't un-complete), so Prometheus `increase()` /
  `rate()` treat it correctly. Treat it as a counter in queries.
- `hermes_debug_loop_retries` is a histogram rebuilt each scrape from the current
  `consecutive_failures` column. It reflects the *current* distribution of retry
  counts across tasks, not a cumulative count of retry events. Buckets:
  `le=0,1,2,3,5,+Inf`.

## Deploy

The sidecar is deployed on the server at `/opt/hermes-metrics/` and joins the
`coolify` network so Prometheus scrapes it as
`ds6c-hermes-metrics-exporter:9091`.

```bash
# On the server, after the hermes app is running:
scp -i ssh_key -r infra/hermes/metrics-exporter root@45.195.159.80:/opt/hermes-metrics/
ssh -i ssh_key root@45.195.159.80 \
  'cd /opt/hermes-metrics && HERMES_MAX_AGENTS=7 docker compose up -d --build'
curl -s http://localhost:9091/metrics | head
```

### Volume wiring

The hermes app's `~/.hermes` is a host bind mount at `/opt/hermes/data/hermes`
(verified via `docker inspect hermes`). The sidecar re-binds that same host path
read-only (`:/root/.hermes:ro`) so both containers see the same `kanban.db`. If
hermes is ever re-deployed with a different home path, update the source side of
the bind in `docker-compose.yml`.

## Scrape job (Prometheus)

```yaml
- job_name: "hermes"
  metrics_path: /metrics
  static_configs:
    - targets: ["ds6c-hermes-metrics-exporter:9091"]
      labels:
        service: hermes
        environment: production
```

## Local smoke test

```bash
docker build -t hermes-metrics-exporter:test .
python3 -c "
import sqlite3
c = sqlite3.connect('/tmp/fake-kanban.db')
c.execute('CREATE TABLE tasks (id TEXT, status TEXT, priority INTEGER, assignee TEXT, consecutive_failures INTEGER)')
c.executemany('INSERT INTO tasks VALUES (?,?,?,?,?)', [
  ('1','progress',3,'developer',0),
  ('2','progress',3,'developer',0),
  ('3','ready',2,'tester',0),
  ('4','blocked',3,'developer',2),
  ('5','done',2,'developer',1),
])
c.commit(); c.close()
"
docker run --rm -p 9092:9091 -v /tmp/fake-kanban.db:/root/.hermes/kanban.db:ro \
  -e HERMES_HOME=/root/.hermes hermes-metrics-exporter:test &
sleep 1
curl -s http://localhost:9092/metrics
```
