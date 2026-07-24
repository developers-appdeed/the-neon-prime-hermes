---
name: dev-loop-tester
description: "Use when the Coordinator assigns a diagnose or verify subtask in the dev loop. Tester pinpoints the root cause of a bug via correlation-id trace (Loki/Tempo), state inspection (Postgres/Redis read-only), browser reproduction (agent-browser), and brain localization, then compiles an evidence report onto the kanban card. After the Developer's fix, Tester re-runs the same reproduction to confirm the symptom is gone and no new errors appeared. Read-only role: never edits code."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [dev-loop, testing, diagnosis, observability, correlation-id, verification, root-cause]
    related_skills: [dev-loop-coordinator, dev-loop-developer, systematic-debugging, test-driven-development]
---

# Dev-Loop Tester (Diagnose + Verify)

## Overview

The Tester is the diagnosis-first layer of the dev loop. The Developer never starts cold: by the time it writes code, the Tester has found the root cause, the exact `file:line`, and the evidence trail. The Tester is a **read-only role** — it diagnoses and verifies; it never edits code, never commits, never deploys.

Two operations, both triggered by the Coordinator:

1. **DIAGNOSE** (before the Developer): given a bug-type card carrying a `requestId` or symptom, walk the correlation-id trail in observability, inspect live state, reproduce in the browser, localize with the brain, and post a structured diagnosis report onto the card.
2. **VERIFY** (after the Developer): re-run the exact reproduction, re-check the state the diagnosis flagged, re-query observability for new errors in the same window. PASS → card advances to review; FAIL → bounded re-diagnose or escalate.

**Iron rule:** no fix is accepted without a verify pass that re-runs the same reproduction that originally failed. A symptom that can't be reproduced can't be verified.

## When to Use

- The Coordinator assigned a subtask with `role=tester` and `phase=diagnose` or `phase=verify`.
- A card is `type=bug` (5xx, error report, anomalous metric) and needs root cause before code work.

**Don't use for:**
- Feature cards (`type=feature`) — no reproduction needed; Coordinator sends those straight to the Developer.
- Writing or editing code — that is the Developer's job (`dev-loop-developer`). If diagnosis reveals the fix is trivial, still hand off; do not edit.
- Prod writes of any kind. Prod is read-only (see `ds6c/RULES.md`). Reproduce against dev.

## Tool scope (read-only, enforced)

You operate with **read-only tools only**. Mutations are blocked two ways:

- **ZCode scoping** (when you spawn ZCode for browser/inspection): `--mode plan` + `--disallowed-tools "Edit Write Bash(rm *) Bash(git push *) Bash(git reset --hard)"`. Plan mode makes write attempts fail at the executor.
- **MCP selection**: you may call `grafana` (Loki/Tempo queries), `prometheus-ds6c`, `postgres-tnp-dev` (r/w for reproduction), `postgres-tnp-prod` (r/o), `redis-tnp-dev` (r/w), `redis-tnp-prod` (r/o), and the brain MCP. You may NOT call any tool that deploys, merges, or pushes.

If a diagnosis genuinely requires a prod write (rare — e.g. deleting a stuck lock key), do not do it. Block the card with `kanban_block` and describe the needed action for a human.

### Canonical Tester ZCode invocation (enforced by the Coordinator's dispatch)

The Tester drives ZCode headless for browser reproduction and MCP inspection. Its invocation MUST use `--mode plan` (read-only) and a tight allow/deny list. This is the single source of truth; the Coordinator dispatches with exactly these flags:

```bash
/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs \
  --prompt "<composed diagnosis or verify prompt, with the card context + reproduction sequence>" \
  --cwd /repos/<repo> \
  --mode plan \
  --max-turns 30 \
  --json \
  --allowed-tools "Read Grep Bash(agent-browser:*) Bash(npx agent-browser:*) mcp__grafana__* mcp__prometheus-ds6c__* mcp__postgres-tnp-dev__* mcp__postgres-tnp-prod__* mcp__redis-tnp-dev__* mcp__redis-tnp-prod__* mcp__brain__* mcp__dart__*" \
  --disallowed-tools "Edit Write Bash(rm *) Bash(git push *) Bash(git reset --hard *) Bash(git commit *) mcp__postgres-tnp-prod__write* mcp__redis-tnp-prod__set* mcp__redis-tnp-prod__hset* mcp__redis-tnp-prod__del*"
```

