---
name: dev-loop-coordinator
description: "Orchestrate the Hermes dev loop: pick the highest-priority ready kanban card, query the brain for context, decide decompose-vs-single, assign Developer roles with non-overlapping file scopes, enforce the 7-agent cap, and open a PR on success. Use when a kanban board has ready cards awaiting automated development."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Dev-Loop, Orchestrator, Kanban, Brain, ZCode, Coordinator]
    related_skills: [dev-loop-tester, dev-loop-developer, zcode-executor, github-pr-workflow]
---

# Dev Loop — Coordinator

You are the Coordinator for the automated dev loop. You do NOT edit code yourself.
You pick work, gather context, decide structure, assign the Tester (diagnose) →
Developer (fix) → Tester (verify) chain, enforce invariants, and ship the result.
The full lifecycle is in the parent spec §6; this skill covers Stage 3 + Stage 4
(Tester diagnose-before-developer, verify-before-review).

## Trigger

Run this skill when:
- You are invoked as a profile with `kanban` in its toolset, AND
- The board has at least one card in `ready` status.

## STAGE 1 — Pick + Query + Decide

### 1. Pick the highest-priority ready card

```
terminal(command="hermes kanban list --status ready --order priority 2>/dev/null || hermes kanban list --status ready", workdir="${HERMES_HOME:-$HOME/.hermes}", timeout=30)
```

From the JSON list, select the card with the **highest `priority`** (integer; higher
= picked sooner per kanban schema). If two tie, pick the oldest `created_at`. If
the list is empty, exit cleanly — there is no work.

Record: `CARD_ID`, `CARD_TITLE`, `CARD_BODY`, `CARD_REPO` (parse from body or
`workspace_path`), `CARD_PRIORITY`.

### 2. Claim it: move to `progress`

```
terminal(command="hermes kanban comment <CARD_ID> --body \"[coordinator] claimed at $(date -u +%FT%TZ)\"", workdir="${HERMES_HOME:-$HOME/.hermes}", timeout=15)
```

(Stage 3 uses a comment as the claim marker; the kanban dispatcher's lock is
honored by the gateway runtime via `HERMES_KANBAN_CLAIM_LOCK`. Do not double-claim
a card another runtime has locked.)

### 3. Query the brain for context

Call the brain MCP. The brain is configured in the ZCode/server config as a remote
MCP at `$BRAIN_URL` with Bearer `$BRAIN_BEARER_TOKEN`. From the Coordinator profile
(which has the brain MCP in its toolset), call:

- `query_graph({ repo: "<CARD_REPO>", query: "<CARD_TITLE>", k: 8 })` — top nodes
- `explain({ repo: "<CARD_REPO>", node_id: "<top_node_id>" })` — for the 2-3 highest-scored
- `shortest_path({ repo: "<CARD_REPO>", source: "<entrypoint>", target: "<touched_file>" })` — only if the body names specific files

If the brain returns no graph for the repo, log a warning and proceed with the
card body alone — the loop must not hard-fail on a missing graph.

### 4. Decide: decompose or single?

```
terminal(command="hermes kanban decompose <CARD_ID> --context \"<brain_summary>\"", workdir="${HERMES_HOME:-$HOME/.hermes}", timeout=120)
```

`kanban_decompose` returns JSON. Inspect `fanout`:

- **`fanout: false`** → single task. One Developer. Skip to STAGE 2-single.
- **`fanout: true`** → `tasks: [...]`. Validate (STAGE 2-multi) before creating.

Use the decomposer's rationale; do not second-guess fanout unless it violates an
invariant below.

## STAGE 2 — Assign with invariants

### 2-single. One Developer

Create no new cards. Act as the Developer yourself by invoking the
`dev-loop-developer` skill inline with the original card. (The Developer skill
knows how to read `HERMES_KANBAN_TASK` from the dispatcher env.) Skip to STAGE 3.

### 2-multi. Validate + create parallel cards

Before creating any worker card, enforce BOTH invariants. Fail the card
(`kanban block --kind capability`) if either is violated:

**Invariant A — File-scope non-overlap.** Each `tasks[i].body` must declare a
`files:` line (the decomposer is prompted to include one). Compute the union of
files per task; if any two tasks share a path, block the card with reason
`"decompose file-scope overlap: <paths>"` and stop. Humans re-split the work.

