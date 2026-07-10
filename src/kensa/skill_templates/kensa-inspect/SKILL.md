---
name: kensa-inspect
description: >
  Read redacted Kensa TraceView evidence and write a schema-validated YAML queue of potential
  trace-backed eval ideas without writing pytest files.
---

# Kensa Inspect

Normally invoked by `kensa-evals`.

Use this skill after the harness is ready and trace evidence exists.

Trace access:

1. Prefer the latest import with `kensa traces list --json`, `kensa traces sample --json`, and
   `kensa traces get <trace_id> --json`.
2. If there is no latest import, tell the user to run `kensa import --from <provider> --source
   <file>` or ask for an explicit `--source`.
3. Every trace read is gated on the artifact's sibling redaction manifest: payloads are exposed
   only when the manifest is a safe `kensa.redactor.v2` manifest written by mandatory redaction.
   When a read is blocked (missing, older, or unsafe manifest), the fix is to re-import with
   `kensa import`; never work around the gate by reading artifact files or runtime
   `.kensa/traces/runs/` directories directly — those contain raw payloads.
4. Treat `TraceView.raw` as redacted inspection evidence only. Do not rely on provider-specific
   raw schema in code. Redacted values appear as typed instance placeholders such as
   `[PERSON_1]` or `[EMAIL_ADDRESS_2]`; the same placeholder means the same underlying value
   within one import, which is evidence you can use when correlating spans.

Before writing, read existing queue ids with `kensa inspect list --json`. Never re-propose an
existing id, regardless of its status; items are never deleted, and later stages change only
their `status` field. Write only genuinely new ideas to the new file. If a run surfaces no new
ideas, report that instead of creating an empty queue file.

Write a YAML queue at `.kensa/inspect/<timestamp>.yaml` with this shape:

```yaml
schema_version: kensa.inspect.v1
items:
  - id: tool-loop-on-empty-results
    trace_ids: [tr_abc123, tr_def456]
    source: langfuse https://cloud.langfuse.com/trace/tr_abc123
    status: pending
    failure_pattern: agent loops the search tool 6x when results come back empty
    expected_outcome: agent stops retrying after 2 empty results and tells the user
    expected_current_behavior: fail
    proposed_checks: [tools_not_called after 2 empty results, max_turns]
    case_shape: user query with a term known to return zero hits
    risks: needs a mock for the search backend
```

Field rules:

1. `id`: unique kebab-case slug, at most 64 characters.
2. `trace_ids`: one or more `TraceView.id` values backing the idea.
3. `source`: `TraceSource.provider` plus `trace_url` or `source_path`.
4. `status`: always `pending` for new items.
5. `failure_pattern`: one concrete observed behavior or risk, with the trace evidence.
6. `expected_outcome`: what correct behavior looks like.
7. `expected_current_behavior`: `pass` when the eval pins working behavior, `fail` when the
   evidence shows current behavior is broken. An idea expected to fail documents a product bug.
8. `proposed_checks`: optional plain-text hints for `kensa-generate`, using vocabulary such as
   `tools_called`, `tools_not_called`, `tool_order`, `output_contains`, `output_matches`,
   `max_turns`, `max_cost`, `max_duration`, `no_repeat_calls`. Hints only, never executable.
9. `case_shape` and `risks`: optional prose.

Ground every idea in evidence: prefer traces whose input and output survived import, and report
how many of the imported traces were evidence-rich.

Run `kensa inspect lint` after writing the queue and fix reported errors until it exits clean.

Do not write pytest files, edit `tests/evals/`, or run `kensa eval` from this skill. If no traces
exist, report that no trace source is available and return control to `kensa-evals` for the
evidence stage.
