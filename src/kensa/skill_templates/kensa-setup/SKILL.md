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
2. Connect `tests/evals/conftest.py::kensa_run(case)` to the real local agent or
   application boundary.
3. Mock only external side effects. Do not replace the agent with a fake implementation.
   The model call is part of the agent, not an external side effect. Replacing or stubbing the
   model client requires explicit user approval and must state the consequence: the resulting
   evals pin plumbing and guardrails only, not model behavior.
4. Before the first run that consumes real model credentials, ask the user one session-scoped
   cost question with three options: approve real model calls for this session, approve this run
   only, or stop. Cite that approval for later runs instead of re-asking.
5. Guard `kensa_run` against silent fallbacks: raise a clear error when a required credential,
   client, or module is missing instead of letting the boundary degrade to a no-model path.
6. Wrap model calls with Kensa tracing helpers when needed so the persistent smoke eval records
   at least one LLM span.
7. Run `kensa doctor` and fix harness blockers until it passes. When blocked on a missing
   credential, name the exact variable and the dotenv file options in a single ask, then wait.
8. Know what `kensa init` and `kensa doctor` do for mandatory trace redaction. When a trace
   source is configured, `kensa init` offers to install the `kensa[redaction]` dependencies
   through the project's package manager (`uv add --group traces 'kensa[redaction]'` with a
   `uv.lock`, otherwise `pip install 'kensa[redaction]'`), downloads and checksum-verifies the
   pinned spaCy model, writes `.kensa/redaction.json`, and records an explicit
   `evidence_environment` (`local`, `staging`, or `production`) in `.kensa/settings.json`.
   For noninteractive setup, pass `--evidence-environment` alongside `--trace-source`.
   `kensa doctor` reports redaction dependency presence, readiness, model tier, evidence
   environment, and any unsafe old artifacts. If doctor reports redaction not ready, re-run
   `kensa init`; trace import and payload exposure stay blocked until readiness exists. Never
   bypass this by editing `.kensa/redaction.json` by hand.

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
