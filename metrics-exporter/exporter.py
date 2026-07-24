"""Hermes metrics exporter sidecar.

A read-only Prometheus exporter that does NOT touch the running `hermes gateway`
process. It inspects the kanban SQLite DB (`$HERMES_HOME/kanban.db`) over a
read-only mount and serves Prometheus text format on :9091/metrics.

The kanban schema (verified on the live server 2026-07-24) is the `tasks` table
inside `kanban.db`, with columns: status, priority, assignee, consecutive_failures,
block_kind, block_recurrences. (The plan body's `kanban_cards` table name was a
placeholder; reality is `tasks`.)

Env:
  HERMES_HOME            default /root/.hermes  — the ~/.hermes volume (read-only mount)
  HERMES_KANBAN_DB       default $HERMES_HOME/kanban.db
  HERMES_MAX_AGENTS      default 7              — the documented agent cap (HERMES_MAX_AGENTS)
  LISTEN_ADDR            default 0.0.0.0:9091

Metrics (see README.md for the full table):
  hermes_up                          gauge   — DB is openable (1) or not (0)
  hermes_cards_total{status,priority} gauge — COUNT(*) FROM tasks GROUP BY status, priority
  hermes_agents_active               gauge   — tasks WHERE status='progress'
  hermes_agent_cap                   gauge   — HERMES_MAX_AGENTS env
  hermes_blocked_cards               gauge   — tasks WHERE status='blocked'
  hermes_tasks_completed             gauge   — tasks WHERE status='done' (cumulative; use increase() in queries)
  hermes_debug_loop_retries          histogram — observes consecutive_failures per task
"""
from __future__ import annotations

import math
import os
import sqlite3
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

HERMES_HOME = os.environ.get("HERMES_HOME", "/root/.hermes")
KANBAN_DB = os.environ.get("HERMES_KANBAN_DB", os.path.join(HERMES_HOME, "kanban.db"))
MAX_AGENTS = int(os.environ.get("HERMES_MAX_AGENTS", "7"))
LISTEN = os.environ.get("LISTEN_ADDR", "0.0.0.0:9091")

# Histogram buckets for consecutive_failures: 0 (no retries) up through a cap.
# A task with consecutive_failures >= 5 is in the +Inf bucket — that's a stuck
# loop a human should look at (matches the Hermes DEFAULT_FAILURE_LIMIT pattern).
RETRY_BUCKETS = (0.0, 1.0, 2.0, 3.0, 5.0, math.inf)


def _open_ro():
    """Open the kanban DB read-only via URI. Returns a connection or None.

    `mode=ro` guarantees the exporter can never write — important because a
    stray write from a sidecar into the gateway's live WAL would corrupt the
    kanban. `immutable=1` tells SQLite the file will not be modified by any
    other process during our connection, so it MUST NOT create or read the
    `-wal`/`-shm` sidecar files. This is required because the sidecar binds
    the hermes home directory `:ro` — without `immutable=1`, SQLite in WAL
    mode tries (and fails) to create `-shm` next to the DB, raising
    'unable to open database file' even though the DB itself is readable.

    Trade-off: a kanban write that is in-flight (uncheckpointed in the WAL)
    when we open the connection will not be visible to us until SQLite's next
    checkpoint. For Prometheus scrape cadence (15s) + kanban write cadence
    (human/agent-paced) this is acceptable. If you need strict consistency,
    drop immutable=1 and bind the volume read-write instead.
    """
    if not os.path.exists(KANBAN_DB):
        return None
    try:
        return sqlite3.connect(
            f"file:{KANBAN_DB}?mode=ro&immutable=1", uri=True, timeout=2
        )
    except sqlite3.Error:
        return None


def _query(con, sql, args=()):
    """Run a query; return rows or () on any sqlite error (degraded = zero)."""
    try:
        return con.execute(sql, args).fetchall()
    except sqlite3.Error:
        return ()


