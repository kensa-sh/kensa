# Security Policy

## Reporting A Vulnerability

If you discover a security vulnerability in Kensa, report it privately.

Do not open a public GitHub issue. Email **satya.borg@gmail.com** with:

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

## Out Of Scope

- Vulnerabilities in upstream dependencies
- Security of user-written agent code evaluated by Kensa
- Production side effects caused by an unsafe user-provided `kensa_run` fixture
- Issues requiring physical access to the machine running Kensa