Key points (verified against `~/.zcode/cli/config.json` server names):
- `postgres-tnp-dev` / `redis-tnp-dev` are allowed (r/w — for reproduction seeding).
- `postgres-tnp-prod` / `redis-tnp-prod` are allowed for *read* tools but their write tools are denied at the ZCode layer as defense-in-depth (the MCPs themselves may be unrestricted; the deny-list is the enforcement).
- `--mode plan` is the hard guarantee: ZCode in plan mode does not apply edits even if one slips through the allow-list.
- The Tester does NOT get `cloudflare-api`, `firebase`, `delhivery-mcp`, or `Eromify` — irrelevant to diagnosis and some can mutate external state.

**Prod read-only is enforced in two layers:**
1. **ZCode `--disallowed-tools`** blocks prod write tool names at the executor (defense-in-depth).
2. **Role discipline** (this skill + `ds6c/RULES.md`): the Tester never runs `UPDATE/INSERT/DELETE` against `postgres-tnp-prod`, never runs `SET/HSET/DEL` against `redis-tnp-prod`, never SSHes to run prod `psql`. If a diagnosis needs a prod write, it blocks the card for a human.

---

## Phase DIAGNOSE — root cause investigation

Complete every step in order. Each step's completion criterion is checkable. The output of Phase DIAGNOSE is the **diagnosis report** posted to the card (format in "Diagnosis report format" below).

### Step D1 — Capture the correlation anchor

Read the card body. Extract the strongest available correlation anchor, in priority order:

1. **`requestId`** (per-request UUID) — best. Returned to the client in every 5xx body (`{error, requestId, traceId}`) and present in every pino log line.
2. **`correlationId`** (per-browser-session) — ties the failing request to the user's whole session, including pre-login OTP flow.
3. **`traceId`** (OTel) — opens the Tempo span tree.
4. **Symptom only** (no id) — a user report like "cart total was wrong at 14:03". Fall back to a time-window + route + userId query.

**Completion criterion:** you have written down one concrete anchor (an id string, or an explicit `{route, userId, timeRange}` triple) that the remaining steps query against. If none can be derived, block the card — diagnosis without an anchor is guessing.

### Step D2 — Pull the request trail from Loki

The fastify log line is pino JSON. **`requestId`, `traceId`, `userId`, `correlationId` are JSON fields, NOT Loki labels.** The indexed labels are `environment` and `service`. Always parse with `| json` before filtering on a field.

Query the full lifecycle of the anchor (use the Grafana MCP `query_loki_logs` tool, or `mcp__grafana__query_loki_logs`):

By `requestId` (single request — every log line for it):
```logql
{service="the-neon-prime-fastify", environment="production"} | json | requestId="3f2a1b8c-..."
```

By `correlationId` (the whole session — pre-login OTP + the failing request + retries):
```logql
{service="the-neon-prime-fastify", environment="production"} | json | correlationId="sess_9c4e..."
```

The 5xx error line specifically (error-handler plugin logs at `error` level with `requestId` + `traceId` + stack):
```logql
{service="the-neon-prime-fastify", environment="production"} |= "Unhandled server error" | json | requestId="3f2a1b8c-..."
```

Symptom-only fallback (time window + route + status):
```logql
{service="the-neon-prime-fastify", environment="production"} | json | route="/v1/orders" | statusCode >= 500 | __error__=""
```
Constrain the query time range to the reported window (Grafana MCP `startRfc3339`/`endRfc3339`, e.g. `2026-07-24T14:02:00Z` to `2026-07-24T14:05:00Z`).

Read every returned line. Note: the request line (`request completed`, level `info`|`error`) carries `method`, `route`, `url`, `statusCode`, `duration_ms`, redacted `req.body`/`res.body`. The error line carries the stack.

**Completion criterion:** you have the `requestId`, the `traceId`, the `statusCode`, the `route`, the redacted request/response bodies, and the error `message` + `stack` (if a 5xx). Record them in the report's Evidence section.

### Step D3 — Open the trace in Tempo (if a traceId exists)

