---
name: kensa-evals
description: >
  Orchestrate the state-aware Kensa lifecycle from setup through evidence, inspection, approval,
  generation, verification, and iteration.
---

# Kensa Evals

Use this public orchestrator as the only entrypoint after `kensa init`.

Always start with state detection, then route to the first incomplete or broken lifecycle stage.
Compact lifecycle: setup -> evidence -> inspect -> approval -> generate -> verify.

Phase skills end at their own stage boundary. That is a stage handoff, not the end of the run.
After every stage completes, re-run state detection and continue to the next stage in the same
invocation. Stop the run only when a phase skill requires a user decision (approval, credentials,
model cost) or after the verify report.

Lifecycle:

0. Detect state
1. Setup
2. Evidence
3. Inspect
4. Approval
5. Generate
6. Verify
7. Iterate

## 0. Detect State

If `kensa` is not on PATH, fall back to `uv run kensa`, then `python -m kensa`, and keep the
working form for every later command.

Check, in order:

1. `[tool.kensa]` in the nearest `pyproject.toml`
2. `tests/evals/conftest.py`
3. `tests/evals/test_kensa_smoke.py`
4. `kensa doctor --json`
5. `.kensa/traces/imports/*`
6. `.kensa/traces/runs/*`
7. `.kensa/inspect/*.yaml`
8. Any approved queue item via `kensa inspect list --status approved --json`
9. Non-smoke `tests/evals/test_*.py`
10. `kensa eval --json`

Route to the first incomplete or broken stage.

## 1. Setup

Use `kensa-setup`.

That phase must only:

1. Wire `tests/evals/conftest.py::kensa_run(case)`.
2. Call the real local agent or application boundary.
3. Mock only external side effects.
4. Record at least one LLM span.
5. Run `kensa doctor` until harness readiness passes.

Do not import traces, inspect traces, or write evals during setup.

## 2. Evidence

Read `[tool.kensa].evidence_source` from the nearest `pyproject.toml` when it is saved. Valid values
are `langfuse`, `trace_export`, and `local`. If no saved source exists, infer it from the repo
state or ask the user.

Trace access fails closed. If import or trace access reports a redaction blocker, run
`kensa init` if needed, then re-import. Never use raw runtime traces as evidence.

For `langfuse`:

```bash
kensa connect langfuse
kensa import --from langfuse
```

Langfuse imports need network access. Under a sandboxed harness that blocks network by default,
request escalation for these commands instead of deferring or switching sources. Rate-limit
backoff with retries is normal; let a healthy retry finish. Kensa supports both the legacy Langfuse
trace API and observations-v2/events-only deployments.

Use the CLI default import scope unless the user specifies one, and ask before substantially expanding it.

For `trace_export`:

```bash
kensa import --from <provider> --source <file>
```

If the provider or source file is not saved anywhere, infer it from repo state or ask.

For `local`:

1. Run one representative local case through the real boundary. The real boundary includes the
   real model call; capturing evidence from a stubbed model requires the same explicit user
   approval as stubbing in setup, and the import must be reported as stub-derived.
2. Capture with `kensa.instrument()` and `KENSA_TRACE_DIR`.
3. Import the captured JSONL from a path under `.kensa/` (already gitignored), not a temp dir,
   so provenance survives without committing raw pre-redaction captures.

If no safe realistic case is obvious, ask the user for one.

## 3. Inspect

Use `kensa-inspect`.

That phase must only:

1. Read normalized TraceView evidence.
2. Write `.kensa/inspect/<timestamp>.yaml`.
3. Propose concrete eval ideas.
4. Mark every idea `status: pending`.
5. Run `kensa inspect lint` until it exits clean.

Do not write pytest files during inspection.

## 4. Approval

Present every pending queue item to the user for an approve/reject decision. Use the harness's
structured question tool when it is available this turn; otherwise ask in plain chat and wait.
Always report the inspect queue path and both approval mechanics: reply in chat, or edit queue
items to `status: approved`.

Valid approvals are:

1. Explicit user approval in chat.
2. A queue item changed to `status: approved`, as reported by
   `kensa inspect list --status approved --json`.

When approval arrives in chat, immediately edit only the approved items in the queue file to
`status: approved` so the file stays the source of truth for generation.

If the current harness mode cannot edit files (for example a plan or read-only mode), ask the
user to switch modes, then persist approvals and continue. Never emit a plan in place of
generation.

Never generate pending or rejected ideas.

## 5. Generate

Use `kensa-generate`.

That phase must only:

1. Read approved ideas or cases.
2. Write focused `tests/evals/test_*.py` files.
3. Use `kensa.pytest` APIs.
4. Run targeted pytest or `kensa eval`.
5. Fix eval mistakes.

Do not import traces or invent new inspect ideas during generation.

## 6. Verify

Run:

```bash
kensa eval
kensa doctor
```

Report:

1. Harness readiness.
2. Trace source.
3. Inspect queue path.
4. Approved evals generated.
5. Eval pass/fail, separating eval mistakes from product findings. A failure matching a queue
   item's `expected_current_behavior: fail` is a product finding; report the evidence and the
   recommended code fix.
6. Whether the model was real or stubbed for these results.
7. Doctor status.
8. Remaining blockers.

## 7. Iterate

Route failures:

1. Harness broken -> `kensa-setup`
2. Missing or bad evidence -> evidence stage
3. No approved ideas -> approval stage
4. Eval file broken -> `kensa-generate`
5. Agent behavior failure -> report the failure and recommend the code or product fix
