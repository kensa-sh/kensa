# Security Policy

## Reporting A Vulnerability

If you discover a security vulnerability in Kensa, report it privately.

Do not open a public GitHub issue. Email **satya@kensa.sh** with:

- A description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix, if you have one

You should receive an acknowledgment within 48 hours.

## Scope

Security-relevant areas in this repo include:

- Pytest harness execution through user-provided `kensa_run` fixtures
- OpenTelemetry span export and trace parsing
- Local result artifacts under `.kensa/`
- Local trace imports
- Kensa eval files under `tests/evals/`
- Environment-variable handling in `kensa doctor`, judges, and LLM adapters

Kensa OSS should write local artifacts only.

Local `.env` files and `.kensa/` runtime outputs must remain uncommitted.

## Mandatory Trace Redaction

Trace evidence passes through one mandatory redaction boundary before it can be
imported, stored, listed, sampled, inspected, or used for eval generation:

- `kensa import` refuses to write trace artifacts until redaction readiness exists
  (`kensa init` installs the `kensa[redaction]` dependencies with consent, downloads
  and checksum-verifies a pinned spaCy model, and writes `.kensa/redaction.json`).
- Every imported payload is scanned by Kensa deterministic recognizers,
  detect-secrets, Presidio built-ins, and spaCy NER, and rewritten with typed,
  instance-numbered placeholders (for example `[PERSON_1]`, `[EMAIL_ADDRESS_2]`).
  The in-memory value-to-alias map is discarded at the end of the import run and is
  never persisted.
- Redaction fails closed. Analyzer errors abort the import; missing dependencies or
  models block imports instead of degrading to weaker redaction.
- Payload exposure (`kensa traces list/sample/get`, `kensa inspect`, generation)
  is gated on a safe `kensa.redactor.v2` manifest next to the artifact. Artifacts
  with missing, older, or unsafe manifests are blocked and must be re-imported.
- Connected Langfuse imports redact fetched payloads in memory; raw connected
  payloads are never written to temporary or final files.
- Production evidence additionally requires the pinned `en_core_web_lg` model;
  the `en_core_web_sm` fallback is degraded readiness for local and staging only.

Known residual risks:

- Runtime trial run directories (`.kensa/traces/runs/<run_id>/`) contain **raw,
  unredacted** telemetry written by `kensa.tracing`. They are marked as raw source
  data, are never directly exposable as evidence, and become evidence only through
  `kensa import`. Treat these directories as sensitive and keep them uncommitted.
- Schema-owned timing fields (span/trace start, end, duration, created-at) are
  exempt from `DATE_TIME` redaction so ordering and latency signals survive; they
  are still scanned for secrets and every other entity. Preserved timestamps remain
  a quasi-identifier when correlated with external systems.

## Out Of Scope

- Vulnerabilities in upstream dependencies
- Security of user-written agent code evaluated by Kensa
- Production side effects caused by an unsafe user-provided `kensa_run` fixture
- Issues requiring physical access to the machine running Kensa