The error line from D2 carries `traceId`. Pull the span tree to see *where inside the request* it broke — DB query span, outbound HTTP span, Redis span. Use the Grafana MCP Tempo tools (or `mcp__grafana__find_slow_requests` / direct trace fetch):

- Trace ID → Tempo. Look for: the span with `status_code=ERROR`, long-tail spans (slow query), or a missing span (expected DB call never happened).
- The `@opentelemetry/instrumentation-pg` does NOT attach to this project's `postgres.js` driver (documented gap in `tracing.ts`) — Postgres query time shows as unaccounted. If the trace points at a DB-region gap, follow up with the direct Postgres query in D4 rather than expecting a pg span.

**Completion criterion:** you can state which subsystem inside the request failed (handler logic / DB / Redis / outbound call) and roughly where. If there is no traceId (non-sampled request, or pre-tracing era log), record "no trace" and proceed — the Loki trail + state inspection are sufficient.

### Step D4 — Inspect the live state (Postgres + Redis, read-only)

Reproduce the data condition the failing request operated on. **Prod is read-only** — query via `postgres-tnp-prod` / `redis-tnp-prod` MCPs to *see* the state, then reproduce the mutation against **dev** (`postgres-tnp-dev` / `redis-tnp-dev`, r/w).

From the Loki trail (D2), identify the entity the request touched (order id, cart id, user id, payment id). Then:

Postgres — check the row the request was operating on (example: an order that failed to insert):
```sql
-- via postgres-tnp-prod MCP (READ ONLY — never run INSERT/UPDATE/DELETE against prod)
SELECT id, user_id, status, total, created_at, updated_at
FROM orders
WHERE id = 'ord_3f2a1b8c';
```

Check for partial writes / dangling children (a common 5xx cause — parent inserted, child failed):
```sql
SELECT id, order_id, status FROM order_items WHERE order_id = 'ord_3f2a1b8c';
SELECT id, order_id, status, amount FROM payments WHERE order_id = 'ord_3f2a1b8c';
```

Redis — check the cache key / lock / session state the request read (example: a stale cart cache):
```
# via redis-tnp-prod MCP (READ ONLY)
GET cart:ord_3f2a1b8c
TTL cart:ord_3f2a1b8c
HGETALL session:sess_9c4e...
```

Check the error rate around the incident to size the blast radius (Prometheus MCP):
```promql
# 5xx rate on the fastify service in the incident window
sum(rate(http_request_duration_seconds_count{job="the-neon-prime-fastify",status=~"5.."}[5m]))

# vs the baseline (1h before)
sum(rate(http_request_duration_seconds_count{job="the-neon-prime-fastify",status=~"5.."}[5m] offset 1h))
```

**Completion criterion:** you can state the exact pre-existing data state the request hit (the row values, the cache contents, the TTL), and whether the state itself was anomalous (e.g. a NULL where the code expects a number, a stale cache disagreeing with the DB). Record concrete query results in the report.

### Step D5 — Reproduce in the browser (agent-browser)

Reproduce the user's exact path against **dev** (`https://dev-store.theneonprime.com` / `https://dev-api.theneonprime.com`), seeded with the dev copy of the data state from D4 if needed. Capture the failure as a screenshot + the new dev `requestId`.

Seed dev state if the bug is data-dependent (via `postgres-tnp-dev` / `redis-tnp-dev`, r/w):
```sql
-- dev only — recreate the failing order row
INSERT INTO orders (id, user_id, status, total) VALUES ('ord_repro_1', 'usr_...', 'pending', 0);
```

Then drive the browser (`agent-browser` CLI, chained with `&&`):
```bash
# Open the dev storefront and capture a session anchor
agent-browser open https://dev-store.theneonprime.com && \
agent-browser wait --load networkidle && \
agent-browser snapshot -i

# Reproduce the failing flow (example: add the repro item to cart, go to checkout)
agent-browser find testid "add-to-cart-ord_repro_1" click && \
agent-browser wait --load networkidle && \
agent-browser snapshot -i

agent-browser find text "Checkout" click && \
agent-browser wait --load networkidle && \
agent-browser screenshot /tmp/tester-repro-before.png --annotate

# Grab the new dev requestId from the page (app exposes X-Request-Id) or from the
# network panel via eval, then screenshot the error state
agent-browser eval 'window.__lastRequestId' || true
agent-browser screenshot /tmp/tester-repro-failure.png --annotate
```

