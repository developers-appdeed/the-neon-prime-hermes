---
name: dev-loop-developer
description: "Execute a single kanban card as the Developer role: compose a diagnosis-injected prompt, invoke headless ZCode (one-shot, then bounded --resume on gate failure), enforce the file-scope gate (filesChanged ⊆ allowed), run repo gates (lint/tsc/test or flutter analyze/test), and complete the card with artifacts. Use when a card is assigned to the developer profile."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Dev-Loop, Developer, ZCode, Gates, Scope-Check]
    related_skills: [zcode-executor, dev-loop-coordinator, github-pr-workflow]
---

# Dev Loop — Developer

You are the Developer. You run ZCode headless to make the change, then you prove
it passes the repo's own gates. You do NOT open the PR — the Coordinator does.

## Inputs

Read from the dispatcher env / card:
- `HERMES_KANBAN_TASK` — the card ID
- Card `body` — the task spec (must include a `files:` line listing allowed paths)
- `workspace_path` or `CARD_REPO` — the repo root (e.g. `/repos/the-neon-prime-fastify`)
- Diagnosis (Stage 4) — NOT present in Stage 3. The card body is the only spec.

## Step 1 — Compose the prompt

Build a single string with these sections, in order:

1. **Task** — the card title + body verbatim.
2. **Allowed files** — the `files:` list. State: "Edit ONLY these paths. Any other change fails the gate."
3. **Brain context** (best-effort) — paste the Coordinator's `query_graph`/`explain` output if provided in the card comments (prefix `[coordinator]`). If absent, omit.
4. **Hard constraints**:
   - Do not run `rm -rf`, `git push`, `git reset --hard`, `git rebase`.
   - Do not touch files outside the allowed list.
   - Commit on branch `dev-loop/<CARD_ID>` with a conventional-commit message.
   - Stop when the change is complete and staged.
5. **Verify before stopping**:
   - For a pnpm repo: `pnpm lint && pnpm tsc --noEmit && pnpm test`
   - For a flutter repo: `flutter analyze && flutter test`
   - If a gate fails, fix it within the allowed scope; do not skip.

Write the composed prompt to `/tmp/zcode-prompt-<CARD_ID>.txt` (avoids shell-quoting hell):

```
terminal(command="cat > /tmp/zcode-prompt-<CARD_ID>.txt <<'EOF'
## Task
<TITLE>
<BODY>

## Allowed files
<FILES_LIST>

## Brain context
<BRAIN_SUMMARY_OR_NONE>

## Hard constraints
- Edit ONLY the allowed files.
- Do NOT run: rm -rf, git push, git reset --hard, git rebase.
- Create branch dev-loop/<CARD_ID> from develop, commit with a conventional message.
- Stop once changes are staged and committed.

## Verify before stopping
Run the repo gates and ensure they pass. Fix failures within scope.
EOF", workdir="/repos/<CARD_REPO>", timeout=10)
```

## Step 2 — Branch + one-shot ZCode

```
terminal(command="git fetch origin && git checkout develop && git pull origin develop && git checkout -b dev-loop/<CARD_ID>", workdir="/repos/<CARD_REPO>", timeout=60)
```

Then invoke ZCode via the `zcode-executor` skill's one-shot pattern. The exact
command (see zcode-executor for flag reference):

```
terminal(command="node /opt/zcode/zcode.cjs --prompt \"$(cat /tmp/zcode-prompt-<CARD_ID>.txt)\" --cwd /repos/<CARD_REPO> --mode yolo --max-turns 25 --json --allowed-tools \"Read Edit Write Bash(pnpm *):* Bash(git add:*):* Bash(git commit:*):* Bash(git checkout:*):* Bash(git branch:*):* Bash(git fetch:*):*\" --disallowed-tools \"Bash(rm *):* Bash(git push:*):* Bash(git reset:*):* Bash(git rebase:*):*\" --settings /opt/zcode/config.json > /tmp/zcode-result-<CARD_ID>.json 2>/tmp/zcode-result-<CARD_ID>.err", workdir="/repos/<CARD_REPO>", timeout=1800)
```

`--max-turns 25` is the §7 default; `timeout=1800` (30 min) is the ceiling for a one-shot.

## Step 3 — Parse + scope-check gate

Read `/tmp/zcode-result-<CARD_ID>.json`. The `--json` envelope has `session_id`,
`result`, `total_cost_usd`, `num_turns`, and (when files were edited)
`filesChanged: [...]`.

