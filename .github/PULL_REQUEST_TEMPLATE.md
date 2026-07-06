## What

<!-- One-sentence summary of the change. -->

## Why

<!-- What problem does this solve? Link an issue if applicable. -->

## How

<!-- Briefly describe the approach. -->

## Test Plan

- [ ] `uv run ruff check .`
- [ ] `uv run ruff format --check .`
- [ ] `uv run ty check`
- [ ] `uv run pytest -q`
- [ ] `uv run python -m coverage run -m pytest -q && uv run python -m coverage report`
- [ ] `uv build`