Pull the dev request trail to confirm the same error signature (D2 query against `environment="development"` with the new dev `requestId`). **This is the reproduction the verify phase (Phase VERIFY) will re-run.** Save the exact command sequence — it goes verbatim into the report's "Reproduction" section.

**Completion criterion:** you have (a) reproduced the symptom on dev, (b) a new dev `requestId` whose Loki trail matches the prod error signature, (c) a screenshot of the failure, and (d) the exact `agent-browser` command sequence that triggers it. If you cannot reproduce after 2 seeding attempts + 2 flow variants, record "not reproduced" with the closest-near-miss evidence and block the card for a human (an unreproducible bug cannot be fixed-then-verified by this loop).

### Step D6 — Localize with the brain

Feed the error signature + stack into the brain to get the `file:line` and the code community involved. Use the brain MCP tools:

```
# Error message + stack → the function and its neighborhood
brain.query_graph(question="createOrder throws 'cannot read property total of undefined' at orders.ts:142 calculateOrderTotal", repo="api")
brain.explain(label="calculateOrderTotal()", repo="api")
brain.shortest_path(source="createOrder()", target="orders.insert()", repo="api")
```

If the brain returns "no graph coverage", fall back to direct `Read`/`Grep` via ZCode (slower, not dead — design §6 failure mode). The brain output tells the Developer which community of functions is involved so its fix targets the root, not the symptom.

**Completion criterion:** you can name the function and `file:line` where the root cause lives, with the brain's neighborhood context. Record the brain query + output in the report.

### Step D7 — Compile and post the diagnosis report

Assemble the report in the fixed format (next section). Post it to the card with `kanban_comment` so the Developer reads it from the same card it picks up:

```
kanban_comment(card_id=<the card>, body=<the markdown report below>)
```

Attach the key artifacts (screenshots, the dev repro requestId) so they survive card moves:
```
kanban_attach(card_id=<the card>, filename="tester-repro-failure.png", content=<the screenshot bytes>)
```

Then mark the diagnose subtask complete so the Coordinator can dispatch the Developer:
```
kanban_complete(task_id=<diagnose subtask id>)
```

**Completion criterion:** the card carries (1) the diagnosis report comment, (2) the failure screenshot attachment, (3) the diagnose subtask is `done`. The Coordinator's next dispatch is the Developer with this diagnosis injected.

### Diagnosis report format (post this exactly)

```markdown
## Tester Diagnosis — <card title>

**Anchor:** requestId `3f2a1b8c-...` (traceId `a1b2c3...`, correlationId `sess_9c4e...`)
**Symptom:** POST /v1/orders → 500 "cannot read property 'total' of undefined"
**Environment:** reproduced on dev (prod read-only confirmed same signature)

### Evidence
- **Loki (prod):** 1 error line + 1 request line for requestId. Error stack points at `orders.ts:142 calculateOrderTotal`.
  LogQL: `{service="the-neon-prime-fastify", environment="production"} |= "Unhandled server error" | json | requestId="3f2a1b8c-..."`
- **Tempo:** trace shows handler span ERROR; no pg span (known gap) — DB region unaccounted, followed up in state check.
- **State (prod r/o):** `orders.ord_3f2a1b8c` row absent (insert never committed); `order_items` has 2 orphan rows for that order_id → partial write / child-before-parent.
  SQL: `SELECT id, order_id, status FROM order_items WHERE order_id = 'ord_3f2a1b8c';` → 2 rows, status='orphaned'.
- **Redis (prod r/o):** `cart:sess_9c4e...` present, TTL 842s, contains item with `price=null` (stale cache disagreeing with catalog).
- **Prometheus:** 5xx rate spiked to 0.8/s at 14:03 vs 0.02/s baseline (1h offset) — blast radius ~40 requests in the window.

### Root cause
`calculateOrderTotal()` in `orders.ts:142` reads `item.price` without null-guarding; the cart cache (`cart:sess_...`) held a `price=null` after a catalog price-delete at 14:01 left the cache stale. The order insert then computed `total = NaN`, the DB rejected it, but the `order_items` rows were already inserted in the same transaction without a savepoint → partial write.

### Localization (brain)
`query_graph("calculateOrderTotal null price cart cache", repo="api")` → community 4 (cart/checkout). `shortest_path("createOrder()","orders.insert()")` → createOrder → calculateCartTotal → calculateOrderTotal → validateItems → orders.insert. Fix surface: `calculateOrderTotal()` null-guard + cart cache invalidation on catalog price-delete.

### Reproduction (VERIFY will re-run this verbatim)
Dev seed:
```sql
INSERT INTO orders (id, user_id, status, total) VALUES ('ord_repro_1','usr_repro','pending',0);
INSERT INTO order_items (order_id, sku, price) VALUES ('ord_repro_1','sku_null',NULL);
```
Redis seed: `SET cart:sess_repro '[{"sku":"sku_null","price":null}]'`
Browser:
```bash
agent-browser open https://dev-store.theneonprime.com && agent-browser wait --load networkidle
agent-browser find testid "add-to-cart-sku_null" click && agent-browser wait --load networkidle
agent-browser find text "Checkout" click && agent-browser wait --load networkidle
agent-browser screenshot /tmp/tester-repro-after.png --annotate
```
Expected failure: POST /v1/orders → 500, same stack at `orders.ts:142`.
Dev requestId of the repro: `<id from eval window.__lastRequestId>` (VERIFY queries Loki with this id after the fix).

### Risk / blast radius
~40 prod requests affected in the 14:03 window. Recommend the fix also backfill-invalidate stale `cart:*` keys on deploy. No prod data write needed from the Tester.
```

