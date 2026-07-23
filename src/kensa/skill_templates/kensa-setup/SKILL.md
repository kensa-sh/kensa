---
name: kensa-setup
description: >
  Connect a repository's Kensa pytest harness to the real local agent or app boundary
  and finish when `kensa doctor` passes.
---

# Kensa Setup

Normally invoked by `kensa-evals`.

Use this skill only for harness readiness.

Do:

1. Inspect the repository entrypoints, tests, app factories, and dependency wiring. For any
   non-trivial repo, run a read-only exploration pass first: list every model-client call site
   with exact file, line, and signature, then rank boundaries by self-containedness. Prefer a
   boundary with plain data in and out and the fewest DB, session, or org dependencies.
2. Make `tests/evals/conftest.py::kensa_run(case)` construct one case-aware agent instance per
   trial. Its `respond(messages)` method must return `ConversationResponse` from the real local
   agent or application boundary. Preserve fixture setup and teardown ownership.
3. Mock only external side effects. Do not replace the agent with a fake implementation.
   The model call is part of the agent, not an external side effect. Replacing or stubbing the
   model client requires explicit user approval and must state the consequence: the resulting
   evals pin plumbing and guardrails only, not model behavior.
4. Before the first run that consumes real model credentials, ask the user one session-scoped
   cost question with three options: approve real model calls for this session, approve this run
   only, or stop. Cite that approval for later runs instead of re-asking.
5. Guard agent construction and `respond` against silent fallbacks: raise a clear error when a
   required credential, client, or module is missing instead of letting the boundary degrade to a
   no-model path.
6. Wrap model calls with Kensa tracing helpers when needed so the persistent smoke eval records
   at least one LLM span.
7. Run `kensa doctor` and fix harness blockers until it passes. If redaction is not ready, rerun
   `kensa init`; do not create or edit readiness files manually. When blocked on a missing credential,
   name the exact variable and the dotenv file options in a single ask, then wait.

This skill is complete when `kensa doctor` passes; hand back to `kensa-evals`, which continues
with the evidence stage in the same run. Do not import traces, inspect traces, propose eval
ideas, or write pytest eval files in this skill.

Credential rule: detect credential presence by name only. Never read, print, copy, transform,
validate, export, create, edit, or weaken API keys, `.env` files, shell profiles, or credential
stores. Shell environment checks cannot see dotenv-resident values, so never assert credentials
are absent from name checks alone; say which locations were checked and ask the user. If the app
already declares or imports a local/staging dotenv path, you may persist only
that path in `pyproject.toml` as `[tool.kensa] dotenv = "<path>"` so future Kensa commands use the
same credential source. Do not read or edit the dotenv file. If a run will consume already
configured local or staging model credentials, explicit user approval is required.
