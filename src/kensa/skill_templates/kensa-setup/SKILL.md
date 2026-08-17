---
name: kensa-setup
description: >
  Connect `tests/evals/conftest.py::kensa_run(case)` to an approved production function or class
  that starts one conversation. Use when setting up a Kensa pytest harness; finish when
  `kensa doctor` passes.
---

# Kensa Setup

Normally invoked by `kensa-evals`.

Use this skill only for harness readiness. Setup is complete when `kensa doctor` passes; otherwise
report `cannot wire` with the exact reason and required repository change before editing.

## Ownership

The target repository owns its production agent, the function or class used to start one
conversation, prompts, tools, routing, configuration, conversation state, dependencies, external
side effects, resource creation, and cleanup. The setup agent may write only a repository-owned
`kensa_run` adapter after the user approves the production code and proposed mapping.

Kensa owns execution after fixture resolution: simulated turns, trial isolation, timeouts,
tracing, judging, artifacts, reports, and readiness. Never reconstruct missing production behavior
inside the fixture.

## Framework discovery

Before manual tracing, run the bundled read-only detector from the target repository:

```bash
python scripts/detect_frameworks.py --root .
```

The identity data used by the detector is in `assets/frameworks.json`.

Every detector match is an unconfirmed candidate. The detector provides import evidence only; it
never selects a production target or establishes relevance, confidence, safety, or readiness.
Multiple candidates may be emitted for one import and must not be ranked or resolved by the
detector output.

Confirm candidates against actual application control flow before any version or documentation
lookup. Resolve versions only for confirmed candidates, using installed package metadata or the
repository's dependency and lock data. Read version-matched official documentation only for
confirmed candidates, starting from that candidate's registry URL.

If no candidate is confirmed, or discovery is empty or incomplete, continue the generic forward
and backward source tracing in the workflow below. Discovery never blocks that path.

## Workflow

1. Inspect read-only, in order: documented run paths, application entrypoints, tests and factories,
   agent constructors or orchestrators, model and tool call sites, then their callers. Inspect an
   existing user-authored `tests/evals/conftest.py::kensa_run(case)` without overwriting it.
2. Trace forward from a real application entry point and backward from model and tool calls until
   both identify the same function or class that starts the production agent. Follow actual program
   control flow even for unfamiliar frameworks.
3. Record exact source locations, construction path, input and output mapping, conversation-state
   owner, resource creation and cleanup, external side effects, and unresolved gaps. Cite every
   proposed function or class.
4. Before editing, present one production function or class to call and the proposed adapter. Include
   its exact symbol and source location, construction and call path, mappings, state and resource
   owners, cleanup, external side effects, safe dependencies, and unresolved gaps. Wait for explicit
   approval of the production call, adapter, real-model cost, and live side effects. Do not edit first.
5. After approval, make `kensa_run(case)` return one case-aware `ConversationAgent` that delegates to
   the approved production conversation.
   Preserve one production-owned conversation instance per trial and across simulated turns. Map
   Kensa messages and results through `ConversationResponse`; do not reproduce prompts, tools,
   routing, state, configuration, resource creation, or cleanup.
6. Inject only approved external effects. The model call is part of the agent, so stubbing it needs
   explicit approval and must be reported as covering adapter construction and safety checks, not
   model behavior.
7. Fail clearly when construction, credentials, clients, modules, or the selected production symbol
   are unavailable.
   Add Kensa tracing where needed, then run `kensa doctor` without replacing production behavior.
   Its persistent smoke, real LLM span unless stubbing was approved, and authenticity checks remain
   mandatory.

## Cannot wire

Ask the user to select among plausible production functions or classes before giving up. Stop before
editing and report `cannot wire` when the adapter would still require guessing, reproducing agent
behavior, bypassing production construction, changing production code, or hiding an unsafe external
side effect. Include the exact reason and required repository change or user decision, and state
that there was no fixture edit or readiness claim.

If production code must add a callable that starts one conversation without a UI or server request,
or must accept dependencies explicitly, report that required repository change and stop. Continue
only if the user separately authorizes production changes.

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