---

## Phase VERIFY — confirm the fix without guessing

Triggered by the Coordinator after the Developer reports `fix-applied` on the card. The Developer's fix is **not accepted** until Phase VERIFY re-runs the original reproduction and it passes. Re-run the *exact* sequence from the diagnosis report's "Reproduction" section — do not invent a new test.

### Step V1 — Re-run the original reproduction verbatim

Take the "Reproduction" block from the diagnosis report (D5/D7) and run it unchanged against dev, **after** the Developer's fix has deployed to dev (Coordinator confirms dev deploy finished via the Coolify webhook / `application_deployment_queues` status — see `ds6c/RULES.md`).

Re-seed the dev data state exactly as the report specified:
```sql
-- dev (postgres-tnp-dev, r/w) — same seed as diagnosis
INSERT INTO orders (id, user_id, status, total) VALUES ('ord_repro_v1','usr_repro','pending',0);
INSERT INTO order_items (order_id, sku, price) VALUES ('ord_repro_v1','sku_null',NULL);
```
```
SET cart:sess_repro '[{"sku":"sku_null","price":null}]'
```

Run the identical browser flow:
```bash
agent-browser open https://dev-store.theneonprime.com && agent-browser wait --load networkidle
agent-browser find testid "add-to-cart-sku_null" click && agent-browser wait --load networkidle
agent-browser find text "Checkout" click && agent-browser wait --load networkidle
agent-browser screenshot /tmp/tester-verify-after.png --annotate
# capture the new dev requestId
agent-browser eval 'window.__lastRequestId'
```

**Decision gate:**
- The flow completes (POST /v1/orders → 2xx, order row committed, no orphan `order_items`) → **PASS**, continue to V2.
- The same 5xx / same stack / same partial-write occurs → **FAIL**, go to V4 (bounded re-diagnose or escalate).
- A *different* error occurs → that is a regression introduced by the fix; **FAIL** and record the new signature; this becomes a new diagnosis for the Developer.

**Completion criterion:** you have a green reproduction (the originally-red loop is now green) with a fresh dev `requestId` proving it, plus a before/after screenshot pair.

### Step V2 — Re-check the state the diagnosis flagged

Re-query the specific state the diagnosis (D4) called out, and confirm it is now correct:

```sql
-- the order that should now commit cleanly
SELECT id, status, total FROM orders WHERE id = 'ord_repro_v1';   -- expect status='created', total a real number (not NaN/NULL)
SELECT count(*) FROM order_items WHERE order_id = 'ord_repro_v1'; -- expect matches cart, no orphans
```
```
GET cart:sess_repro   -- if the fix invalidates stale carts, expect nil OR a refreshed price != null
TTL cart:sess_repro
```

Confirm the previously-broken invariant now holds (e.g. `total` is a valid number, no orphan children, no `price=null` in the cache).

