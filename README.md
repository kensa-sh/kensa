<div align="center">
  <a href="https://kensa.sh">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/kensa-sh/kensa/main/assets/kensa-banner-dark.png">
      <img src="https://raw.githubusercontent.com/kensa-sh/kensa/main/assets/kensa-banner-light.png" width="540" height="auto" alt="Kensa">
    </picture>
  </a>
</div>

<p align="center">Kensa turns agent traces into pytest evals that run in CI.</p>

<p align="center">
  <a href="https://github.com/kensa-sh/kensa/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/kensa-sh/kensa/ci.yml?label=CI&style=flat-square" alt="CI"></a>
  <a href="https://pypi.org/project/kensa/"><img src="https://img.shields.io/pypi/v/kensa?style=flat-square" alt="PyPI"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/python/required-version-toml?tomlFilePath=https%3A%2F%2Fraw.githubusercontent.com%2Fkensa-sh%2Fkensa%2Fmain%2Fpyproject.toml&style=flat-square" alt="Python"></a>
  <a href="https://github.com/kensa-sh/kensa/blob/main/LICENSE"><img src="https://img.shields.io/github/license/kensa-sh/kensa?style=flat-square" alt="License"></a>
  <a href="https://pepy.tech/projects/kensa"><img src="https://img.shields.io/pepy/dt/kensa?style=flat-square" alt="Downloads"></a>
</p>

<hr />

Kensa mines your real agent traces, so evals assert what your agent actually did, not what it should
have. Generated from traces or written from scratch, they live in your repository as simple, executable
files:

```python
import pytest

from kensa.pytest import judge, kensa_case


@pytest.mark.kensa(trials=3, timeout_s=180)
@pytest.mark.parametrize(
    "case",
    [
        kensa_case(
            id="refund_without_order_history",
            messages=[
                {"role": "user", "content": "I was charged $29 yesterday."},
                {"role": "assistant", "content": "I can help. Do you have an order ID?"},
                {"role": "user", "content": "No, but please refund the charge."},
            ],
        )
    ],
)
def test_refund_policy(case, kensa_run, kensa_trace):
    result = case.run(kensa_run)

    assert kensa_trace.tools.include(["lookup_customer"])
    assert kensa_trace.tools.exclude(["issue_refund"])

    verdict = judge(
        result,
        "The response must not promise an unsupported refund.",
    )
    assert verdict.passed, verdict.reasoning
```

Why? Because agents are non-deterministic: prompts drift, tools change, and models behave differently. Any change can
make them slower, more expensive, or just plain unreliable. Run Kensa in CI with the rest of your
test suite to catch those regressions before they hit prod.

## Getting started

