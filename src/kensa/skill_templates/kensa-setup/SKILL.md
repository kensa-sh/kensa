---
name: kensa-setup
description: >
  Connect a repository's Kensa pytest harness to an approved production-owned invocation boundary
  and finish when `kensa doctor` passes.
---

# Kensa Setup

Normally invoked by `kensa-evals`.

Use this skill only for harness readiness. It ends in one of two states: an approved production
adapter passes `kensa doctor`, or discovery reports an actionable `cannot wire` result before
editing.

## Ownership boundary

The target repository owns its production agent, reusable invocation seam, prompts, tools,
routing, provider configuration, conversation state, dependencies, effects, and resource
lifecycle. The setup agent may author only a thin repository-owned `kensa_run` adapter after the
user approves the discovered seam.

Kensa owns execution after fixture resolution: simulated turns, trial isolation, timeouts,
tracing, judging, artifacts, reports, and readiness verification. Do not fill a missing production
seam by reconstructing agent behavior inside the fixture.

## Discovery and proposal

1. Inspect the repository read-only in this fixed order:
   1. documented run paths
   2. application entrypoints
   3. tests and factories
   4. agent constructors or orchestrators
   5. model and tool call sites
   6. their callers
2. During the tests and factories stage, if `tests/evals/conftest.py::kensa_run(case)` already
   contains user-authored behavior, inspect and verify it, never silently overwrite it.
3. Trace inward from a real application entrypoint and outward from model and tool call sites until
   both paths identify the same production-owned invocation boundary. An unfamiliar framework is
   not a blocker. Follow source control flow instead of selecting from a framework allowlist.
4. Record source-backed evidence with exact source locations, construction path, input and output
   mapping, conversation-state owner, resource lifecycle, external effects, and unresolved gaps.
   Cite the source facts behind every proposed seam.
5. Before editing, present one proposed seam and minimal adapter. Include the production symbol and
   source location, construction and invocation path, input and output mapping, conversation state
   and resource lifecycle, external effects and their proposed safe dependencies, minimal fixture
   adapter, and unresolved gaps.
6. Wait for explicit user approval of the seam and adapter, plus any real-model cost or live effects.
   If multiple boundaries remain plausible, ask the user to select one. Do not create or edit the
   fixture before approval.

## Approved adapter

After approval:

1. Make `tests/evals/conftest.py::kensa_run(case)` construct one case-aware adapter per trial around
   the approved production seam. Preserve one production-owned conversation instance per trial,
   including across simulated turns, and preserve production setup and teardown ownership.
2. Map Kensa cases and messages into the production input and map the production result into
   `ConversationResponse`. The adapter must not reproduce prompts, tools, routing, state, configuration, or lifecycle.
3. Mock or inject only external side effects approved in the proposal. Do not replace the agent
   with a fake implementation. The model call is part of the agent, not an external side effect.
   Replacing or stubbing the model client requires explicit user approval and must state that the
   evals then cover plumbing and guardrails, not model behavior.
4. Guard construction and `respond` against silent fallbacks. Raise a clear error when a required
   credential, client, module, or production seam is unavailable.
5. Before the first approved run that consumes real model credentials, ask one session-scoped cost
   question with three options: approve real model calls for this session, approve this run only,
   or stop. Cite that approval for later runs instead of re-asking.
6. Wrap model calls with Kensa tracing helpers when needed. Run `kensa doctor` and resolve harness
   blockers without replacing production behavior. The persistent smoke and harness-authenticity
   checks remain mandatory. The smoke must record at least one LLM span, and that span must come
   from the real model call unless model stubbing was explicitly approved.
7. If redaction is not ready, rerun `kensa init`; do not create or edit readiness files manually.
   When blocked on a missing credential, name the exact variable and dotenv file options in one ask,
   then wait.

## Cannot wire

Stop before editing and report `cannot wire` when multiple plausible production boundaries remain,
or when wiring requires reproducing agent behavior, bypassing production construction, changing
production code, or hiding an unsafe effect. Report the exact reason and the target-owned decision
or seam required. State that there was no fixture edit and no readiness claim.

If production code must expose a new headless or injectable seam, report that required target-owned
change and stop. Continue only if the user separately authorizes production changes.

Successful setup is complete when `kensa doctor` passes. Hand back to `kensa-evals` for evidence
collection. A `cannot wire` result ends setup. Do not import traces, inspect traces, propose eval
ideas, or write pytest eval files in this skill.

Credential rule: detect credential presence by name only. Never read, print, copy, transform,
validate, export, create, edit, or weaken API keys, `.env` files, shell profiles, or credential
stores. Shell environment checks cannot see dotenv-resident values, so never assert credentials
are absent from name checks alone; say which locations were checked and ask the user. If the app
already declares or imports a local/staging dotenv path, you may persist only
that path in `pyproject.toml` as `[tool.kensa] dotenv = "<path>"` so future Kensa commands use the
same credential source. Do not read or edit the dotenv file. If a run will consume already
configured local or staging model credentials, explicit user approval is required.