def _histogram_lines(name, help_text, observations):
    """Emit a Prometheus histogram from a list of float observations.

    Cumulative buckets + _count + _sum. _sum is the sum of observations
    (consecutive_failures totals) so it's meaningful even with no buckets set.
    """
    lines = [f"# HELP {name} {help_text}", f"# TYPE {name} histogram"]
    n = len(observations)
    total = sum(observations)
    cum = 0
    obs_sorted = sorted(observations)
    idx = 0
    for bound in RETRY_BUCKETS:
        while idx < n and obs_sorted[idx] <= bound:
            cum += 1
            idx += 1
        bound_label = "+Inf" if bound == math.inf else str(int(bound)) if bound == int(bound) else str(bound)
        lines.append(f'{name}_bucket{{le="{bound_label}"}} {cum}')
    lines.append(f"{name}_count {n}")
    lines.append(f"{name}_sum {total}")
    return lines


def render() -> str:
    con = _open_ro()
    up = 1 if con is not None else 0

    lines: list[str] = []

    lines.append("# HELP hermes_up 1 if the kanban DB is openable (read-only), else 0.")
    lines.append("# TYPE hermes_up gauge")
    lines.append(f"hermes_up {up}")

    # hermes_cards_total{status, priority}
    lines.append("# HELP hermes_cards_total Number of kanban tasks by status and priority.")
    lines.append("# TYPE hermes_cards_total gauge")
    if con is not None:
        rows = _query(
            con,
            "SELECT status, priority, COUNT(*) FROM tasks GROUP BY status, priority",
        )
        if rows:
            for status, priority, n in sorted(rows):
                lines.append(
                    f'hermes_cards_total{{status="{status}",priority="{priority}"}} {n}'
                )
        else:
            lines.append('hermes_cards_total{status="none",priority="0"} 0')
    else:
        lines.append('hermes_cards_total{status="none",priority="0"} 0')

    # hermes_agents_active — tasks currently in 'progress' = active worker agents
    active = 0
    completed = 0
    blocked = 0
    retries: list[float] = []
    if con is not None:
        row = _query(con, "SELECT COUNT(*) FROM tasks WHERE status='progress'")
        if row:
            active = row[0][0]
        row = _query(con, "SELECT COUNT(*) FROM tasks WHERE status='done'")
        if row:
            completed = row[0][0]
        row = _query(con, "SELECT COUNT(*) FROM tasks WHERE status='blocked'")
        if row:
            blocked = row[0][0]
        retries = [float(r[0]) for r in _query(
            con, "SELECT consecutive_failures FROM tasks WHERE consecutive_failures > 0"
        )]

    lines.append("# HELP hermes_agents_active Tasks currently in 'progress' (active worker agents).")
    lines.append("# TYPE hermes_agents_active gauge")
    lines.append(f"hermes_agents_active {active}")

    lines.append("# HELP hermes_agent_cap Documented max concurrent agents (HERMES_MAX_AGENTS).")
    lines.append("# TYPE hermes_agent_cap gauge")
    lines.append(f"hermes_agent_cap {MAX_AGENTS}")

    lines.append("# HELP hermes_blocked_cards Tasks in 'blocked' status (escalation backlog).")
    lines.append("# TYPE hermes_blocked_cards gauge")
    lines.append(f"hermes_blocked_cards {blocked}")

    # hermes_tasks_completed — cumulative gauge of completed tasks. Exposed as a
    # gauge because the sidecar re-reads the DB each scrape (no in-process state),
    # but the value is monotonic in normal operation (tasks don't un-complete),
    # so Prometheus increase()/rate() treats it correctly. Documented as a counter
    # equivalent in the README; metric name uses the gauge convention to stay
    # honest about its collection model.
    lines.append("# HELP hermes_tasks_completed Cumulative completed tasks (status='done'). Use increase() in queries.")
    lines.append("# TYPE hermes_tasks_completed gauge")
    lines.append(f"hermes_tasks_completed {completed}")

    # hermes_debug_loop_retries — histogram of consecutive_failures across all tasks
    lines.extend(_histogram_lines(
        "hermes_debug_loop_retries",
        "Distribution of consecutive_failures per task (debug-loop retry signal).",
        retries,
    ))

    if con is not None:
        con.close()

    return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/metrics":
            body = render().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path in ("/healthz", "/health"):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):  # silence default stderr spam
        pass


def main():
    host, _, port = LISTEN.rpartition(":")
    print(
        f"[hermes-metrics-exporter] listening on {host or '0.0.0.0'}:{port or 9091} "
        f"(db={KANBAN_DB}, cap={MAX_AGENTS})",
        file=sys.stderr,
    )
    HTTPServer((host or "0.0.0.0", int(port or 9091)), Handler).serve_forever()


if __name__ == "__main__":
    main()