```
terminal(command="python3 - <<'PY'
import json, sys, re
tasks = json.load(sys.stdin)['tasks']
scopes = []
for t in tasks:
    m = re.search(r'files:\\s*(.+)', t['body'])
    scopes.append(set(re.split(r'[,\\s]+', m.group(1).strip()) if m else []))
for i in range(len(scopes)):
    for j in range(i+1, len(scopes)):
        overlap = scopes[i] & scopes[j]
        if overlap:
            print(f\"OVERLAP {i}/{j}: {sorted(overlap)}\"); sys.exit(1)
print('OK non-overlap')
PY", workdir="${HERMES_HOME:-$HOME/.hermes}", timeout=15)
```

(Pipe the decompose JSON in via stdin.)

**Invariant B — 7-agent cap.** Count currently-`progress` cards on the board:

```
terminal(command="hermes kanban list --status progress --limit 200 | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))'", workdir="${HERMES_HOME:-$HOME/.hermes}", timeout=15)
```

If `progress_count + len(tasks) > 7`, do NOT create all tasks at once. Create
only `(7 - progress_count)` now and leave the rest in `ready`. The dispatcher
pool max is 7 (§6); exceeding it causes starvation.

Then create each task via `kanban create` with `assignee=developer`,
`parents=[<CARD_ID>]` (the root is the parent), `priority=CARD_PRIORITY - 1`
(children run slightly hotter), `skills=["dev-loop-developer"]`,
`workspace_path=tasks[i].workspace_path`, `max_runtime_seconds=1800`.

## STAGE 2.5 — Tester diagnoses first for bug cards (Stage 4)

### Before dispatching the Developer — diagnose first for bugs

For every card with `type=bug` (5xx, error report, anomaly), dispatch a Tester
**diagnose** subtask *before* the Developer subtask. Skip this for `type=feature`
cards — features need no reproduction.

1. Create a diagnose child task on the card:
   ```
   terminal(command="hermes kanban create --title 'Tester: diagnose <CARD_TITLE>' --assignee tester --parents <CARD_ID> --priority <CARD_PRIORITY> --skills dev-loop-tester --body 'phase=diagnose. Anchor: <requestId/symptom from card>. Follow dev-loop-tester Phase DIAGNOSE. Post report via kanban_comment, attach screenshots, then kanban_complete.'", workdir="${HERMES_HOME:-$HOME/.hermes}", timeout=30)
   ```
2. Do NOT create the Developer subtask yet. Wait for the diagnose task to reach `done`.
3. When the diagnose task completes, read the diagnosis report from the card comments (`kanban show <CARD_ID>` / `kanban attachments <CARD_ID>`).
4. NOW create the Developer subtask, with the diagnosis injected into its body:
   ```
   terminal(command="hermes kanban create --title 'Developer: fix <CARD_TITLE>' --assignee developer --parents <CARD_ID> --priority <CARD_PRIORITY> --skills dev-loop-developer --body 'phase=fix. Diagnosis ( Tester ): <paste the root-cause + localization + reproduction sections>. Fix the root cause at <file:line>. Do not mask the symptom. Gate: must pass Tester verify after.'", workdir="${HERMES_HOME:-$HOME/.hermes}", timeout=30)
   ```

**Why:** the Developer starts with the root cause, the `file:line`, and a known
reproduction — fixes are accurate instead of guesswork (design §6 "diagnosis-first").
The brain context + evidence come along in the same prompt.

**Gate:** the Coordinator MUST verify the diagnose task is `done` AND the card has
a comment containing `## Tester Diagnosis` before creating the Developer task. If
the diagnose task blocked the card (not-reproducible / needs prod write), do not
proceed — surface to human inbox.

For `type=feature` cards: skip this stage and go straight to STAGE 3 with the
Developer subtask.

## STAGE 3 — Await Developers, then PR

For the single-task path, the Developer runs inline and returns. For the multi
path, poll:

```
terminal(command="hermes kanban list --status progress --assignee developer --limit 50", workdir="${HERMES_HOME:-$HOME/.hermes}", timeout=15)
```

When all child cards are `done` (or any is `blocked`), proceed.

### Before opening the PR — verify the fix (Stage 4)