```
terminal(command="python3 - <<'PY'
import json, sys, subprocess
r = json.load(open('/tmp/zcode-result-<CARD_ID>.json'))
print('session_id:', r.get('session_id'))
print('num_turns:', r.get('num_turns'))
print('cost_usd:', r.get('total_cost_usd'))
changed = set(r.get('filesChanged') or [])
allowed = set(\"<FILES_SPACE_SEPARATED>\".split())
violators = changed - allowed
if violators:
    print('SCOPE_CHECK_FAILED:', sorted(violators)); sys.exit(2)
# commit any unstaged allowed changes ZCode left behind
if changed:
    subprocess.run(['git','add',*sorted(changed)], check=True)
    subprocess.run(['git','commit','-m','dev-loop: <CARD_ID> (scope-checked)'], check=False)
print('SCOPE_CHECK_OK')
PY", workdir="/repos/<CARD_REPO>", timeout=30)
```

- Exit `2` (SCOPE_CHECK_FAILED) → revert and go to Step 5 (bounded retry) with a
  tightened prompt that names the violators. After 3 failures, block the card.
- Exit `0` (SCOPE_CHECK_OK) → continue to Step 4.

## Step 4 — Repo gates

Detect repo kind by file presence and run the matching gate:

```
terminal(command="if [ -f pnpm-workspace.yaml ] || [ -f package.json ]; then pnpm lint && pnpm tsc --noEmit && pnpm test; elif [ -f pubspec.yaml ]; then flutter analyze && flutter test; else echo 'NO_KNOWN_GATE — skip'; fi", workdir="/repos/<CARD_REPO>", timeout=900)
```

- Pass → Step 6 (complete card).
- Fail → Step 5 (bounded retry).

## Step 5 — Bounded debug loop (--resume)

ZCode sessions are resumable. On any gate/scope failure, resume the SAME session
with the failure output as new context. Max 3 attempts total (1 one-shot + 2
resumes). Capture the session_id from the first result.

```
terminal(command="node /opt/zcode/zcode.cjs --resume <SESSION_ID> --prompt \"The previous attempt failed.\\n\\nFailure:\\n<GATE_OR_SCOPE_OUTPUT>\\n\\nFix it within the allowed file scope and re-run the gates.\" --cwd /repos/<CARD_REPO> --mode yolo --max-turns 15 --json --allowed-tools \"Read Edit Write Bash(pnpm *):* Bash(git add:*):* Bash(git commit:*):*\" --disallowed-tools \"Bash(rm *):* Bash(git push:*):* Bash(git reset:*):*\" --settings /opt/zcode/config.json > /tmp/zcode-result-<CARD_ID>-r<ATTEMPT>.json 2>/tmp/zcode-result-<CARD_ID>-r<ATTEMPT>.err", workdir="/repos/<CARD_REPO>", timeout=1200)
```

Then re-run Step 3 (scope-check) and Step 4 (gates) against the resumed result.
After the 3rd attempt fails, block the card and let the Coordinator escalate:

```
terminal(command="hermes kanban block <CARD_ID> --kind capability --reason \"dev-loop: gates failed after 3 attempts. Last failure: <LAST_FAILURE>\"", workdir="${HERMES_HOME:-$HOME/.hermes}", timeout=15)
```

## Step 6 — Complete the card

```
terminal(command="git log --format='%H %s' develop..dev-loop/<CARD_ID> > /tmp/dev-loop-commits-<CARD_ID>.txt && git diff --stat develop..dev-loop/<CARD_ID> > /tmp/dev-loop-diffstat-<CARD_ID>.txt", workdir="/repos/<CARD_REPO>", timeout=30)
terminal(command="hermes kanban complete <CARD_ID> --result gate_passed --metadata '{\"branch\":\"dev-loop/<CARD_ID>\",\"session_id\":\"<SESSION_ID>\",\"files_changed\":[<FILES_JSON>],\"cost_usd\":<COST>}' --artifacts '{\"commits\":\"/tmp/dev-loop-commits-<CARD_ID>.txt\",\"diffstat\":\"/tmp/dev-loop-diffstat-<CARD_ID>.txt\"}'", workdir="${HERMES_HOME:-$HOME/.hermes}", timeout=15)
```

The Coordinator (watching the root card) sees `done` and opens the PR.

## Rules

1. **One card, one branch.** Branch is always `dev-loop/<CARD_ID>` from `develop`.
2. **Scope-check is absolute.** A single file outside the allowed list fails the card.
3. **Gates are absolute.** No skipping lint/tsc/test. `NO_KNOWN_GATE` only when the repo has neither pnpm nor flutter manifests.
4. **3 attempts max.** Then block — do not loop forever.
5. **Never push.** The Coordinator pushes and opens the PR.
6. **Commit on every successful gate pass** so a resumed session starts clean.
