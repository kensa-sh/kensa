# Repository Guidelines

## Project Structure & Module Organization

Kensa is a Python package under `src/kensa/`. Core pytest authoring APIs live in
`src/kensa/pytest.py`, the pytest plugin in `src/kensa/pytest_plugin.py`, tracing in
`src/kensa/tracing.py` and `src/kensa/runtime.py`, trace import helpers in
`src/kensa/traces.py`, and CLI entrypoints in `src/kensa/cli.py`.

Tests live in `tests/`. Contributor, security, and release docs live in the repo root. GitHub
templates and workflows live under `.github/`. User documentation lives in `docs/` and deploys
through Mintlify.

## Build, Test, and Development Commands

Use `uv` for local development:

```bash
uv sync --group dev
uv run pytest -q
uv run python -m coverage run -m pytest -q
uv run python -m coverage report
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv build
```

`pytest` runs the suite. Coverage is configured in `pyproject.toml` and must remain 100% for
`src/kensa`. `ruff` handles linting and formatting. `ty` runs strict type checking.

## Coding Style & Naming Conventions

Target Python 3.11+. Use 4-space indentation, explicit type hints, and small functions with
clear boundaries. Keep public API surface narrow: test authors should use `kensa.pytest`, and
process-level tracing should use `kensa.instrument()`.

Prefer existing local patterns over new abstractions. Keep eval files constrained to
`tests/evals/test_*.py`.

## Testing Guidelines

Tests use pytest. Name files `test_*.py` and tests `test_<behavior>_<condition>_<result>` when
possible. Add focused tests for every branch, artifact contract, or public behavior change.
Do not lower the coverage threshold; run `uv run python -m coverage report` before handoff.

## Commit & Pull Request Guidelines

Use conventional imperative-style commits with `feat:`, `fix:`, or `chore:`. Append `!` for
breaking changes, for example `feat!:`. Keep messages concise, for example:

```bash
git commit -m "fix: cover trace imports"
```

PR labels mirror commit prefixes for release notes: prefer `feat`, `fix`, or `chore`. Add
`breaking` when a PR changes user-facing behavior or install/API compatibility. Release notes also
accept common aliases such as `feature`, `enhancement`, `bug`, `documentation`, `dependencies`, and
`breaking-change`. PRs should include a short summary, motivation, implementation notes, and a test
plan. Link issues when applicable.

## Security & Configuration Tips

Never commit `.env`, `.venv`, coverage data, build artifacts, or `.kensa` runtime outputs.
Pytest runs must write local artifacts only. Harness fixtures should use local, test, staging,
mocked, or sandboxed dependencies, not production systems.