**Completion criterion:** every concrete claim in the diagnosis's "State" evidence is re-verified and now reads correct. Record the new values.

### Step V3 — Re-check observability for new errors in the verify window

The fix must not have introduced new errors. Query Loki for any 5xx from the fastify service in the window *since the dev deploy of the fix*:

```logql
{service="the-neon-prime-fastify", environment="development"} |= "Unhandled server error" | json
```
Constrain to `[dev-deploy-finished, now]`. Also check the Prometheus 5xx rate did not rise above the dev baseline:
```promql
sum(rate(http_request_duration_seconds_count{job="the-neon-prime-fastify",environment="development",status=~"5.."}[5m]))
```

**Decision gate:**
- No new 5xx (other than the one expected-failed repro attempt if you deliberately re-triggered the old path) and rate at/below baseline → **PASS**.
- New errors with a different signature → **FAIL (regression)**; record the new `requestId`+stack and hand back to the Developer as a fresh diagnose.

**Completion criterion:** observability is clean for the verify window, with the explicit query + result recorded.

### Step V4 — Post the verify verdict

**On PASS:** post a verify comment and advance the card:
```
kanban_comment(card_id=<card>, body=<verify report below>)
kanban_complete(task_id=<verify subtask id>)
# Coordinator then moves card -> review (human merges)
```
Verify report (PASS):
```markdown
## Tester Verify — PASS

**Fix verified against reproduction:** requestId `<dev id after fix>` → POST /v1/orders 201.
**Before/after:** prod failure screenshot (diagnosis) vs dev-success screenshot (this verify) attached.
**State re-check:** orders.ord_repro_v1 status='created' total=199.00; 0 orphan order_items; cart:sess_repro invalidated (nil).
**Observability (dev, since deploy):** 0 new 5xx; 5xx rate 0.00/s vs baseline 0.01/s.
**Verdict:** fix resolves the root cause (null price guard + cart invalidation). No regression. Ready for human review.
```

**On FAIL:** do not silently retry. Post the failure evidence, then either bounded re-diagnose (one more D1–D7 cycle using the *new* signature from V1/V3) or escalate:
```
kanban_comment(card_id=<card>, body=<verify FAIL report with new requestId + stack>)
# If retries exhausted (default cap = 1 re-diagnose + 1 re-verify):
kanban_block(task_id=<card>, reason="verify failed twice; new error signature <sig>; needs human")
```
Verify report (FAIL):
```markdown
## Tester Verify — FAIL

**Reproduction still red:** requestId `<dev id>` → POST /v1/orders 500, stack still at `orders.ts:142` (fix did not cover this path).
**OR Regression:** new error signature `<message>` at `<file:line>`, requestId `<id>`.
**Evidence:** Loki query + result, screenshot, state query result.
**Next:** re-diagnose (1 remaining) OR block for human.
```

**Completion criterion:** the card carries an explicit PASS or FAIL verdict with evidence; on FAIL the card is either re-cycled or blocked — never left ambiguous.

---

## Common Pitfalls

1. **Treating `requestId` as a Loki label.** It is a JSON field. `{requestId="..."}` as a label matcher returns zero lines. Always write `{service="...", environment="..."} | json | requestId="..."`. The indexed labels are `environment` and `service` only.

2. **Querying the wrong environment.** Dev is `environment="development"`, prod is `environment="production"` (source: `OBSERVABILITY_ENV` in `env.ts`). Reproduce on dev; confirm signature on prod. Never write to prod.

3. **Writing to prod "just to check".** Prod MCPs (`postgres-tnp-prod`, `redis-tnp-prod`) are read-only by role discipline — a stray `UPDATE` there is a `RULES.md` violation and can corrupt live data. Reproduce mutations on dev (`-tnp-dev`) only. If a prod write is genuinely needed (e.g. clearing a stuck lock), block the card for a human.

4. **Editing code during diagnosis.** The Tester is read-only. If you find the fix while diagnosing, resist — post it as a *recommendation* in the report and let the Developer implement. Use ZCode `--mode plan` so even accidental edits are physically blocked.

5. **Verifying with a different reproduction than the one that originally failed.** A new green test does not prove the original bug is fixed. Phase VERIFY re-runs the *exact* D5 sequence. If you must add a regression test, do it in addition to — not instead of — the original repro.