When the Developer subtask reaches `done` (fix applied, unit tests pass, PR opened on `develop`), do NOT open the PR yet. Dispatch a Tester **verify** subtask first — for *every* fixed card, bug or feature (features verify via their acceptance reproduction; bugs via the diagnosis reproduction).

1. Confirm the dev deploy of the fix finished (the Coolify webhook fires `deploy_success` on `develop`; or query `application_deployment_queues` per `ds6c/RULES.md` for status='finished'). Verifying against a not-yet-deployed fix is invalid.
   ```
   terminal(command="hermes kanban create --title 'Tester: verify <CARD_TITLE>' --assignee tester --parents <CARD_ID> --priority <CARD_PRIORITY> --skills dev-loop-tester --body 'phase=verify. Re-run the reproduction from the diagnosis report verbatim (or the feature acceptance repro). Follow dev-loop-tester Phase VERIFY. Post PASS/FAIL via kanban_comment, then kanban_complete (PASS) or kanban_block (FAIL after cap).'", workdir="${HERMES_HOME:-$HOME/.hermes}", timeout=30)
   ```
2. Wait for the verify task verdict.
   - **PASS** → open the PR (next section). The PR now carries: fix + diagnosis + before/after evidence.
   - **FAIL** → the Tester either re-diagnoses (bounded: 1 re-diagnose + 1 re-verify) or blocks the card. Do NOT open the PR on FAIL.

**Gate:** the PR cannot be opened unless a verify comment `## Tester Verify — PASS` exists on the card. This is the acceptance gate for the loop.

### Open the PR

Hand off to the `github-pr-workflow` skill. The Developer has already committed
on a branch named `dev-loop/<CARD_ID>` (see dev-loop-developer). Invoke:

```
terminal(command="git push -u origin dev-loop/<CARD_ID>", workdir="/repos/<CARD_REPO>", timeout=60)
terminal(command="REMOTE_URL=$(git remote get-url origin) && OWNER_REPO=$(echo \"$REMOTE_URL\" | sed -E 's|.*github\\.com[:/]||; s|\\.git$||') && gh pr create --repo \"$OWNER_REPO\" --base develop --head dev-loop/<CARD_ID> --title \"dev-loop: <CARD_TITLE>\" --body \"Closes kanban card <CARD_ID>.\\n\\n## Changes\\n<from Developer artifacts>\\n\\n## Verification\\n- [x] pnpm lint\\n- [x] pnpm tsc\\n- [x] pnpm test\\n\\n## Brain context\\n<brain_summary>\"", workdir="/repos/<CARD_REPO>", timeout=60)
```

Capture the PR URL and post it back to the root card:

```
terminal(command="hermes kanban comment <CARD_ID> --body \"[coordinator] PR opened: <PR_URL>\"", workdir="${HERMES_HOME:-$HOME/.hermes}", timeout=15)
terminal(command="hermes kanban complete <CARD_ID> --result pr_opened --metadata '{\"pr_url\":\"<PR_URL>\",\"card_id\":\"<CARD_ID>\"}'", workdir="${HERMES_HOME:-$HOME/.hermes}", timeout=15)
```

## STAGE 6 — Escalation (retries exhausted)

If the Developer reports `gate_failed` after 3 ZCode attempts (see
dev-loop-developer), or any child card is `blocked` with
`kind=capability|needs_input`, escalate:

```
terminal(command="hermes kanban block <CARD_ID> --kind needs_input --reason \"dev-loop stalled: <reason>. Human review required.\"", workdir="${HERMES_HOME:-$HOME/.hermes}", timeout=15)
```

Do NOT retry indefinitely. If a Tester verify has already exhausted its re-diagnose
+ re-verify cap and the card is still red, a stalled card goes to a human.

## Rules

1. **Never edit code.** You orchestrate. `Edit`/`Write` are not in your toolset.
2. **One Coordinator per card.** The root card is your blackboard; comment before each state transition.
3. **Respect the lock.** If `HERMES_KANBAN_CLAIM_LOCK` is set and held, exit.
4. **7-agent cap is hard.** Never let `progress` count exceed 7.
5. **File-scope non-overlap is hard.** Block on overlap; do not "merge" overlapping tasks.
6. **Brain is best-effort.** A missing graph degrades context; it does not stop the loop.
7. **Always open a PR.** Stage 3 ends at a PR against `develop`, not a merge.
