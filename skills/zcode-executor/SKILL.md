---
name: zcode-executor
description: "Delegate coding to ZCode headless CLI (one-shot or --resume). Use for features, fixes, refactors, and bounded debug loops. Returns JSON with session_id, result, filesChanged, total_cost_usd, num_turns. The low-level executor used by dev-loop-developer and (Stage 4) the Tester."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Coding-Agent, ZCode, Headless, JSON, Dev-Loop]
    related_skills: [dev-loop-developer, dev-loop-coordinator, claude-code, codex]
---

# ZCode Headless Executor

Delegate coding tasks to [ZCode](https://z.ai) via its headless CLI. ZCode is a
Z.ai-powered autonomous coding agent; the headless mode runs without a TUI and
emits JSON, making it scriptable from the dev loop.

## When to use

- Implementing a kanban card's spec (one-shot)
- Resuming a failed session with new context (bounded debug loop)
- Any code change inside the dev loop

## Prerequisites

- The `zcode.cjs` bundle at `/opt/zcode/zcode.cjs` (server) or
  `/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs` (macOS dev).
- A server-side `config.json` at `/opt/zcode/config.json` declaring the model
  provider explicitly (the TUI injects it at runtime; headless does not). See
  `ds6c/infra/hermes/zcode/config.json`.
- Node 22 LTS on PATH.
- Run inside a git repository (ZCode, like Codex, refuses outside one).

## Two modes

### One-shot (Mode A, ~90% of dev-loop calls)

```
terminal(command="node /opt/zcode/zcode.cjs --prompt \"<task>\" --cwd /repos/<repo> --mode yolo --max-turns 25 --json --allowed-tools \"Read Edit Write Bash(pnpm *):* Bash(git add:*):* Bash(git commit:*):* Bash(git checkout:*):*\" --disallowed-tools \"Bash(rm *):* Bash(git push:*):* Bash(git reset:*):*\" --settings /opt/zcode/config.json > result.json 2> result.err", workdir="/repos/<repo>", timeout=1800)
```

### Resume (bounded debug loop)

```
terminal(command="node /opt/zcode/zcode.cjs --resume <SESSION_ID> --prompt \"<new context>\" --cwd /repos/<repo> --mode yolo --max-turns 15 --json --settings /opt/zcode/config.json > result-r1.json 2> result-r1.err", workdir="/repos/<repo>", timeout=1200)
```

`--resume` continues an existing session (same branch, same memory). Use it when
a one-shot's gates failed and you want ZCode to fix its own output with the
failure as new context. Keep the total (one-shot + resumes) ≤ 3.

## CLI Flags Reference

| Flag | Effect |
|------|--------|
| `--prompt <text>` | One-shot task text. Exits when done. |
| `--cwd <path>` | Working directory (must be a git repo). |
| `--mode <mode>` | `yolo` (no approvals, default for --prompt), `build`, `edit`, `plan`. |
| `--max-turns <n>` | Ceiling on agent turns. 25 for one-shot, 15 for resume. |
| `--json` | Emit JSON envelope on stdout (see schema below). |
| `--allowed-tools "..."` | Whitelist. Tool format: `Read`, `Edit`, `Write`, `Bash(<glob>):*`. |
| `--disallowed-tools "..."` | Blacklist (overrides allow). Use for destructive ops. |
| `--resume <sessionId>` | Continue a prior session. |
| `--settings <path>` | Path to config.json (server headless requires this). |
| `--target <text>` | Optional build target hint. |
| `--verbose` | Debug logging to stderr. |

## `--json` result schema

```json
{
  "session_id": "sess_abc123",
  "subtype": "output_text",
  "result": "<final message from the agent>",
  "filesChanged": ["src/foo.ts", "tests/foo.test.ts"],
  "total_cost_usd": 0.0421,
  "num_turns": 7,
  "is_error": false
}
```

`filesChanged` is the contract the dev-loop-developer scope-check gate reads.
If `is_error` is true, treat the whole run as a gate failure and resume or block.

## Per-role tool scopes (from §7)

**Developer** (the only Stage 3 role):
- allowed: `Read`, `Edit`, `Write`, `Bash(pnpm *):*`, `Bash(git add:*):*`, `Bash(git commit:*):*`, `Bash(git checkout:*):*`, `Bash(git branch:*):*`, `Bash(git fetch:*):*`
- disallowed: `Bash(rm *):*`, `Bash(git push:*):*`, `Bash(git reset:*):*`, `Bash(git rebase:*):*`, any prod DB MCP

**Tester** (Stage 4 — not enabled yet, documented for forward-compat):
- allowed: `Read`, `Bash(pnpm test):*`, `Bash(pnpm tsc):*`, dev DB MCPs (read-only)
- disallowed: `Edit`, `Write`, `Bash(git commit):*`, prod DB MCPs

## Composing a diagnosis-injected prompt

Stage 3 has no Tester, so "diagnosis" is the card body. In Stage 4 the
Tester's diagnosis JSON gets spliced in at the same position. The shape:

```
## Task
<card title + body>

## Diagnosis   <-- Stage 4 only; omit in Stage 3
<tester diagnosis JSON>

## Allowed files
<files list>

## Brain context
<query_graph / explain output>

## Hard constraints
<no push, no reset, scope-only, commit on branch>

## Verify before stopping
<repo gates>
```

## Rules

1. **Always `--json`.** The loop parses the envelope; free-text output is unusable.
2. **Always `--settings` on the server.** Headless ZCode without an explicit config cannot find the provider.
3. **Always `--cwd` into a git repo.** ZCode refuses otherwise.
4. **`--mode yolo` for the dev loop.** Approvals would hang the headless run.
5. **Tight `--allowed-tools`.** The scope-check gate is defense-in-depth; the tool whitelist is the primary fence.
6. **3 attempts max.** One one-shot + two `--resume`. Then block.
7. **Capture session_id on every run** — it's the handle for resume and for the card metadata.
