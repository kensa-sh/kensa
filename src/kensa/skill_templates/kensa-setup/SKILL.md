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

Run the bundled detector once from the target repository root before any manual source tracing:

```bash
python .claude/skills/kensa-setup/scripts/detect_frameworks.py --root .
```

Replace `.claude/skills` with the skills root this skill was installed under, such as
`.agents/skills` or `.cursor/skills`.

`scripts/detect_frameworks.py` and its identity registry `assets/frameworks.json` ship with this
skill and must stay next to each other. The detector reads dependency metadata and Python source syntax only. It never imports,
executes, or mutates the target, never reads dotenv files or credential stores, and never reaches
the network. It prints one stably ordered JSON document with declared distributions and version
evidence, imported framework modules and symbols with repository-relative paths and line numbers,
call references through directly imported names, direct model-client evidence, registry aliases and
replacements, scan exclusions, parse failures, and coverage gaps.

Read the result as evidence, never as a decision:

- A declared dependency without a source import is declared-only.
- An import without a call reference is imported-only.
- Call references are syntactic. They are not proof of production use.
- Multiple matches are all reported. Confirm each one against actual control flow.
- A `replaced_by` entry names the maintained replacement for a legacy or superseded framework.
- Gaps and exclusions are real blind spots. Inspect those paths by hand when they matter.
- A `first_party_shadowed_import_root` gap means a repository-local module has the same name as a
  registry import root, so the detector suppressed an uncertain match. Resolve that import by hand.
- A `shadowed_import_binding` gap means an imported name is rebound in the same file, so its call
  references were dropped.

The detector never selects a production target and never reports readiness. An unlisted, private,
new, or custom framework is normal: registry absence never blocks setup and never permits a guessed
adapter. Continue with the generic two-direction source tracing below.

## Current documentation

For each detected framework that source inspection confirms is relevant, and only for those:

1. Resolve the version this repository actually uses from lock files, then declared specifiers,
   then the installed distribution metadata.
2. Read the version-matched official documentation online, starting from the registry's
   `documentation` or `repository` root for that entry.
3. If version-matched documentation is unavailable, or the harness has no web access, inspect the
   installed package source and type signatures read-only instead.
4. Cite every source used in the proposal, including the exact documentation URL or installed
   package path.
5. When no exact project version resolves, current official documentation may be used only after
   stating that version uncertainty explicitly in the proposal.

The target repository's actual control flow is authoritative. Documentation explains an API; it
never overrides what this repository does. Do not load documentation for undetected frameworks.
No documentation index, MCP server, or third-party documentation proxy is required for this step.

If neither documentation nor installed source is available, continue repository source tracing and
end with a source-backed proposal or an actionable `cannot wire`.

## Workflow

1. Inspect read-only, in order: framework discovery evidence, documented run paths, application
   entrypoints, tests and factories, agent constructors or orchestrators, model and tool call
   sites, then their callers. Inspect an existing user-authored
   `tests/evals/conftest.py::kensa_run(case)` without overwriting it.
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