> [!NOTE]
> `kensa>=0.9.0` is a ground-up rewrite with a new API. Older releases live [here](https://github.com/satyaborg/kensa).

Paste this into your coding agent (Claude Code, Codex, Cursor):

```text
Fetch https://kensa.sh/install and follow it.
```

Your agent installs Kensa, runs `kensa init`, then follows the `kensa-evals` lifecycle skill: setup, evidence
import, inspection, approval, generation, and verification.

<details>
<summary>Agent can't fetch URLs? Paste this instead</summary>

```text
Install `uv add --dev kensa`, run `uv run kensa init`, then use the `kensa-evals` skill.
```

Same flow, hardcoded for uv. Use this only when your agent has no web access.

</details>

### Prefer to run it yourself

Install, then hand off to your agent for the `kensa-evals` skill.

For uv projects:

```bash
uv add --dev kensa
uv run kensa init
```

For pip projects:

```bash
python -m pip install kensa
kensa init
```

For projects that track dependencies with `requirements.txt`, add `kensa`, then run `kensa init`.

In interactive mode, `kensa init` asks for the trace source, stores it and the redaction model under
`[tool.kensa]` in `pyproject.toml`, and checks credentials without printing secrets.

### CLI-only

Drive the full lifecycle from the terminal:

```bash
kensa init
kensa doctor
kensa connect langfuse
kensa import --from langfuse --limit 50
kensa traces sample --json
kensa eval
```

Use `kensa-inspect` to create a YAML review queue, approve ideas by changing `status: pending` to
`status: approved`, then run `kensa-generate`. If you do not have traces yet, capture a local run
with `kensa.instrument()` and import the JSONL.

## Core commands

| Command | What it does |
| --- | --- |
| `kensa init` | Set up the pytest harness and the `kensa-evals` skill. Use `--redaction-model large` for higher-recall model. |
| `kensa doctor` | Check that the harness is wired to a safe local agent boundary. |
| `kensa connect langfuse` | Authenticate with Langfuse and save non-secret connection metadata. |
| `kensa import --from <provider>` | Import local or connected trace evidence. |
| `kensa traces list/sample/get` | Read redacted imported TraceView evidence. |
| `kensa inspect list/lint` | Read and validate the YAML eval-idea review queue. |
| `kensa eval` | Run Kensa evals with four pytest workers by default (`--workers 1` for sequential). |

Recommended agent flow: `kensa-evals`: setup -> evidence -> inspect -> approval -> generate -> verify.
`kensa-evals` reads `[tool.kensa].evidence_source`; `kensa doctor` checks readiness.

Trace imports read bounded trace export files from JSON, JSONL, OTLP, and Langfuse. Connected
Langfuse imports use metadata from `kensa connect langfuse`. By default, `connect` verifies
credentials (without reading trace data) before saving metadata; use `--configure-only` for
metadata-only setup. API key values come from runtime env vars or a configured dotenv, and are
never written to connection metadata.

Run `kensa --help` or `kensa <command> --help` for the full CLI reference. Use `--json` when a
coding agent needs a stable machine-readable response.

## CI

```yaml
name: Kensa

on: [pull_request]

jobs:
  kensa:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: astral-sh/setup-uv@v7
      - run: uv sync
      - run: uv run --with kensa kensa eval
```

If you only use deterministic assertions, you do not need API keys. If you use LLM-as-judge
assertions, add provider secrets in CI. By default, Kensa uses a small frontier model through Any
LLM unless you override `KENSA_JUDGE_PROVIDER` or `KENSA_JUDGE_MODEL`.

## FAQ

<details>
<summary>How does Kensa work?</summary>

Kensa keeps the regression contract inside pytest. You define cases with `kensa_case(...)`, use the
`kensa_run(case)` fixture to build one case-aware conversation agent, and call
`case.run(kensa_run)`. Every run returns `CaseResult(messages, output, termination)`.
Assert traces with `kensa_trace` and reserve LLM-as-judge for semantic checks.

</details>

<details>
<summary>Why not just ask my agent to write pytest tests?</summary>

You can. The difference is evidence: an agent writing tests from scratch guesses what should
happen, while Kensa mines real traces so evals assert what your agent actually did. It adds the
primitives raw pytest lacks: `kensa_run`, `kensa_trace`, `judge()`, and trials.

</details>

<details>
<summary>How does Kensa compare to LangSmith, Braintrust, DeepEval, or Promptfoo?</summary>

Kensa is OpenTelemetry-native: it turns trace evidence into plain pytest evals that live in your
repo and gate CI, with no metric catalog or proprietary format to adopt.

| Tool | What it is | How Kensa differs |
| --- | --- | --- |
| LangSmith | Hosted observability and eval platform; datasets and results live in the service, account required | Evals are pytest committed to your repo, not datasets locked inside their platform |
| Braintrust | Proprietary eval SaaS; evals run through its SDK and land as experiments in its UI | Your tests are portable pytest you own, not results locked in a vendor format |
| DeepEval | Open-source pytest framework with a catalog of prebuilt, mostly LLM-judged metrics | Kensa generates evals from your traces and judges only what deterministic and trace checks cannot |
| Promptfoo | Open-source, language-agnostic YAML config run through its own CLI | Kensa is plain pytest, with no separate config language or runner |

All four can run in CI; the difference is that Kensa's evals are trace-generated pytest files your
repo owns, gating on tool and decision changes rather than judge scores.

</details>

<details>
<summary>When do evals call an LLM?</summary>

Only LLM-as-judge assertions call a model. Deterministic assertions, trace assertions, and normal
pytest checks do not. For judge assertions, configure `KENSA_JUDGE_PROVIDER`, `KENSA_JUDGE_MODEL`,
and provider credentials.

</details>

<details>
<summary>Can I use existing traces?</summary>

Yes. Import bounded local JSON, JSONL, OTLP, or Langfuse exports, inspect redacted TraceView
evidence, approve eval ideas, then let your agent write pytest files. Local instrumentation is
optional.

</details>

<details>
<summary>How does Kensa handle PII in traces?</summary>

Every `kensa import` keeps only allowlisted trace evidence, then replaces PII and secrets with
typed placeholders like `[PERSON_1]` before storage. If redaction is not ready, import and payload
access stay blocked until `kensa init` fixes it.

</details>

## Resources & contributing

- Read the [Kensa documentation](https://kensa.sh/docs).
- Find a bug or request a feature in [GitHub Issues](https://github.com/kensa-sh/kensa/issues).
- Read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request.
- Follow the [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) and report vulnerabilities through
  [SECURITY.md](SECURITY.md).

## License

[Apache 2.0](LICENSE)
