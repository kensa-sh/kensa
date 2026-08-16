---
name: kensa-setup
description: >
  Use this skill to connect a repository's Kensa pytest harness to an approved production-owned
  invocation boundary and finish when `kensa doctor` passes.
---

# Kensa Setup

Normally invoked by `kensa-evals`.

Use this skill only for harness readiness. Setup is complete when `kensa doctor` passes; otherwise
finish with an actionable `cannot wire` result before editing.

## Boundary

The target repository owns its production agent, invocation seam, prompts, tools, routing,
configuration, conversation state, dependencies, effects, and lifecycle. The setup agent may
author only a thin repository-owned `kensa_run` adapter after the user approves the seam.

Kensa owns execution after fixture resolution: simulated turns, trial isolation, timeouts,
tracing, judging, artifacts, reports, and readiness. Never reconstruct missing production behavior
inside the fixture.

## Workflow

1. Inspect read-only, in order: documented run paths, application entrypoints, tests and factories,
   agent constructors or orchestrators, model and tool call sites, then their callers. Inspect an
   existing user-authored `tests/evals/conftest.py::kensa_run(case)` without overwriting it.
2. Trace inward from a real entrypoint and outward from model and tool calls until both reach the
   same production-owned invocation seam. Follow source control flow even for unfamiliar frameworks.
3. Record exact source locations, construction path, input and output mapping, conversation-state
   owner, resource lifecycle, external effects, and unresolved gaps. Cite every proposed seam.
4. Before editing, present one seam and minimal adapter with the production symbol and source
   location, construction and invocation path, mappings, state and lifecycle ownership, effects,
   safe dependencies, and unresolved gaps. Wait for explicit approval of the seam, adapter,
   real-model cost, and live effects. Do not edit first.
5. After approval, make `kensa_run(case)` return one case-aware `ConversationAgent` around the seam.
   Preserve one production-owned conversation instance per trial and across simulated turns. Map
   Kensa messages and results through `ConversationResponse`; do not reproduce prompts, tools,
   routing, state, configuration, or lifecycle.
6. Inject only approved external effects. The model call is part of the agent, so stubbing it needs
   explicit approval and must be reported as covering plumbing and guardrails, not model behavior.
7. Fail clearly when construction, credentials, clients, modules, or the seam are unavailable.
   Add Kensa tracing where needed, then run `kensa doctor` without replacing production behavior.
   Its persistent smoke, real LLM span unless stubbing was approved, and authenticity checks remain
   mandatory.

## Cannot wire

Ask the user to select among plausible seams before giving up. Stop before editing and report
`cannot wire` when wiring would still require guessing, reproducing agent behavior, bypassing
production construction, changing production code, or hiding an unsafe effect. Include the exact
reason, the target-owned decision or seam required, and state that there was no fixture edit or
readiness claim.

If production code must expose a new headless or injectable seam, report that required target-owned
change and stop. Continue only if the user separately authorizes production changes.

## Credentials

Credential rule: detect credential presence by name only. Never read, print, copy, transform,
validate, export, create, edit, or weaken API keys, `.env` files, shell profiles, or credential
stores. Shell environment checks cannot see dotenv-resident values, so never assert credentials
are absent from name checks alone; say which locations were checked and ask the user. If the app
already declares or imports a local/staging dotenv path, you may persist only
that path in `pyproject.toml` as `[tool.kensa] dotenv = "<path>"` so future Kensa commands use the
same credential source. Do not read or edit the dotenv file. If a run will consume already
configured local or staging model credentials, explicit user approval is required.

Before the first approved real-model run, ask once whether approval covers the session, this run,
or neither. If blocked, name the exact variable and dotenv options, then wait. If redaction is not
ready, rerun `kensa init`; never edit readiness files manually.

## Handoff

On `kensa doctor` success, return to `kensa-evals`. Do not import traces, inspect traces, propose
eval ideas, or write pytest eval files here. A `cannot wire` result ends setup.