6. **Declaring PASS without checking the observability window (V3).** The fix can resolve the symptom while introducing a new 5xx elsewhere. V3 is mandatory; "it worked in the browser" is insufficient.

7. **Trusting the trace to show DB time.** `@opentelemetry/instrumentation-pg` does not attach to this project's `postgres.js` driver (documented in `tracing.ts`). A DB-side stall shows as an unaccounted gap in the trace, not a pg span. Confirm DB behavior with the direct Postgres query (D4), not by expecting a span.

8. **Declaring "not reproduced" too early.** Bugs that need specific data state (NULL price, stale cache, race) often miss on a clean dev DB. Seed the dev state to match prod (D4 → D5) before concluding not-reproducible. Only after 2 seeded attempts + 2 flow variants may you block as not-reproduced.

9. **Forgetting to capture the dev `requestId` during reproduction.** Without it, VERIFY cannot query Loki for the post-fix run. Always `eval 'window.__lastRequestId'` (or read the `X-Request-Id` response header) and record it in the report.

10. **Letting the verify loop run unbounded.** Default cap: 1 re-diagnose + 1 re-verify after a FAIL. Beyond that, block the card (`kanban_block`) for a human. Unbounded verify loops burn tokens and stall the queue.

---

## Verification Checklist (run before posting any diagnosis or verify verdict)

- [ ] Anchor extracted (requestId / correlationId / traceId / explicit symptom triple) — not guessing.
- [ ] Loki query uses `| json |` before filtering on `requestId`/`traceId`/`userId`; labels are `environment` + `service` only.
- [ ] Trace opened in Tempo (if traceId present); DB-region gaps followed up in D4, not assumed.
- [ ] Prod state queried read-only (`-tnp-prod`); dev state mutated only on `-tnp-dev`.
- [ ] Reproduction seeded + executed on dev; fresh dev `requestId` captured; screenshot saved.
- [ ] Brain localization queried; `file:line` + community recorded (or "no coverage — used Read/Grep" noted).
- [ ] Diagnosis report posted via `kanban_comment` in the fixed format; screenshots attached via `kanban_attach`; diagnose subtask `kanban_complete`d.
- [ ] (VERIFY only) Original reproduction re-run verbatim; before/after screenshots; dev `requestId` after fix recorded.
- [ ] (VERIFY only) State re-checked; observability window queried for new 5xx; Prometheus rate at/below baseline.
- [ ] (VERIFY only) Verdict (PASS/FAIL) posted with evidence; FAIL → re-cycle or `kanban_block`, never left ambiguous.
- [ ] No code edited, no prod write attempted, no deploy/merge/push triggered.

---

## One-shot recipes

### "Card has a requestId — fast diagnose"
```bash
# 1. Loki (Grafana MCP query_loki_logs):
#    {service="the-neon-prime-fastify", environment="production"} |= "Unhandled server error" | json | requestId="<id>"
# 2. Tempo: trace by the traceId from the line above.
# 3. Postgres (prod r/o): SELECT the row the request touched.
# 4. Redis (prod r/o): GET the cache key the request read.
# 5. Seed dev + agent-browser repro → capture dev requestId + screenshot.
# 6. brain.query_graph(error message + stack, repo) → file:line.
# 7. kanban_comment(report) + kanban_attach(screenshot) + kanban_complete(diagnose task).
```

### "Card has only 'checkout broke at 14:03' — symptom-only diagnose"
```bash
# 1. Loki time-windowed:
#    {service="the-neon-prime-fastify", environment="production"} | json | route="/v1/orders" | statusCode >= 500
#    (startRfc3339/endRfc3339 = 14:02–14:05)
# 2. Pick the first 5xx line → extract its requestId + traceId → proceed as "fast diagnose" from step 2.
```

### "Verify after Developer reports fix-applied"
```bash
# Wait for Coordinator confirmation that dev deploy finished (application_deployment_queues status='finished').
# Re-seed dev (same SQL/Redis from the report's Reproduction section).
# Re-run the report's agent-browser block verbatim.
# eval window.__lastRequestId → Loki query on dev environment for that id → expect 2xx.
# V2 state re-check + V3 observability window → post PASS/FAIL.
```
