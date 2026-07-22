---
name: kensa-generate
description: >
  Write and maintain pytest-native Kensa eval files from approved ideas or explicit user-approved
  cases.
---

# Kensa Generate

Normally invoked by `kensa-evals`.

Use this phase skill only to write `tests/evals/test_*.py` files with `kensa.pytest`.

Inputs you may act on:

1. Items reported by `kensa inspect list --status approved --json`.
2. An eval idea or case explicitly approved by the user in chat.

Ignore queue items marked `status: pending` or `status: rejected`.

Workflow:

1. Read the approved idea or user-approved case.
2. Build case inputs from `kensa traces get` payloads for `trace_ids` and export them to
   `tests/evals/data/*.json`. If access is blocked, return to `kensa-evals` for re-import rather
   than reading raw files. Preserve redaction placeholders verbatim. Invent inputs only when no
   trace payload exists.
3. Read the `kensa.pytest` and `kensa.case` source or docs for the authoring contract before
   writing; do not guess the API.
4. Write or edit only focused pytest eval files under `tests/evals/test_*.py`.
5. Use `kensa.pytest.kensa_case`, `KensaTrace` assertions, and `judge` as needed. Treat
   `case.run(...)` as `RunResult`: assert evaluated values through `.output`, visible
   dialogue through `.messages`, and stop behavior through `.termination`. Pass the complete result
   to `judge` when conversation context matters. Treat the item's `proposed_checks` as hints to
   translate into these assertions or discard, never as a contract.
6. Run targeted pytest or `kensa eval` and fix eval mistakes. Runs that make many real model
   calls take minutes; run them in the background when the harness supports it.
7. A failing eval whose queue item declared `expected_current_behavior: fail` is a product
   finding, not an eval mistake. Do not weaken the eval to make it pass.
8. After an item's eval is written and its targeted run behaves as expected, edit that item in
   its queue file to `status: generated` so later runs do not re-generate it.
9. Preserve existing eval files unless the requested change requires editing them.

Do not import traces, create inspection queue items, invent new inspect ideas, or act on
unapproved ideas in this skill.
