# Contributing To Kensa

This repo is Kensa's pytest agent eval harness. Keep changes small, typed, and
covered by tests.

## Setup

```bash
git clone https://github.com/kensa-sh/kensa.git
cd kensa
uv sync --group dev
```

Pip users can install the same editable development environment with:

```bash
python -m pip install -e . --group dev
```

## Testing In Another Local Project

When testing Kensa from another `uv` project, prefer an editable path dependency so the
consumer uses your live checkout. For example, from a sibling project named `acme`:

```bash
cd ~/Projects/acme
uv add --editable ../kensa
uv run python -c "import kensa; print(kensa.__file__)"
uv run pytest -q
```

For a one-off smoke test that does not update the consumer project's `pyproject.toml` or
`uv.lock`, use `--with-editable`:

```bash
cd ~/Projects/acme
uv run --with-editable ../kensa python -c "import kensa; print(kensa.__file__)"
uv run --with-editable ../kensa pytest -q
```

With pip, install Kensa editable into the consumer project's active virtualenv:

```bash
cd ~/Projects/acme
python -m pip install -e ../kensa
python -c "import kensa; print(kensa.__file__)"
pytest -q
```

For repeat use, add the editable path dependency to the consumer project's
`requirements-dev.txt`:

```text
-e ../kensa
```

Pip does not have a direct equivalent of `uv run --with-editable` for a temporary command.
For one-off smoke tests, either uninstall Kensa afterward with
`python -m pip uninstall kensa` or use a disposable virtualenv.

## Development Checks

Run these before opening a PR:

```bash
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run pytest -q
uv run python -m coverage run -m pytest -q
uv run python -m coverage report
uv build
```

With pip, run the same tools from the active virtualenv:

```bash
ruff check .
ruff format --check .
ty check
pytest -q
python -m coverage run -m pytest -q
python -m coverage report
python -m build
```

Coverage is intentionally strict: `src/kensa` must stay at 100%.

The default suite is offline. Tests marked `live` are skipped unless explicitly enabled.

## Live Integration Tests

Live provider tests exercise OpenAI and Anthropic through the same public LLM and pytest/judge
surfaces that users run. They are opt-in because they require credentials and may incur API cost.

1. Put local credentials in `.env`:

   ```bash
   OPENAI_API_KEY=...
   ANTHROPIC_API_KEY=...
   ```

2. Run the live tests:

   ```bash
   uv run pytest tests/integration --run-live
   ```

The test harness loads `.env` with `python-dotenv` only when `--run-live` is present. Do not
commit `.env`, provider responses, or generated `.kensa` artifacts.

Provider-specific subsets are available:

```bash
uv run pytest tests/integration --run-live -m openai
uv run pytest tests/integration --run-live -m anthropic
```

CI does not run live provider tests. Maintainers can run them locally before releases or when
changing `src/kensa/llm.py`, `src/kensa/judge.py`, or the pytest plugin.

## Releases

Maintainers release from a clean, CI-green `main` checkout:

```bash
./scripts/release.sh patch --dry-run
./scripts/release.sh patch
./scripts/release.sh publish
```

Use `minor` or `major` when appropriate. The bump command opens a version-bump PR. After that PR
merges, `publish` pushes the release tag. The tag workflow publishes to PyPI and creates the GitHub
Release with generated notes.

## Code Conventions

- Python 3.11+.
- Strict typing is enforced with `ty` and `all = "error"`.
- Prefer explicit datamodels and stable serialized contracts for artifacts.
- Keep public API surface narrow: `kensa.pytest` for test authoring, `kensa.instrument()`
  for process-level tracing.
- Keep pytest runs local-only; do not add implicit network export behavior.
- Use structured parsers and serializers rather than ad hoc string parsing.
- Keep live API tests behind `--run-live`; normal test and CI runs must stay offline.

## Security Hygiene

- `.env`, `.venv`, coverage files, build outputs, and `.kensa` runtime outputs are ignored.
- If a secret is committed, revoke it before opening a cleanup PR.

## Pull Requests

- Use a branch named `feat/...`, `fix/...`, or `chore/...`.
- Use conventional commit prefixes: `feat:`, `fix:`, `test:`, `docs:`, or `chore:`.
- GitHub release notes are grouped by PR labels; the PR Labels workflow applies those labels from
  conventional PR titles.
- Keep eval files constrained to `tests/evals/test_*.py`.
- Include a short test plan in the PR.
